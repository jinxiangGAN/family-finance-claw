"""Skill definitions: all DB operations as callable functions for the LLM agent.

Each skill function accepts a dict of parameters and returns a dict result.
The SKILL_DEFINITIONS list provides the function-calling schema for MiniMax.
"""

import logging
from datetime import datetime
from typing import Any, Optional

from zoneinfo import ZoneInfo

from app.config import CATEGORIES, CURRENCY, FAMILY_MEMBERS, TIMEZONE
from app.database import get_connection
from app.models.expense import Expense
from app.services.expense_service import delete_last_expense, save_expense
from app.services.stats_service import (
    get_category_total,
    get_member_name,
    get_month_summary,
    get_month_total,
    get_spouse_id,
    resolve_user_ids,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════
#  Skill implementations
# ═══════════════════════════════════════════

def skill_record_expense(user_id: int, user_name: str, params: dict) -> dict:
    """Record one expense."""
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    category = params.get("category", "其他")
    if category not in CATEGORIES:
        category = "其他"
    amount = float(params.get("amount", 0))
    note = params.get("note", "")

    expense = Expense(
        user_id=user_id,
        user_name=user_name,
        category=category,
        amount=amount,
        note=note,
        created_at=now.isoformat(),
    )
    row_id = save_expense(expense)

    # Check budget after recording
    budget_alert = _check_budget_alert(user_id, category)

    return {
        "success": True,
        "id": row_id,
        "category": category,
        "amount": amount,
        "note": note,
        "currency": CURRENCY,
        "budget_alert": budget_alert,
    }


def skill_delete_last(user_id: int, user_name: str, params: dict) -> dict:
    """Delete the most recent expense."""
    deleted = delete_last_expense(user_id)
    if deleted:
        return {
            "success": True,
            "deleted": {
                "category": deleted.category,
                "amount": deleted.amount,
                "note": deleted.note,
            },
            "currency": CURRENCY,
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

    cat_label = "总预算" if category == "_total" else f"{category}预算"
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
        # Get current spending
        if cat == "_total":
            spent = get_month_total([user_id])
            cat_label = "总计"
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

    # Also get budget info
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


# ═══════════════════════════════════════════
#  Skill registry & MiniMax function schemas
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
}

# MiniMax function calling tool definitions
TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "record_expense",
            "description": "记录一笔支出。用户说了具体花费时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "支出分类",
                        "enum": CATEGORIES,
                    },
                    "amount": {
                        "type": "number",
                        "description": "金额",
                    },
                    "note": {
                        "type": "string",
                        "description": "备注说明",
                    },
                },
                "required": ["category", "amount", "note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_last_expense",
            "description": "删除用户最近一条支出记录。用户说'删掉上一条'、'撤销'时调用。",
            "parameters": {
                "type": "object",
                "properties": {},
            },
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
                    "scope": {
                        "type": "string",
                        "description": "查询范围：me=自己，spouse=配偶，family=家庭",
                        "enum": ["me", "spouse", "family"],
                    },
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
                    "category": {
                        "type": "string",
                        "description": "支出分类",
                        "enum": CATEGORIES,
                    },
                    "scope": {
                        "type": "string",
                        "description": "查询范围：me=自己，spouse=配偶，family=家庭",
                        "enum": ["me", "spouse", "family"],
                    },
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
                    "scope": {
                        "type": "string",
                        "description": "查询范围：me=自己，spouse=配偶，family=家庭",
                        "enum": ["me", "spouse", "family"],
                    },
                },
                "required": ["scope"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_budget",
            "description": "设置每月预算上限。category 为 '_total' 表示总预算，否则为分类预算。",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "预算分类，'_total' 表示总预算",
                    },
                    "amount": {
                        "type": "number",
                        "description": "每月预算金额",
                    },
                },
                "required": ["category", "amount"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_budget",
            "description": "查询预算使用情况。用户问'预算还剩多少'、'预算情况'时调用。",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_spending_analysis",
            "description": "获取消费数据用于分析。用户需要消费建议、财务规划、省钱建议时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "description": "分析范围：me=自己，spouse=配偶，family=家庭",
                        "enum": ["me", "spouse", "family"],
                    },
                },
                "required": ["scope"],
            },
        },
    },
]


# ═══════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════

def _scope_label(scope: str, my_user_id: int) -> str:
    """Return a human-readable label for the query scope."""
    if scope == "me":
        name = FAMILY_MEMBERS.get(my_user_id, "我")
        return name
    elif scope == "spouse":
        spouse_id = get_spouse_id(my_user_id)
        if spouse_id is not None:
            return get_member_name(spouse_id)
        return "配偶"
    else:
        return "家庭"


def _check_budget_alert(user_id: int, category: str) -> Optional[str]:
    """Check if spending has exceeded or is near budget. Returns alert message or None."""
    with get_connection() as conn:
        # Check category budget
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

        # Check total budget
        row = conn.execute(
            "SELECT monthly_limit FROM budgets WHERE user_id = ? AND category = '_total'",
            (user_id,),
        ).fetchone()
        if row:
            limit_val = float(row["monthly_limit"])
            spent = get_month_total([user_id])
            if spent > limit_val:
                alerts.append(f"⚠️ 总支出已超出预算！({spent:.2f}/{limit_val:.2f} {CURRENCY})")
            elif spent > limit_val * 0.8:
                alerts.append(f"⚡ 总支出已用预算 {spent/limit_val*100:.0f}%（{spent:.2f}/{limit_val:.2f} {CURRENCY}）")

    return "\n".join(alerts) if alerts else None


def execute_skill(skill_name: str, user_id: int, user_name: str, params: dict) -> dict:
    """Execute a skill by name with given parameters."""
    func = SKILL_MAP.get(skill_name)
    if func is None:
        return {"success": False, "message": f"未知的操作: {skill_name}"}
    try:
        return func(user_id, user_name, params)
    except Exception as e:
        logger.exception("Skill %s failed", skill_name)
        return {"success": False, "message": f"操作失败: {str(e)}"}
