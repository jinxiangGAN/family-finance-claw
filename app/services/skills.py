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
from app.services.expense_service import (
    delete_expense_by_id,
    delete_last_expense,
    export_expenses_csv,
    get_expense_by_id,
    get_expenses,
    save_expense,
)
from app.services.stats_service import (
    get_category_total,
    get_member_name,
    get_month_summary,
    get_month_total,
    get_monthly_archive,
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
    ledger_type = str(params.get("ledger_type", "")).strip().lower()

    # If no event tag provided, check for active event
    if not event_tag:
        active_event = _get_active_event(user_id)
        event_tag = active_event.get("tag", "")
        if not ledger_type and event_tag:
            ledger_type = active_event.get("ledger_type", "special")

    if ledger_type not in {"regular", "special"}:
        ledger_type = "special" if event_tag else "regular"

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
        ledger_type=ledger_type,
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
    if ledger_type == "special":
        confirm_parts.append("🧳 口径：专项开销（默认不计入日常预算/常规统计）")
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
    result["ledger_type"] = ledger_type
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


def skill_delete_expense_by_id(user_id: int, user_name: str, params: dict) -> dict:
    """Delete one expense by id within the family scope."""
    expense_id = int(params.get("expense_id", 0))
    if expense_id <= 0:
        return {"success": False, "message": "请提供有效的账目 ID"}

    allowed_user_ids = _family_user_ids(user_id)
    expense = get_expense_by_id(expense_id, allowed_user_ids=allowed_user_ids)
    if expense is None:
        return {"success": False, "message": f"没有找到可删除的账目 #{expense_id}"}

    deleted = delete_expense_by_id(expense_id, allowed_user_ids=allowed_user_ids)
    if deleted is None:
        return {"success": False, "message": f"删除账目 #{expense_id} 失败，请稍后重试"}

    confirmation = (
        f"🗑 已删除账目 #{deleted.id}：{deleted.user_name} / {deleted.category} "
        f"{deleted.amount:.2f} {deleted.currency}"
    )
    if deleted.note:
        confirmation += f" / 备注：{deleted.note}"
    return {
        "success": True,
        "confirmation": confirmation,
        "deleted": {
            "id": deleted.id,
            "user_id": deleted.user_id,
            "user_name": deleted.user_name,
            "category": deleted.category,
            "amount": deleted.amount,
            "currency": deleted.currency,
            "note": deleted.note,
            "created_at": deleted.created_at,
        },
    }


def skill_query_monthly_total(user_id: int, user_name: str, params: dict) -> dict:
    """Query monthly total spending."""
    scope = params.get("scope", "me")
    include_special = bool(params.get("include_special", False))
    if scope == "spouse" and get_spouse_id(user_id) is None:
        return {"success": False, "message": "未配置配偶账号，无法查询配偶账单"}
    user_ids = resolve_user_ids(scope, user_id)
    total = get_month_total(user_ids, include_special=include_special)
    label = _scope_label(scope, user_id)
    return {
        "success": True,
        "label": label,
        "total": total,
        "currency": CURRENCY,
        "includes_special": include_special,
    }


def skill_query_category_total(user_id: int, user_name: str, params: dict) -> dict:
    """Query spending for a specific category."""
    scope = params.get("scope", "me")
    category = params.get("category", "其他")
    include_special = bool(params.get("include_special", False))
    if scope == "spouse" and get_spouse_id(user_id) is None:
        return {"success": False, "message": "未配置配偶账号，无法查询配偶账单"}
    user_ids = resolve_user_ids(scope, user_id)
    total = get_category_total(category, user_ids, include_special=include_special)
    label = _scope_label(scope, user_id)
    return {
        "success": True,
        "label": label,
        "category": category,
        "total": total,
        "currency": CURRENCY,
        "includes_special": include_special,
    }


def skill_query_summary(user_id: int, user_name: str, params: dict) -> dict:
    """Query monthly summary by category."""
    scope = params.get("scope", "me")
    include_special = bool(params.get("include_special", False))
    if scope == "spouse" and get_spouse_id(user_id) is None:
        return {"success": False, "message": "未配置配偶账号，无法查询配偶账单"}
    user_ids = resolve_user_ids(scope, user_id)
    summary = get_month_summary(user_ids, include_special=include_special)
    grand_total = sum(item["total"] for item in summary)
    label = _scope_label(scope, user_id)
    return {
        "success": True,
        "label": label,
        "summary": summary,
        "grand_total": grand_total,
        "currency": CURRENCY,
        "includes_special": include_special,
    }


def skill_query_category_items(user_id: int, user_name: str, params: dict) -> dict:
    """Query itemized expenses for a category in the current month."""
    scope = params.get("scope", "me")
    category = params.get("category", "其他")
    limit = min(max(int(params.get("limit", 20)), 1), 100)
    include_special = bool(params.get("include_special", False))
    if scope == "spouse" and get_spouse_id(user_id) is None:
        return {"success": False, "message": "未配置配偶账号，无法查询配偶账单"}

    user_ids = resolve_user_ids(scope, user_id)
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if now.month == 12:
        end = start.replace(year=now.year + 1, month=1)
    else:
        end = start.replace(month=now.month + 1)

    expenses = get_expenses(
        user_ids=user_ids,
        category=category,
        ledger_type="" if include_special else "regular",
        start=start.isoformat(),
        end=end.isoformat(),
        limit=limit,
    )
    label = _scope_label(scope, user_id)

    items = [
        {
            "id": expense.id,
            "user_name": expense.user_name,
            "amount": expense.amount,
            "currency": expense.currency,
            "amount_sgd": expense.amount_sgd if expense.amount_sgd > 0 else expense.amount,
            "note": expense.note,
            "event_tag": expense.event_tag,
            "ledger_type": expense.ledger_type,
            "created_at": expense.created_at,
        }
        for expense in expenses
    ]

    return {
        "success": True,
        "label": label,
        "category": category,
        "items": items,
        "count": len(items),
        "currency": CURRENCY,
        "includes_special": include_special,
    }


def skill_query_recent_expenses(user_id: int, user_name: str, params: dict) -> dict:
    """Query recent expense records across me/spouse/family."""
    scope = params.get("scope", "me")
    category = params.get("category", "").strip()
    limit = min(max(int(params.get("limit", 10)), 1), 30)
    ledger_type = str(params.get("ledger_type", "")).strip().lower()
    if scope == "spouse" and get_spouse_id(user_id) is None:
        return {"success": False, "message": "未配置配偶账号，无法查询配偶账单"}

    if ledger_type not in {"", "regular", "special"}:
        return {"success": False, "message": "ledger_type 仅支持 regular 或 special"}

    user_ids = resolve_user_ids(scope, user_id)
    expenses = get_expenses(
        user_ids=user_ids,
        category=category,
        ledger_type=ledger_type,
        limit=limit,
    )
    label = _scope_label(scope, user_id)

    items = [
        {
            "id": expense.id,
            "user_id": expense.user_id,
            "user_name": expense.user_name,
            "category": expense.category,
            "amount": expense.amount,
            "currency": expense.currency,
            "amount_sgd": expense.amount_sgd if expense.amount_sgd > 0 else expense.amount,
            "note": expense.note,
            "event_tag": expense.event_tag,
            "ledger_type": expense.ledger_type,
            "created_at": expense.created_at,
        }
        for expense in expenses
    ]

    return {
        "success": True,
        "label": label,
        "category": category or None,
        "ledger_type": ledger_type or None,
        "items": items,
        "count": len(items),
        "currency": CURRENCY,
    }


def skill_set_budget(user_id: int, user_name: str, params: dict) -> dict:
    """Set a family-shared monthly budget for a category or total.

    Budgets are stored with user_id=0 (family-shared).
    Spending is tracked as the sum of ALL family members.
    """
    category = params.get("category", "_total")
    amount = float(params.get("amount", 0))
    note = params.get("note", "").strip()
    if amount <= 0:
        return {"success": False, "message": "预算金额必须大于0"}

    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT monthly_limit FROM budgets WHERE user_id = 0 AND category = ?",
            (category,),
        ).fetchone()
        old_limit = float(existing["monthly_limit"]) if existing else None

        # user_id=0 → family-shared budget
        conn.execute(
            "INSERT INTO budgets (user_id, category, monthly_limit, updated_at) "
            "VALUES (0, ?, ?, ?) "
            "ON CONFLICT(user_id, category) DO UPDATE SET monthly_limit = ?, updated_at = ?",
            (category, amount, now.isoformat(), amount, now.isoformat()),
        )
        conn.execute(
            "INSERT INTO budget_changes (budget_user_id, category, old_limit, new_limit, changed_by_id, changed_by_name, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (0, category, old_limit, amount, user_id, user_name, note, now.isoformat()),
        )
        conn.commit()

    cat_label = "家庭总预算" if category == "_total" else f"家庭{category}预算"
    if old_limit is None:
        change_summary = f"新建预算：{amount:.2f} {CURRENCY}/月"
    else:
        change_summary = f"由 {old_limit:.2f} 调整为 {amount:.2f} {CURRENCY}/月"
    return {
        "success": True,
        "message": f"已设置{cat_label}：{change_summary}",
        "category": category,
        "monthly_limit": amount,
        "old_monthly_limit": old_limit,
        "changed_by": user_name,
        "currency": CURRENCY,
    }


