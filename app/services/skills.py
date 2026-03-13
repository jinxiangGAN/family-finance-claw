"""Skill definitions: all DB operations as callable functions for the LLM agent.

Each skill function accepts a dict of parameters and returns a dict result.
The TOOL_DEFINITIONS list provides the function-calling schema for the LLM.
"""

import logging
from datetime import datetime
from typing import Any, Optional

from zoneinfo import ZoneInfo

from app.config import CATEGORIES, CURRENCY, FAMILY_MEMBERS, TIMEZONE
from app.database import get_connection
from app.models.expense import Expense
from app.services.expense_service import delete_last_expense, export_expenses_csv, save_expense
from app.services.stats_service import (
    get_category_total,
    get_member_name,
    get_month_summary,
    get_month_total,
    get_spouse_id,
    resolve_user_ids,
)

logger = logging.getLogger(__name__)

# ⚠️ REFERENCE exchange rates — NOT real-time.
# These are approximate mid-market rates for quick household bookkeeping.
# For actual financial decisions, check a live source.
# TODO: Replace with a live API (e.g. exchangerate-api.com) if long-term accuracy matters.
EXCHANGE_RATES: dict[str, float] = {
    "SGD": 1.0,
    "CNY": 0.19,   # 1 CNY ≈ 0.19 SGD
    "RMB": 0.19,
    "USD": 1.35,   # 1 USD ≈ 1.35 SGD
    "AUD": 0.88,   # 1 AUD ≈ 0.88 SGD
    "JPY": 0.009,  # 1 JPY ≈ 0.009 SGD
    "MYR": 0.30,   # 1 MYR ≈ 0.30 SGD
    "EUR": 1.45,   # 1 EUR ≈ 1.45 SGD
    "GBP": 1.70,   # 1 GBP ≈ 1.70 SGD
    "THB": 0.038,  # 1 THB ≈ 0.038 SGD
    "KRW": 0.001,  # 1 KRW ≈ 0.001 SGD
}


def _convert_to_sgd(amount: float, currency: str) -> float:
    """Convert amount to SGD using exchange rates."""
    currency = currency.upper()
    if currency == "SGD":
        return amount
    rate = EXCHANGE_RATES.get(currency)
    if rate is None:
        logger.warning("Unknown currency %s, treating as SGD", currency)
        return amount
    return round(amount * rate, 2)


# ═══════════════════════════════════════════
#  Skill implementations
# ═══════════════════════════════════════════

def skill_record_expense(user_id: int, user_name: str, params: dict) -> dict:
    """Record one expense with optional currency and event tag."""
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    category = params.get("category", "其他")
    if category not in CATEGORIES:
        category = "其他"
    amount = float(params.get("amount", 0))
    currency = params.get("currency", CURRENCY).upper()
    note = params.get("note", "")
    event_tag = params.get("event_tag", "")

    # If no event tag provided, check for active event
    if not event_tag:
        event_tag = _get_active_event(user_id)

    amount_sgd = _convert_to_sgd(amount, currency)

    expense = Expense(
        user_id=user_id,
        user_name=user_name,
        category=category,
        amount=amount,
        currency=currency,
        amount_sgd=amount_sgd,
        note=note,
        event_tag=event_tag,
        created_at=now.isoformat(),
    )
    row_id = save_expense(expense)

    budget_alert = _check_budget_alert(user_id, category)

    # ── Build formatted confirmation string ──
    confirm_parts = [f"✅ 已记录：{category} {amount:.2f} {currency}"]
    if currency != CURRENCY:
        confirm_parts[0] += f" → {amount_sgd:.2f} {CURRENCY}（参考汇率）"
    confirm_parts.append(f"👤 归属：{user_name}")
    if note:
        confirm_parts.append(f"📝 备注：{note}")
    if event_tag:
        confirm_parts.append(f"🏷 事件：{event_tag}")
    if budget_alert:
        confirm_parts.append(budget_alert)
    confirmation = "\n".join(confirm_parts)

    result: dict[str, Any] = {
        "success": True,
        "id": row_id,
        "category": category,
        "amount": amount,
        "currency": currency,
        "note": note,
        "budget_alert": budget_alert,
        "confirmation": confirmation,
    }
    if currency != CURRENCY:
        result["amount_sgd"] = amount_sgd
        result["default_currency"] = CURRENCY
    if event_tag:
        result["event_tag"] = event_tag
    return result


