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

from app.config import CATEGORIES, CURRENCY
from app.database import init_db
from app.core.observability import log_event, timed_event
from app.services.skills import execute_skill

logger = logging.getLogger(__name__)

_AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)")
_RECORD_RE = re.compile(
    r"^\s*(?P<note>.+?)\s+(?P<amount>\d+(?:\.\d+)?)\s*(?P<currency>[A-Za-z]{3}|元|块|人民币)?\s*$"
)
_DELETE_BY_ID_RE = re.compile(r"^\s*删除\s*#?(?P<expense_id>\d+)\s*$")
_RECENT_RE = re.compile(r"^\s*(?:看看|看下|查看)?最近\s*(?P<limit>\d+)?\s*笔")
_BUDGET_SET_RE = re.compile(
    r"^\s*(?P<category>[\u4e00-\u9fffA-Za-z_]+)\s*预算\s*(?:设为|改成|改为|调整为)\s*(?P<amount>\d+(?:\.\d+)?)\s*$"
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
    if value in {"元", "块", "人民币"}:
        return "CNY"
    return value


def _infer_category(note: str) -> str:
    lowered = note.lower()
    for category, hints in _CATEGORY_HINTS.items():
        if any(hint.lower() in lowered for hint in hints):
            return category
    return "其他" if "其他" in CATEGORIES else CATEGORIES[0]


def _infer_scope(text: str) -> str:
    if any(token in text for token in ("老婆", "小白")):
        return "spouse"
    if any(token in text for token in ("我们", "家庭", "全家")):
        return "family"
    return "me"


def _infer_include_special(text: str) -> bool:
    return any(token in text for token in ("专项", "旅行", "计划", "全部"))


def _parse_record_expense(text: str) -> dict[str, Any]:
    match = _RECORD_RE.match(text)
    if not match:
        raise ValueError("Could not parse a simple expense from this message.")
    note = match.group("note").strip()
    amount = float(match.group("amount"))
    currency = _normalize_currency(match.group("currency"))
    category = _infer_category(note)
    return {
        "category": category,
        "amount": amount,
        "currency": currency,
        "note": note,
    }


def _parse_recent_expenses(text: str) -> dict[str, Any]:
    match = _RECENT_RE.match(text)
    limit = int(match.group("limit")) if match and match.group("limit") else 5
    return {
        "scope": _infer_scope(text),
        "limit": limit,
        "ledger_type": "special" if "专项" in text else "",
    }


def _parse_month_total(text: str) -> dict[str, Any]:
    return {
        "scope": _infer_scope(text),
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


def _render_month_total(result: dict[str, Any]) -> str:
    label = result.get("label", "本月")
    total = float(result.get("total", 0))
    currency = result.get("currency", CURRENCY)
    if result.get("includes_special"):
        return f"{label}本月合计（含专项）是 {total:.2f} {currency}。"
    return f"{label}本月合计是 {total:.2f} {currency}。"


def _render_budget_query(result: dict[str, Any]) -> str:
    budgets = result.get("budgets") or []
    if not budgets:
        return str(result.get("message") or "目前还没有设置预算。")
    lines = ["当前预算："]
    for item in budgets[:8]:
        lines.append(
            f"{item['category']} {float(item['spent']):.2f}/{float(item['monthly_limit']):.2f} {CURRENCY}"
        )
    return "\n".join(lines)


def _render_budget_set(result: dict[str, Any]) -> str:
    return str(result.get("message") or "预算已更新。")


def _render_delete_by_id(result: dict[str, Any]) -> str:
    return str(result.get("confirmation") or result.get("message") or "已删除。")


_WORKBENCH_ACTIONS: dict[str, tuple[str, Any]] = {
    "record_expense": ("record_expense", _parse_record_expense),
    "recent_expenses": ("query_recent_expenses", _parse_recent_expenses),
    "month_total": ("query_monthly_total", _parse_month_total),
    "budget_query": ("query_budget", _parse_budget_query),
    "budget_set": ("set_budget", _parse_budget_set),
    "delete_by_id": ("delete_expense_by_id", _parse_delete_by_id),
}

_WORKBENCH_RENDERERS: dict[str, Any] = {
    "record_expense": _render_record_expense,
    "recent_expenses": _render_recent_expenses,
    "month_total": _render_month_total,
    "budget_query": _render_budget_query,
    "budget_set": _render_budget_set,
    "delete_by_id": _render_delete_by_id,
}


def run_workbench_action(action: str, user_id: int, user_name: str, text: str) -> dict[str, Any]:
    if action not in _WORKBENCH_ACTIONS:
        raise ValueError(f"Unsupported workbench action: {action}")
    skill_name, parser = _WORKBENCH_ACTIONS[action]
    params = parser(text)
    log_event(
        logger,
        "finance_workbench.action_start",
        action=action,
        skill_name=skill_name,
        user_id=user_id,
    )
    with timed_event(
        logger,
        "finance_workbench.action_complete",
        action=action,
        skill_name=skill_name,
        user_id=user_id,
    ):
        raw_result = execute_skill(skill_name, user_id, user_name, params)
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