def skill_query_budget(user_id: int, user_name: str, params: dict) -> dict:
    """Query family-shared budget status.

    Budgets are stored with user_id=0.
    Spending is the sum of ALL family members (scope=family).
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT category, monthly_limit FROM budgets WHERE user_id = 0",
        ).fetchall()
        change_rows = conn.execute(
            "SELECT category, old_limit, new_limit, changed_by_name, note, created_at "
            "FROM budget_changes WHERE budget_user_id = 0 "
            "ORDER BY datetime(created_at) DESC, id DESC LIMIT 5",
        ).fetchall()

    if not rows:
        return {"success": True, "budgets": [], "message": "尚未设置任何预算"}

    # Family-wide spending (None = all users)
    budgets = []
    for row in rows:
        cat = row["category"]
        limit_val = float(row["monthly_limit"])
        if cat == "_total":
            spent = get_month_total(None)  # None = all family members
            cat_label = "家庭总计"
        else:
            spent = get_category_total(cat, None)
            cat_label = f"家庭{cat}"
        remaining = limit_val - spent
        budgets.append({
            "category": cat_label,
            "monthly_limit": limit_val,
            "spent": spent,
            "remaining": remaining,
            "over_budget": remaining < 0,
        })

    recent_changes = [
        {
            "category": "家庭总预算" if row["category"] == "_total" else f"家庭{row['category']}预算",
            "old_limit": float(row["old_limit"]) if row["old_limit"] is not None else None,
            "new_limit": float(row["new_limit"]),
            "changed_by_name": row["changed_by_name"],
            "note": row["note"],
            "created_at": row["created_at"],
        }
        for row in change_rows
    ]

    return {
        "success": True,
        "budgets": budgets,
        "recent_changes": recent_changes,
        "currency": CURRENCY,
    }


def skill_query_budget_changes(user_id: int, user_name: str, params: dict) -> dict:
    """Query recent family budget changes."""
    limit = min(max(int(params.get("limit", 10)), 1), 30)
    category = params.get("category", "").strip()

    sql = (
        "SELECT category, old_limit, new_limit, changed_by_id, changed_by_name, note, created_at "
        "FROM budget_changes WHERE budget_user_id = 0"
    )
    sql_params: list[Any] = []
    if category:
        sql += " AND category = ?"
        sql_params.append(category)
    sql += " ORDER BY datetime(created_at) DESC, id DESC LIMIT ?"
    sql_params.append(limit)

    with get_connection() as conn:
        rows = conn.execute(sql, sql_params).fetchall()

    changes = [
        {
            "category": row["category"],
            "category_label": "家庭总预算" if row["category"] == "_total" else f"家庭{row['category']}预算",
            "old_limit": float(row["old_limit"]) if row["old_limit"] is not None else None,
            "new_limit": float(row["new_limit"]),
            "changed_by_id": row["changed_by_id"],
            "changed_by_name": row["changed_by_name"],
            "note": row["note"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]

    return {
        "success": True,
        "changes": changes,
        "count": len(changes),
        "currency": CURRENCY,
    }


def skill_get_spending_analysis(user_id: int, user_name: str, params: dict) -> dict:
    """Get raw spending data for LLM analysis."""
    scope = params.get("scope", "me")
    include_special = bool(params.get("include_special", False))
    if scope == "spouse" and get_spouse_id(user_id) is None:
        return {"success": False, "message": "未配置配偶账号，无法查询配偶账单"}
    user_ids = resolve_user_ids(scope, user_id)
    summary = get_month_summary(user_ids, include_special=include_special)
    grand_total = sum(item["total"] for item in summary)
    label = _scope_label(scope, user_id)

    with get_connection() as conn:
        budget_rows = conn.execute(
            "SELECT category, monthly_limit FROM budgets WHERE user_id = ?",
            (0,),
        ).fetchall()
    budgets = {row["category"]: float(row["monthly_limit"]) for row in budget_rows}

    return {
        "success": True,
        "label": label,
        "summary": summary,
        "grand_total": grand_total,
        "budgets": budgets,
        "currency": CURRENCY,
        "includes_special": include_special,
    }


def skill_start_event(user_id: int, user_name: str, params: dict) -> dict:
    """Create or activate an event/trip plan for the whole family."""
    tag = params.get("tag", "").strip()
    description = params.get("description", "").strip()
    status = str(params.get("status", "planning")).strip().lower() or "planning"
    activate = bool(params.get("activate", status == "active"))
    if not tag:
        return {"success": False, "message": "请提供事件标签名"}
    if status not in {"planning", "active", "closed"}:
        return {"success": False, "message": "status 仅支持 planning、active、closed"}

    member_ids = list(FAMILY_MEMBERS.keys()) if FAMILY_MEMBERS else [user_id]
    if user_id not in member_ids:
        member_ids.append(user_id)

    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    with get_connection() as conn:
        for uid in member_ids:
            if activate:
                conn.execute("UPDATE events SET is_active = 0 WHERE user_id = ?", (uid,))
            conn.execute(
                "INSERT INTO events (user_id, tag, description, is_active, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, tag) DO UPDATE SET "
                "is_active = excluded.is_active, description = excluded.description, "
                "status = excluded.status, created_at = excluded.created_at",
                (uid, tag, description, 1 if activate else 0, status, now.isoformat()),
            )
        conn.commit()

    if activate:
        message = f"已为全家开启专项计划「{tag}」并设为当前活跃事件，后续相关记账会默认记入专项。"
    else:
        message = f"已创建全家专项计划「{tag}」，当前状态：{status}。需要时可单独把机票、签证等记到这个计划下。"
    return {
        "success": True,
        "message": message,
        "tag": tag,
        "status": status,
        "is_active": activate,
    }


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
            f"UPDATE events SET is_active = 0, status = 'closed' WHERE user_id IN ({placeholders})",
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
        event_row = conn.execute(
            "SELECT status, description, MAX(created_at) AS created_at "
            "FROM events WHERE tag = ? GROUP BY tag",
            (tag,),
        ).fetchone()
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
        "status": event_row["status"] if event_row else None,
        "description": event_row["description"] if event_row else "",
        "event_created_at": event_row["created_at"] if event_row else None,
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


def skill_query_monthly_archive(user_id: int, user_name: str, params: dict) -> dict:
    """查询已归档的历史月度账单。"""
    year = int(params.get("year", 0))
    month = int(params.get("month", 0))
    if not (1 <= month <= 12) or year < 2020:
        return {"success": False, "message": "请提供有效的年月，如 year=2026, month=2"}

    scope = params.get("scope", "family")
    if scope == "me":
        uid = user_id
        label = get_member_name(user_id)
    elif scope == "spouse":
        sid = get_spouse_id(user_id)
        if sid is None:
            return {"success": False, "message": "未配置配偶账号，无法查询配偶账单"}
        uid = sid
        label = get_member_name(uid)
    else:
        uid = None  # family → user_id=0 in get_monthly_archive
        label = "家庭"

    rows = get_monthly_archive(year, month, user_id=uid)
    if not rows:
        return {
            "success": True,
            "message": f"{year}年{month}月暂无归档数据。可能还未到归档时间，或该月没有记录。",
            "summary": [],
            "grand_total": 0,
        }

    grand_total = sum(r["total"] for r in rows)
    currency = rows[0]["currency"] if rows else CURRENCY

    return {
        "success": True,
        "label": f"{label} {year}年{month}月",
        "summary": rows,
        "grand_total": round(grand_total, 2),
        "currency": currency,
    }


# ═══════════════════════════════════════════
#  Skill registry & function schemas
# ═══════════════════════════════════════════

SKILL_MAP: dict[str, Any] = {
    "record_expense": skill_record_expense,
    "delete_last_expense": skill_delete_last,
    "delete_expense_by_id": skill_delete_expense_by_id,
    "query_monthly_total": skill_query_monthly_total,
    "query_category_total": skill_query_category_total,
    "query_category_items": skill_query_category_items,
    "query_recent_expenses": skill_query_recent_expenses,
    "query_summary": skill_query_summary,
    "set_budget": skill_set_budget,
    "query_budget": skill_query_budget,
    "query_budget_changes": skill_query_budget_changes,
    "get_spending_analysis": skill_get_spending_analysis,
    "start_event": skill_start_event,
    "stop_event": skill_stop_event,
    "query_event_summary": skill_query_event_summary,
    "export_csv": skill_export_csv,
    "query_monthly_archive": skill_query_monthly_archive,
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
                    "ledger_type": {"type": "string", "description": "口径：regular=日常开销，special=专项开销（默认有 event_tag 时记为专项）", "enum": ["regular", "special"]},
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
            "name": "delete_expense_by_id",
            "description": "按账目 ID 删除一条指定记录。适合先看最近账目，再明确删除 #123 这种场景。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expense_id": {"type": "integer", "description": "要删除的账目 ID"},
                },
                "required": ["expense_id"],
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
            "name": "query_category_items",
            "description": "查询本月某个分类下的逐笔消费明细。用户说'餐饮明细'、'看看交通每一笔'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "支出分类", "enum": CATEGORIES},
                    "scope": {"type": "string", "description": "查询范围", "enum": ["me", "spouse", "family"]},
                    "limit": {"type": "integer", "description": "最多返回多少条，默认20"},
                    "include_special": {"type": "boolean", "description": "是否包含专项开销，默认 false"},
                },
                "required": ["category", "scope"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_recent_expenses",
            "description": "查询最近几笔账目，适合用户说'最近花了什么'、'给我看最近10笔'、'我想确认删哪条'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "description": "查询范围", "enum": ["me", "spouse", "family"]},
                    "category": {"type": "string", "description": "可选：只看某个分类", "enum": ["", *CATEGORIES]},
                    "limit": {"type": "integer", "description": "最多返回多少条，默认10，最大30"},
                    "ledger_type": {"type": "string", "description": "可选：regular 只看日常，special 只看专项"},
                },
                "required": ["scope"],
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
                    "include_special": {"type": "boolean", "description": "是否包含专项开销，默认 false"},
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
            "name": "query_budget_changes",
            "description": "查询最近的预算调整历史，适合用户说'预算改过什么'、'房租预算什么时候改的'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "可选：只看某个预算分类，'_total' 表示总预算"},
                    "limit": {"type": "integer", "description": "最多返回多少条，默认10，最大30"},
                },
            },
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
                    "include_special": {"type": "boolean", "description": "是否包含专项开销，默认 false"},
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
                    "status": {"type": "string", "description": "专项阶段：planning/active/closed。planning 表示计划期，active 表示进行中。", "enum": ["planning", "active", "closed"]},
                    "activate": {"type": "boolean", "description": "是否设为当前活跃专项。planning 阶段默认 false，active 阶段默认 true。"},
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


def _family_user_ids(my_user_id: int) -> list[int]:
    member_ids = list(FAMILY_MEMBERS.keys())
    if not member_ids:
        return [my_user_id]
    if my_user_id not in member_ids:
        member_ids.append(my_user_id)
    return member_ids


def _get_active_event(user_id: int) -> dict[str, str]:
    """Get the currently active event metadata for a user."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT tag, status FROM events WHERE user_id = ? AND is_active = 1",
            (user_id,),
        ).fetchone()
    if not row:
        return {}
    return {
        "tag": row["tag"],
        "status": row["status"],
        "ledger_type": "special",
    }