def skill_delete_last(user_id: int, user_name: str, params: dict) -> dict:
    """Delete the most recent expense."""
    deleted = delete_last_expense(user_id)
    if deleted:
        confirmation = (
            f"🗑 已撤销最后一笔：{deleted.category} {deleted.amount:.2f} {deleted.currency}"
            f"（{deleted.note}）"
        )
        return {
            "success": True,
            "confirmation": confirmation,
            "deleted": {
                "category": deleted.category,
                "amount": deleted.amount,
                "currency": deleted.currency,
                "note": deleted.note,
            },
        }
    return {"success": False, "message": "没有可以删除的记录"}


def skill_query_monthly_total(user_id: int, user_name: str, params: dict) -> dict:
    """Query monthly total spending."""
    scope = params.get("scope", "me")
    user_ids = resolve_user_ids(scope, user_id)
    total = get_month_total(user_ids)
    label = _scope_label(scope, user_id)
    return {"label": label, "total": total, "currency": CURRENCY}


def skill_query_category_total(user_id: int, user_name: str, params: dict) -> dict:
    """Query spending for a specific category."""
    scope = params.get("scope", "me")
    category = params.get("category", "其他")
    user_ids = resolve_user_ids(scope, user_id)
    total = get_category_total(category, user_ids)
    label = _scope_label(scope, user_id)
    return {"label": label, "category": category, "total": total, "currency": CURRENCY}


def skill_query_summary(user_id: int, user_name: str, params: dict) -> dict:
    """Query monthly summary by category."""
    scope = params.get("scope", "me")
    user_ids = resolve_user_ids(scope, user_id)
    summary = get_month_summary(user_ids)
    grand_total = sum(item["total"] for item in summary)
    label = _scope_label(scope, user_id)
    return {
        "label": label,
        "summary": summary,
        "grand_total": grand_total,
        "currency": CURRENCY,
    }


def skill_set_budget(user_id: int, user_name: str, params: dict) -> dict:
    """Set a monthly budget for a category or total."""
    category = params.get("category", "_total")
    amount = float(params.get("amount", 0))
    if amount <= 0:
        return {"success": False, "message": "预算金额必须大于0"}

    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO budgets (user_id, category, monthly_limit, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, category) DO UPDATE SET monthly_limit = ?, updated_at = ?",
            (user_id, category, amount, now.isoformat(), amount, now.isoformat()),
        )
        conn.commit()

    cat_label = "个人总预算" if category == "_total" else f"{category}预算"
    return {
        "success": True,
        "message": f"已设置{cat_label}：{amount:.2f} {CURRENCY}/月",
        "category": category,
        "monthly_limit": amount,
        "currency": CURRENCY,
    }


def skill_query_budget(user_id: int, user_name: str, params: dict) -> dict:
    """Query budget status."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT category, monthly_limit FROM budgets WHERE user_id = ?",
            (user_id,),
        ).fetchall()

    if not rows:
        return {"success": True, "budgets": [], "message": "尚未设置任何预算"}

    budgets = []
    for row in rows:
        cat = row["category"]
        limit_val = float(row["monthly_limit"])
        if cat == "_total":
            spent = get_month_total([user_id])
            cat_label = "个人总计"
        else:
            spent = get_category_total(cat, [user_id])
            cat_label = cat
        remaining = limit_val - spent
        budgets.append({
            "category": cat_label,
            "monthly_limit": limit_val,
            "spent": spent,
            "remaining": remaining,
            "over_budget": remaining < 0,
        })

    return {"success": True, "budgets": budgets, "currency": CURRENCY}


def skill_get_spending_analysis(user_id: int, user_name: str, params: dict) -> dict:
    """Get raw spending data for LLM analysis."""
    scope = params.get("scope", "me")
    user_ids = resolve_user_ids(scope, user_id)
    summary = get_month_summary(user_ids)
    grand_total = sum(item["total"] for item in summary)
    label = _scope_label(scope, user_id)

    with get_connection() as conn:
        budget_rows = conn.execute(
            "SELECT category, monthly_limit FROM budgets WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    budgets = {row["category"]: float(row["monthly_limit"]) for row in budget_rows}

    return {
        "label": label,
        "summary": summary,
        "grand_total": grand_total,
        "budgets": budgets,
        "currency": CURRENCY,
    }


def skill_start_event(user_id: int, user_name: str, params: dict) -> dict:
    """Start an event/trip tag for the WHOLE family.

    When one person starts an event, it is activated for every configured
    family member so both spouses' expenses get auto-tagged.
    """
    tag = params.get("tag", "").strip()
    description = params.get("description", "").strip()
    if not tag:
        return {"success": False, "message": "请提供事件标签名"}

    # Resolve all family member IDs (fallback to current user only)
    member_ids = list(FAMILY_MEMBERS.keys()) if FAMILY_MEMBERS else [user_id]
    if user_id not in member_ids:
        member_ids.append(user_id)

    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    with get_connection() as conn:
        for uid in member_ids:
            # Deactivate all other events first
            conn.execute("UPDATE events SET is_active = 0 WHERE user_id = ?", (uid,))
            conn.execute(
                "INSERT INTO events (user_id, tag, description, is_active, created_at) "
                "VALUES (?, ?, ?, 1, ?) "
                "ON CONFLICT(user_id, tag) DO UPDATE SET is_active = 1, description = ?, created_at = ?",
                (uid, tag, description, now.isoformat(), description, now.isoformat()),
            )
        conn.commit()

    return {"success": True, "message": f"已为全家开启事件标签「{tag}」，后续记账将自动标记", "tag": tag}


def skill_stop_event(user_id: int, user_name: str, params: dict) -> dict:
    """Stop the active event tag for the WHOLE family."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT tag FROM events WHERE user_id = ? AND is_active = 1", (user_id,)
        ).fetchone()
        if not row:
            return {"success": False, "message": "当前没有活跃的事件标签"}
        tag = row["tag"]
        # Deactivate for ALL family members (not just the caller)
        member_ids = list(FAMILY_MEMBERS.keys()) if FAMILY_MEMBERS else [user_id]
        if user_id not in member_ids:
            member_ids.append(user_id)
        placeholders = ",".join("?" for _ in member_ids)
        conn.execute(
            f"UPDATE events SET is_active = 0 WHERE user_id IN ({placeholders})",
            member_ids,
        )
        conn.commit()
    return {"success": True, "message": f"已为全家关闭事件标签「{tag}」", "tag": tag}


