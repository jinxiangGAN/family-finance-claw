"""Simple finance workbench for latency-sensitive Telegram turns.

This module is intentionally narrow: it standardizes a few very common
household finance actions so resident Codex can call one stable workbench
command instead of planning a broader tool chain.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from typing import Any

from app.config import CATEGORIES, CURRENCY, FAMILY_MEMBERS
from app.database import init_db
from app.core.observability import log_event, timed_event
from app.services.expense_service import get_today_total
from app.services.fx_service import normalize_currency_code
from app.services.skills import execute_skill
from app.services.stats_service import get_spouse_id, get_member_name

logger = logging.getLogger(__name__)

_AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)")
_RECORD_RE = re.compile(
    r"^\s*(?P<note>.+?)\s*(?P<amount>\d+(?:\.\d+)?)\s*(?P<currency>[A-Za-z]{3}|元|块|人民币)?\s*$"
)
_RECORD_PREFIX_RE = re.compile(r"^\s*(?:记一笔|记账|入账|记下|记一下)\s*[，,:：]?\s*")
_RECORD_OWNER_SUFFIX_RE = re.compile(
    r"\s*[，,]?\s*(?:我花的|我付的|我出的|是我花的|小鸡毛花的|小鸡毛付的|小鸡毛出的|小白花的|小白付的|小白出的)\s*$"
)
_DELETE_BY_ID_RE = re.compile(r"^\s*删除\s*#?(?P<expense_id>\d+)\s*$")
_RECENT_RE = re.compile(r"^\s*(?:看看|看下|查看)?最近\s*(?P<limit>\d+)?\s*笔")
_TODAY_TOTAL_RE = re.compile(
    r"^\s*(?:查看|看看)?(?:今日|今天)(?:我|我们|家庭|全家)?(?:所有)?(?:花费|花销|开销|支出|消费|花了多少|一共花了多少)\s*[？?]?\s*$"
)
_DETAIL_RE = re.compile(
    r"^\s*(?:查看|看看|查下|查一下)?(?:[\u4e00-\u9fffA-Za-z_]+的?)?(?:今天|今日|本月|这个月|最近)?(?:花费|花销|开销|支出|消费)?(?:明细|细则)\s*[？?]?\s*$"
)
_BUDGET_SET_RE = re.compile(
    r"^\s*(?P<category>[\u4e00-\u9fffA-Za-z_]+)\s*预算(?:\s*(?:设为|改成|改为|调整为))?\s*(?P<amount>\d+(?:\.\d+)?)(?:\s*(?P<currency>[A-Za-z]{3}|元|块|人民币))?\s*$"
)
_EXCHANGE_RATE_EQUAL_RE = re.compile(
    r"^\s*(?:现在|今天)?\s*1\s*(?P<base>[\u4e00-\u9fffA-Za-z]+)\s*(?:等于|兑|对)\s*多少\s*(?P<quote>[\u4e00-\u9fffA-Za-z]+)\s*[？?]?\s*$"
)
_EXCHANGE_RATE_PAIR_RE = re.compile(
    r"^\s*(?:现在|今天|查下|查一下|看看)?\s*(?P<base>[\u4e00-\u9fffA-Za-z]+)\s*(?:兑|对|/)\s*(?P<quote>[\u4e00-\u9fffA-Za-z]+)\s*(?:汇率)?(?:多少|是多少)?\s*[？?]?\s*$"
)
_EXCHANGE_RATE_SINGLE_RE = re.compile(
    r"^\s*(?:现在|今天|查下|查一下|看看)?\s*(?P<base>[\u4e00-\u9fffA-Za-z]+)\s*(?:汇率)(?:多少|是多少)?\s*[？?]?\s*$"
)

_CATEGORY_HINTS: dict[str, tuple[str, ...]] = {
    "餐饮": ("饭", "餐", "奶茶", "咖啡", "外卖", "早餐", "午饭", "晚饭", "火锅"),
    "交通": ("打车", "地铁", "公交", "车费", "停车", "加油", "机票", "高铁", "火车"),
    "超市": ("超市", "买菜", "菜场", "水果", "蔬菜", "杂货"),
    "购物": ("淘宝", "衣服", "鞋", "包", "购物", "买了"),
    "房租": ("房租", "租金"),
    "水电网": ("电费", "水费", "网费", "燃气", "话费", "宽带"),
    "娱乐": ("电影", "游戏", "ktv", "演出", "娱乐", "桌游"),
    "医疗": ("医院", "药", "挂号", "体检", "医疗", "牙医"),
}


def _normalize_currency(raw: str | None) -> str:
    if not raw:
        return CURRENCY
    value = raw.strip().upper()
    # In a Singapore-first household context, colloquial "元/块" should follow
    # the default configured currency unless the user explicitly says RMB/CNY.
    if value in {"元", "块"}:
        return CURRENCY
    if value == "人民币":
        return "CNY"
    return value


def _infer_category(note: str) -> str:
    lowered = note.lower()
    for category, hints in _CATEGORY_HINTS.items():
        if any(hint.lower() in lowered for hint in hints):
            return category
    return "其他" if "其他" in CATEGORIES else CATEGORIES[0]


def _infer_scope(text: str, user_id: int) -> str:
    if any(token in text for token in ("我们", "家庭", "全家")):
        return "family"

    me_name = get_member_name(user_id)
    spouse_id = get_spouse_id(user_id)
    spouse_name = get_member_name(spouse_id) if spouse_id is not None else ""

    if spouse_name and spouse_name in text:
        return "spouse"
    if me_name and me_name in text:
        return "me"

    if user_id in FAMILY_MEMBERS:
        if FAMILY_MEMBERS[user_id] == "小鸡毛" and any(token in text for token in ("老婆", "老伴")):
            return "spouse"
        if FAMILY_MEMBERS[user_id] == "小白" and any(token in text for token in ("老公", "先生")):
            return "spouse"

    if any(token in text for token in ("老婆", "老公", "配偶", "另一半", "对象")):
        return "spouse"
    return "me"


def _infer_include_special(text: str) -> bool:
    return any(token in text for token in ("专项", "旅行", "计划", "全部"))


def _normalize_record_text(text: str) -> str:
    stripped = _RECORD_PREFIX_RE.sub("", text.strip())
    stripped = _RECORD_OWNER_SUFFIX_RE.sub("", stripped)
    return stripped.strip()


def _resolve_expense_owner(text: str, user_id: int, user_name: str) -> tuple[int, str]:
    stripped = text.strip()
    if re.search(r"(?:^|[，,\s])我(?:花的|付的|出的|来付)\s*$", stripped):
        return user_id, user_name

    for member_id, member_name in FAMILY_MEMBERS.items():
        escaped = re.escape(member_name)
        if re.search(rf"^\s*(?:给|替|帮)?{escaped}(?:记一笔|记账|入账)?[，,:：\s]", stripped):
            return member_id, member_name
        if re.search(rf"^\s*{escaped}的", stripped):
            return member_id, member_name
        if re.search(rf"(?:^|[，,\s]){escaped}(?:花的|付的|出的|来付)\s*$", stripped):
            return member_id, member_name

    if "老婆花的" in stripped or "老婆付的" in stripped or "老婆出的" in stripped:
        for member_id, member_name in FAMILY_MEMBERS.items():
            if member_name == "小白":
                return member_id, member_name
    if stripped.startswith("老婆的"):
        for member_id, member_name in FAMILY_MEMBERS.items():
            if member_name == "小白":
                return member_id, member_name
    if "老公花的" in stripped or "老公付的" in stripped or "老公出的" in stripped:
        for member_id, member_name in FAMILY_MEMBERS.items():
            if member_name == "小鸡毛":
                return member_id, member_name
    if stripped.startswith("老公的"):
        for member_id, member_name in FAMILY_MEMBERS.items():
            if member_name == "小鸡毛":
                return member_id, member_name

    return user_id, user_name


def _parse_record_expense(text: str, user_id: int, user_name: str) -> dict[str, Any]:
    match = _RECORD_RE.match(_normalize_record_text(text))
    if not match:
        raise ValueError("Could not parse a simple expense from this message.")
    note = match.group("note").strip()
    amount = float(match.group("amount"))
    currency = _normalize_currency(match.group("currency"))
    category = _infer_category(note)
    owner_user_id, owner_user_name = _resolve_expense_owner(text, user_id, user_name)
    return {
        "category": category,
        "amount": amount,
        "currency": currency,
        "note": note,
        "owner_user_id": owner_user_id,
        "owner_user_name": owner_user_name,
    }


def _parse_recent_expenses(text: str, user_id: int) -> dict[str, Any]:
    match = _RECENT_RE.match(text)
    limit = int(match.group("limit")) if match and match.group("limit") else 5
    return {
        "scope": _infer_scope(text, user_id),
        "limit": limit,
        "ledger_type": "special" if "专项" in text else "",
    }


def _parse_expense_details(text: str, user_id: int) -> dict[str, Any]:
    scope = _infer_scope(text, user_id)
    include_special = _infer_include_special(text)
    category = ""
    for item in CATEGORIES:
        if item in text:
            category = item
            break
    if category:
        return {
            "mode": "category",
            "scope": scope,
            "category": category,
            "limit": 20,
            "include_special": include_special,
        }
    return {
        "mode": "recent",
        "scope": scope,
        "limit": 20,
        "ledger_type": "special" if "专项" in text else "",
    }


def _parse_month_total(text: str, user_id: int) -> dict[str, Any]:
    return {
        "scope": _infer_scope(text, user_id),
        "include_special": _infer_include_special(text),
    }


def _parse_today_total(text: str, user_id: int) -> dict[str, Any]:
    return {
        "scope": _infer_scope(text, user_id),
        "include_special": _infer_include_special(text),
    }


def _parse_budget_query(text: str) -> dict[str, Any]:
    return {}


def _parse_budget_set(text: str) -> dict[str, Any]:
    match = _BUDGET_SET_RE.match(text)
    if not match:
        raise ValueError("Could not parse the budget category and amount.")
    category = match.group("category").strip()
    amount = float(match.group("amount"))
    if category in {"总", "总计", "全部", "家庭总预算"}:
        category = "_total"
    return {
        "category": category,
        "amount": amount,
    }


def _parse_exchange_rate(text: str) -> dict[str, Any]:
    match = _EXCHANGE_RATE_EQUAL_RE.match(text) or _EXCHANGE_RATE_PAIR_RE.match(text)
    if match:
        return {
            "base_currency": normalize_currency_code(match.group("base")),
            "quote_currency": normalize_currency_code(match.group("quote")),
        }
    match = _EXCHANGE_RATE_SINGLE_RE.match(text)
    if match:
        return {
            "base_currency": normalize_currency_code(match.group("base")),
            "quote_currency": CURRENCY,
        }
    raise ValueError("Could not parse the exchange rate query.")


def _parse_delete_by_id(text: str) -> dict[str, Any]:
    match = _DELETE_BY_ID_RE.match(text)
    if not match:
        raise ValueError("Could not parse the expense id to delete.")
    return {
        "expense_id": int(match.group("expense_id")),
    }


def _render_record_expense(result: dict[str, Any]) -> str:
    return str(result.get("confirmation") or result.get("message") or "已记录。")


def _render_recent_expenses(result: dict[str, Any]) -> str:
    items = result.get("items") or []
    if not items:
        return "最近没有找到相关账目。"
    lines = [f"最近 {len(items)} 笔："]
    for item in items[:10]:
        lines.append(
            f"#{item['id']} {item['user_name']} / {item['category']} {float(item['amount']):.2f} {item['currency']}"
            f"{' / ' + item['note'] if item.get('note') else ''}"
        )
    return "\n".join(lines)


def _render_expense_details(result: dict[str, Any]) -> str:
    return _render_recent_expenses(result)


def _render_month_total(result: dict[str, Any]) -> str:
    label = result.get("label", "本月")
    total = float(result.get("total", 0))
    currency = result.get("currency", CURRENCY)
    if result.get("includes_special"):
        return f"{label}本月合计（含专项）是 {total:.2f} {currency}。"
    return f"{label}本月合计是 {total:.2f} {currency}。"


def _render_today_total(result: dict[str, Any]) -> str:
    scope = str(result.get("scope") or "me")
    label = str(result.get("label") or "").strip()
    if not label:
        label = {
            "me": "今天",
            "spouse": "配偶今天",
            "family": "今天家庭",
        }.get(scope, "今天")
    elif scope != "family":
        label = f"{label}今天"
    total = float(result.get("total", 0))
    currency = str(result.get("currency") or CURRENCY)
    if result.get("includes_special"):
        return f"{label}合计（含专项）是 {total:.2f} {currency}。"
    return f"{label}合计是 {total:.2f} {currency}。"


def _render_budget_query(result: dict[str, Any]) -> str:
    budgets = result.get("budgets") or []
    budget_groups = result.get("budget_groups") or []
    if not budgets and not budget_groups:
        return str(result.get("message") or "目前还没有设置预算。")
    lines = ["当前预算："]
    for item in budgets[:8]:
        lines.append(
            f"{item['category']} {float(item['spent']):.2f}/{float(item['monthly_limit']):.2f} {CURRENCY}"
        )
    for item in budget_groups[:5]:
        categories = " / ".join(item.get("categories") or [])
        lines.append(
            f"{item['name']}（{categories}） {float(item['spent']):.2f}/{float(item['monthly_limit']):.2f} {CURRENCY}"
        )
    return "\n".join(lines)


def _render_budget_set(result: dict[str, Any]) -> str:
    return str(result.get("message") or "预算已更新。")


def _render_delete_by_id(result: dict[str, Any]) -> str:
    return str(result.get("confirmation") or result.get("message") or "已删除。")


def _render_exchange_rate(result: dict[str, Any]) -> str:
    return str(result.get("message") or "这次没查到汇率。")


_WORKBENCH_ACTIONS: dict[str, tuple[str, Any]] = {
    "record_expense": ("record_expense", _parse_record_expense),
    "recent_expenses": ("query_recent_expenses", _parse_recent_expenses),
    "expense_details": ("query_recent_expenses", _parse_expense_details),
    "month_total": ("query_monthly_total", _parse_month_total),
    "today_total": ("query_today_total", _parse_today_total),
    "exchange_rate": ("query_exchange_rate", _parse_exchange_rate),
    "budget_query": ("query_budget", _parse_budget_query),
    "budget_set": ("set_budget", _parse_budget_set),
    "delete_by_id": ("delete_expense_by_id", _parse_delete_by_id),
}

_WORKBENCH_RENDERERS: dict[str, Any] = {
    "record_expense": _render_record_expense,
    "recent_expenses": _render_recent_expenses,
    "expense_details": _render_expense_details,
    "month_total": _render_month_total,
    "today_total": _render_today_total,
    "exchange_rate": _render_exchange_rate,
    "budget_query": _render_budget_query,
    "budget_set": _render_budget_set,
    "delete_by_id": _render_delete_by_id,
}


def run_workbench_action(action: str, user_id: int, user_name: str, text: str) -> dict[str, Any]:
    if action not in _WORKBENCH_ACTIONS:
        raise ValueError(f"Unsupported workbench action: {action}")
    skill_name, parser = _WORKBENCH_ACTIONS[action]
    if action == "record_expense":
        params = parser(text, user_id, user_name)
    elif action in {"recent_expenses", "expense_details", "month_total", "today_total"}:
        params = parser(text, user_id)
    else:
        params = parser(text)
    effective_user_id = int(params.pop("owner_user_id", user_id))
    effective_user_name = str(params.pop("owner_user_name", user_name))
    log_event(
        logger,
        "finance_workbench.action_start",
        action=action,
        skill_name=skill_name,
        user_id=effective_user_id,
    )
    with timed_event(
        logger,
        "finance_workbench.action_complete",
        action=action,
        skill_name=skill_name,
        user_id=effective_user_id,
    ):
        if action == "today_total":
            raw_result = get_today_total(
                user_id=effective_user_id,
                scope=str(params.get("scope") or "me"),
                include_special=bool(params.get("include_special", False)),
            )
            raw_result["label"] = get_member_name(effective_user_id) if raw_result.get("scope") == "me" else (
                get_member_name(get_spouse_id(effective_user_id)) if raw_result.get("scope") == "spouse" and get_spouse_id(effective_user_id) is not None else "家庭"
            )
        elif action == "expense_details":
            if str(params.get("mode") or "") == "category":
                raw_result = execute_skill("query_category_items", effective_user_id, effective_user_name, {
                    "scope": params.get("scope", "me"),
                    "category": params.get("category", "其他"),
                    "limit": params.get("limit", 20),
                    "include_special": bool(params.get("include_special", False)),
                })
            else:
                raw_result = execute_skill("query_recent_expenses", effective_user_id, effective_user_name, {
                    "scope": params.get("scope", "me"),
                    "limit": params.get("limit", 20),
                    "ledger_type": params.get("ledger_type", ""),
                })
        else:
            raw_result = execute_skill(skill_name, effective_user_id, effective_user_name, params)
    renderer = _WORKBENCH_RENDERERS[action]
    success = bool(raw_result.get("success", False))
    reply = renderer(raw_result) if success else str(raw_result.get("message") or "这次操作失败了。")
    result = {
        "success": success,
        "action": action,
        "skill_name": skill_name,
        "params": params,
        "reply": reply.strip(),
        "payload": raw_result,
    }
    log_event(
        logger,
        "finance_workbench.action_result",
        action=action,
        skill_name=skill_name,
        user_id=user_id,
        success=success,
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Simple finance workbench")
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--user-name", required=True)
    parser.add_argument("--action", required=True, choices=sorted(_WORKBENCH_ACTIONS.keys()))
    parser.add_argument("--text", required=True)
    args = parser.parse_args()

    init_db()
    result = run_workbench_action(args.action, args.user_id, args.user_name, args.text)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