def _check_budget_alert(user_id: int, category: str) -> Optional[str]:
    """Check family-shared budgets (user_id=0) against family-wide spending."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT monthly_limit FROM budgets WHERE user_id = 0 AND category = ?",
            (category,),
        ).fetchone()
        alerts = []
        if row:
            limit_val = float(row["monthly_limit"])
            spent = get_category_total(category, None)  # family total
            if spent > limit_val:
                alerts.append(f"⚠️ 家庭{category}已超出预算！({spent:.2f}/{limit_val:.2f} {CURRENCY})")
            elif spent > limit_val * 0.8:
                alerts.append(f"⚡ 家庭{category}已用预算 {spent/limit_val*100:.0f}%（{spent:.2f}/{limit_val:.2f} {CURRENCY}）")

        row = conn.execute(
            "SELECT monthly_limit FROM budgets WHERE user_id = 0 AND category = '_total'",
        ).fetchone()
        if row:
            limit_val = float(row["monthly_limit"])
            spent = get_month_total(None)  # family total
            if spent > limit_val:
                alerts.append(f"⚠️ 家庭总支出已超出预算！({spent:.2f}/{limit_val:.2f} {CURRENCY})")
            elif spent > limit_val * 0.8:
                alerts.append(f"⚡ 家庭总支出已用预算 {spent/limit_val*100:.0f}%（{spent:.2f}/{limit_val:.2f} {CURRENCY}）")

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