def skill_query_event_summary(user_id: int, user_name: str, params: dict) -> dict:
    """Get expense summary for a specific event/trip."""
    tag = params.get("tag", "").strip()
    if not tag:
        return {"success": False, "message": "请提供事件标签名"}

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT user_name, category, SUM(amount_sgd) AS total "
            "FROM expenses WHERE event_tag = ? "
            "GROUP BY user_name, category ORDER BY user_name, total DESC",
            (tag,),
        ).fetchall()
        total_row = conn.execute(
            "SELECT SUM(amount_sgd) AS grand_total FROM expenses WHERE event_tag = ?",
            (tag,),
        ).fetchone()

    if not rows:
        return {"success": True, "message": f"事件「{tag}」暂无记录", "tag": tag}

    per_person: dict[str, list] = {}
    for r in rows:
        name = r["user_name"]
        per_person.setdefault(name, []).append({"category": r["category"], "total": float(r["total"])})

    grand_total = float(total_row["grand_total"]) if total_row["grand_total"] else 0
    per_person_total = {}
    for name, items in per_person.items():
        per_person_total[name] = sum(i["total"] for i in items)

    return {
        "success": True,
        "tag": tag,
        "per_person": per_person,
        "per_person_total": per_person_total,
        "grand_total": grand_total,
        "split_each": round(grand_total / max(len(per_person_total), 1), 2),
        "currency": CURRENCY,
    }


def skill_export_csv(user_id: int, user_name: str, params: dict) -> dict:
    """Export expense data as CSV. Returns CSV content string."""
    scope = params.get("scope", "me")
    event_tag = params.get("event_tag", "")

    target_uid = user_id if scope == "me" else None
    csv_content = export_expenses_csv(user_id=target_uid, event_tag=event_tag)
    line_count = csv_content.count("\n")

    return {
        "success": True,
        "csv_content": csv_content,
        "record_count": line_count,  # minus header
    }


# ═══════════════════════════════════════════
#  Skill registry & function schemas
# ═══════════════════════════════════════════

SKILL_MAP: dict[str, Any] = {
    "record_expense": skill_record_expense,
    "delete_last_expense": skill_delete_last,
    "query_monthly_total": skill_query_monthly_total,
    "query_category_total": skill_query_category_total,
    "query_summary": skill_query_summary,
    "set_budget": skill_set_budget,
    "query_budget": skill_query_budget,
    "get_spending_analysis": skill_get_spending_analysis,
    "start_event": skill_start_event,
    "stop_event": skill_stop_event,
    "query_event_summary": skill_query_event_summary,
    "export_csv": skill_export_csv,
}

TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "record_expense",
            "description": "记录一笔支出。用户说了具体花费时调用。支持多币种和事件标签。",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "支出分类", "enum": CATEGORIES},
                    "amount": {"type": "number", "description": "金额"},
                    "note": {"type": "string", "description": "备注说明"},
                    "currency": {"type": "string", "description": f"货币代码，默认 {CURRENCY}。支持 SGD/CNY/USD/AUD/JPY/MYR/EUR/GBP/THB/KRW 等"},
                    "event_tag": {"type": "string", "description": "事件/旅行标签（留空则自动使用活跃标签）"},
                },
                "required": ["category", "amount", "note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_last_expense",
            "description": "删除用户最近一条支出记录。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_monthly_total",
            "description": "查询本月总支出。",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "description": "查询范围", "enum": ["me", "spouse", "family"]},
                },
                "required": ["scope"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_category_total",
            "description": "查询本月某个分类的支出。",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "支出分类", "enum": CATEGORIES},
                    "scope": {"type": "string", "description": "查询范围", "enum": ["me", "spouse", "family"]},
                },
                "required": ["category", "scope"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_summary",
            "description": "查询本月按分类的支出汇总。",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "description": "查询范围", "enum": ["me", "spouse", "family"]},
                },
                "required": ["scope"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_budget",
            "description": "设置当前用户的每月预算上限（个人维度）。category 为 '_total' 表示个人总预算。",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "预算分类，'_total' 表示个人总预算"},
                    "amount": {"type": "number", "description": "每月预算金额"},
                },
                "required": ["category", "amount"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_budget",
            "description": "查询预算使用情况。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_spending_analysis",
            "description": "获取消费数据用于分析和财务建议。",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "description": "分析范围", "enum": ["me", "spouse", "family"]},
                },
                "required": ["scope"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_event",
            "description": "开启一个事件/旅行标签。开启后所有记账自动附带此标签，方便事后汇总。用户说'开始日本旅行'、'开启XX事件'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "description": "事件标签名，如'日本旅行'、'春节'"},
                    "description": {"type": "string", "description": "事件描述"},
                },
                "required": ["tag"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_event",
            "description": "关闭当前活跃的事件标签。用户说'结束旅行'、'关闭事件'时调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_event_summary",
            "description": "查询某个事件/旅行的花费汇总和AA结算。用户说'日本旅行花了多少'、'XX事件汇总'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "description": "事件标签名"},
                },
                "required": ["tag"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_csv",
            "description": "导出账单为CSV。用户说'导出账单'、'导出数据'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "description": "导出范围", "enum": ["me", "family"]},
                    "event_tag": {"type": "string", "description": "只导出指定事件的数据（留空导出全部）"},
                },
            },
        },
    },
]


# ═══════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════

def _scope_label(scope: str, my_user_id: int) -> str:
    if scope == "me":
        return FAMILY_MEMBERS.get(my_user_id, "我")
    elif scope == "spouse":
        spouse_id = get_spouse_id(my_user_id)
        if spouse_id is not None:
            return get_member_name(spouse_id)
        return "配偶"
    else:
        return "家庭"


def _get_active_event(user_id: int) -> str:
    """Get the currently active event tag for a user."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT tag FROM events WHERE user_id = ? AND is_active = 1",
            (user_id,),
        ).fetchone()
    return row["tag"] if row else ""


def _check_budget_alert(user_id: int, category: str) -> Optional[str]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT monthly_limit FROM budgets WHERE user_id = ? AND category = ?",
            (user_id, category),
        ).fetchone()
        alerts = []
        if row:
            limit_val = float(row["monthly_limit"])
            spent = get_category_total(category, [user_id])
            if spent > limit_val:
                alerts.append(f"⚠️ {category}已超出预算！({spent:.2f}/{limit_val:.2f} {CURRENCY})")
            elif spent > limit_val * 0.8:
                alerts.append(f"⚡ {category}已用预算 {spent/limit_val*100:.0f}%（{spent:.2f}/{limit_val:.2f} {CURRENCY}）")

        row = conn.execute(
            "SELECT monthly_limit FROM budgets WHERE user_id = ? AND category = '_total'",
            (user_id,),
        ).fetchone()
        if row:
            limit_val = float(row["monthly_limit"])
            spent = get_month_total([user_id])
            if spent > limit_val:
                alerts.append(f"⚠️ 个人总支出已超出预算！({spent:.2f}/{limit_val:.2f} {CURRENCY})")
            elif spent > limit_val * 0.8:
                alerts.append(f"⚡ 个人总支出已用预算 {spent/limit_val*100:.0f}%（{spent:.2f}/{limit_val:.2f} {CURRENCY}）")

    return "\n".join(alerts) if alerts else None


def execute_skill(skill_name: str, user_id: int, user_name: str, params: dict) -> dict:
    func = SKILL_MAP.get(skill_name)
    if func is None:
        return {"success": False, "message": f"未知的操作: {skill_name}"}
    try:
        return func(user_id, user_name, params)
    except Exception as e:
        logger.exception("Skill %s failed", skill_name)
        return {"success": False, "message": f"操作失败: {str(e)}"}
