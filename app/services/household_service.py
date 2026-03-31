"""Household finance helpers beyond basic bookkeeping."""

from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from zoneinfo import ZoneInfo

from app.config import CURRENCY, FAMILY_MEMBERS, TIMEZONE
from app.database import get_connection
from app.services.fx_service import convert_amount, normalize_currency_code
from app.services.stats_service import get_member_name, get_spouse_id, resolve_user_ids


def _shift_year_month(year: int, month: int, delta: int) -> tuple[int, int]:
    total = year * 12 + (month - 1) + delta
    return total // 12, total % 12 + 1


def month_range_from_offset(offset: int = 0) -> tuple[int, int, str, str]:
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    year, month = _shift_year_month(now.year, now.month, offset)
    start = datetime(year, month, 1, tzinfo=tz)
    next_year, next_month = _shift_year_month(year, month, 1)
    end = datetime(next_year, next_month, 1, tzinfo=tz)
    return year, month, start.isoformat(), end.isoformat()


def month_bounds(year: int, month: int) -> tuple[str, str]:
    tz = ZoneInfo(TIMEZONE)
    start = datetime(year, month, 1, tzinfo=tz)
    next_year, next_month = _shift_year_month(year, month, 1)
    end = datetime(next_year, next_month, 1, tzinfo=tz)
    return start.isoformat(), end.isoformat()


def _family_member_ids(request_user_id: int) -> list[int]:
    member_ids = list(FAMILY_MEMBERS.keys())
    if not member_ids:
        return [request_user_id]
    if request_user_id not in member_ids:
        member_ids.append(request_user_id)
    return member_ids


def _range_total(
    *,
    user_ids: Optional[list[int]],
    start: str,
    end: str,
    include_special: bool = False,
    category: str = "",
    event_tag: str = "",
) -> float:
    sum_expr = "COALESCE(SUM(CASE WHEN amount_sgd > 0 THEN amount_sgd ELSE amount END), 0)"
    conditions = ["datetime(created_at) >= datetime(?)", "datetime(created_at) < datetime(?)"]
    params: list[Any] = [start, end]
    if user_ids:
        placeholders = ",".join("?" for _ in user_ids)
        conditions.append(f"user_id IN ({placeholders})")
        params.extend(user_ids)
    if not include_special:
        conditions.append("ledger_type = 'regular'")
    if category:
        conditions.append("category = ?")
        params.append(category)
    if event_tag:
        conditions.append("event_tag = ?")
        params.append(event_tag)
    sql = f"SELECT {sum_expr} AS total FROM expenses WHERE {' AND '.join(conditions)}"
    with get_connection() as conn:
        row = conn.execute(sql, params).fetchone()
    return float(row["total"]) if row else 0.0


def _range_summary(
    *,
    user_ids: Optional[list[int]],
    start: str,
    end: str,
    include_special: bool = False,
    event_tag: str = "",
) -> list[dict[str, Any]]:
    sum_expr = "COALESCE(SUM(CASE WHEN amount_sgd > 0 THEN amount_sgd ELSE amount END), 0)"
    conditions = ["datetime(created_at) >= datetime(?)", "datetime(created_at) < datetime(?)"]
    params: list[Any] = [start, end]
    if user_ids:
        placeholders = ",".join("?" for _ in user_ids)
        conditions.append(f"user_id IN ({placeholders})")
        params.extend(user_ids)
    if not include_special:
        conditions.append("ledger_type = 'regular'")
    if event_tag:
        conditions.append("event_tag = ?")
        params.append(event_tag)
    sql = (
        f"SELECT category, {sum_expr} AS total FROM expenses "
        f"WHERE {' AND '.join(conditions)} GROUP BY category ORDER BY total DESC"
    )
    with get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [{"category": row["category"], "total": float(row["total"])} for row in rows]


def upsert_recurring_rule(
    *,
    request_user_id: int,
    name: str,
    category: str,
    amount: float,
    currency: str,
    due_day: int,
    match_text: str = "",
    note: str = "",
    shared: bool = True,
) -> dict[str, Any]:
    target_user_id = 0 if shared else request_user_id
    due_day = min(max(int(due_day), 1), 31)
    currency = normalize_currency_code(currency)
    now = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO recurring_rules
                (user_id, name, category, amount, currency, due_day, match_text, note, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(user_id, name) DO UPDATE SET
                category = excluded.category,
                amount = excluded.amount,
                currency = excluded.currency,
                due_day = excluded.due_day,
                match_text = excluded.match_text,
                note = excluded.note,
                is_active = 1,
                updated_at = excluded.updated_at
            """,
            (target_user_id, name, category, amount, currency, due_day, match_text, note, now, now),
        )
        conn.commit()
    return {
        "success": True,
        "scope": "family" if target_user_id == 0 else "personal",
        "user_id": target_user_id,
        "name": name,
        "category": category,
        "amount": amount,
        "currency": currency,
        "due_day": due_day,
        "match_text": match_text,
        "note": note,
    }


def list_recurring_rules(request_user_id: int, *, include_inactive: bool = False) -> list[dict[str, Any]]:
    conditions = ["user_id IN (?, ?)"]
    params: list[Any] = [0, request_user_id]
    if not include_inactive:
        conditions.append("is_active = 1")
    sql = (
        "SELECT id, user_id, name, category, amount, currency, due_day, match_text, note, is_active, updated_at "
        f"FROM recurring_rules WHERE {' AND '.join(conditions)} "
        "ORDER BY user_id ASC, due_day ASC, name ASC"
    )
    with get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {
            "id": row["id"],
            "scope": "family" if row["user_id"] == 0 else "personal",
            "user_id": row["user_id"],
            "name": row["name"],
            "category": row["category"],
            "amount": float(row["amount"]),
            "currency": row["currency"],
            "due_day": int(row["due_day"]),
            "match_text": row["match_text"],
            "note": row["note"],
            "is_active": bool(row["is_active"]),
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def _find_recurring_match(rule: dict[str, Any], request_user_id: int, start: str, end: str) -> dict[str, Any] | None:
    user_ids = _family_member_ids(request_user_id) if rule["user_id"] == 0 else [rule["user_id"]]
    conditions = [
        "datetime(created_at) >= datetime(?)",
        "datetime(created_at) < datetime(?)",
        "category = ?",
        "currency = ?",
        "ABS(amount - ?) < 0.01",
    ]
    params: list[Any] = [start, end, rule["category"], rule["currency"], rule["amount"]]
    placeholders = ",".join("?" for _ in user_ids)
    conditions.append(f"user_id IN ({placeholders})")
    params.extend(user_ids)
    match_text = str(rule.get("match_text") or "").strip()
    if match_text:
        conditions.append("LOWER(note) LIKE ?")
        params.append(f"%{match_text.lower()}%")
    sql = (
        "SELECT id, user_id, user_name, category, amount, currency, note, created_at "
        f"FROM expenses WHERE {' AND '.join(conditions)} "
        "ORDER BY datetime(created_at) DESC, id DESC LIMIT 1"
    )
    with get_connection() as conn:
        row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    return {
        "expense_id": row["id"],
        "user_id": row["user_id"],
        "user_name": row["user_name"],
        "category": row["category"],
        "amount": float(row["amount"]),
        "currency": row["currency"],
        "note": row["note"],
        "created_at": row["created_at"],
    }


def get_recurring_status(request_user_id: int) -> dict[str, Any]:
    year, month, start, end = month_range_from_offset(0)
    today = datetime.now(ZoneInfo(TIMEZONE)).day
    rules = list_recurring_rules(request_user_id)
    items: list[dict[str, Any]] = []
    for rule in rules:
        days_in_month = calendar.monthrange(year, month)[1]
        due_day = min(int(rule["due_day"]), days_in_month)
        match = _find_recurring_match(rule, request_user_id, start, end)
        if match:
            status = "logged"
        elif due_day < today:
            status = "overdue"
        else:
            status = "upcoming"
        items.append({**rule, "status": status, "matched_expense": match})
    return {
        "success": True,
        "year": year,
        "month": month,
        "items": items,
        "count": len(items),
    }


def get_period_comparison(
    *,
    request_user_id: int,
    scope: str,
    category: str = "",
    include_special: bool = False,
) -> dict[str, Any]:
    user_ids = resolve_user_ids(scope, request_user_id)
    current_year, current_month, current_start, current_end = month_range_from_offset(0)
    previous_year, previous_month, previous_start, previous_end = month_range_from_offset(-1)

    current_total = _range_total(
        user_ids=user_ids,
        start=current_start,
        end=current_end,
        include_special=include_special,
        category=category,
    )
    previous_total = _range_total(
        user_ids=user_ids,
        start=previous_start,
        end=previous_end,
        include_special=include_special,
        category=category,
    )
    delta = current_total - previous_total
    delta_pct = 0.0 if previous_total == 0 else (delta / previous_total * 100)

    current_summary = _range_summary(
        user_ids=user_ids,
        start=current_start,
        end=current_end,
        include_special=include_special,
    )
    previous_summary = _range_summary(
        user_ids=user_ids,
        start=previous_start,
        end=previous_end,
        include_special=include_special,
    )
    previous_map = {item["category"]: float(item["total"]) for item in previous_summary}
    current_map = {item["category"]: float(item["total"]) for item in current_summary}
    categories = set(previous_map) | set(current_map)
    category_deltas = [
        {
            "category": cat,
            "current_total": round(current_map.get(cat, 0.0), 2),
            "previous_total": round(previous_map.get(cat, 0.0), 2),
            "delta": round(current_map.get(cat, 0.0) - previous_map.get(cat, 0.0), 2),
        }
        for cat in categories
    ]
    category_deltas.sort(key=lambda item: abs(float(item["delta"])), reverse=True)
    return {
        "success": True,
        "current_period": f"{current_year}-{current_month:02d}",
        "previous_period": f"{previous_year}-{previous_month:02d}",
        "scope": scope,
        "category": category or None,
        "current_total": round(current_total, 2),
        "previous_total": round(previous_total, 2),
        "delta": round(delta, 2),
        "delta_pct": round(delta_pct, 1),
        "currency": CURRENCY,
        "category_deltas": category_deltas[:5],
    }


def record_settlement(
    *,
    from_user_id: int,
    to_user_id: int,
    amount: float,
    currency: str,
    note: str = "",
    event_tag: str = "",
) -> dict[str, Any]:
    currency = normalize_currency_code(currency)
    conversion = convert_amount(amount, currency, CURRENCY)
    if not conversion.get("success"):
        return {
            "success": False,
            "message": str(conversion.get("message") or "结算换算失败。"),
        }
    amount_sgd = float(conversion["converted_amount"])
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO settlement_records
                (from_user_id, to_user_id, amount, currency, amount_sgd, note, event_tag, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                from_user_id,
                to_user_id,
                amount,
                currency,
                amount_sgd,
                note,
                event_tag,
                datetime.now(ZoneInfo(TIMEZONE)).isoformat(),
            ),
        )
        conn.commit()
    return {
        "success": True,
        "settlement_id": cursor.lastrowid,
        "from_user_id": from_user_id,
        "to_user_id": to_user_id,
        "amount": amount,
        "currency": currency,
        "amount_sgd": amount_sgd,
        "event_tag": event_tag,
        "note": note,
    }


def get_balance_status(request_user_id: int, *, event_tag: str = "") -> dict[str, Any]:
    member_ids = _family_member_ids(request_user_id)
    if len(member_ids) < 2:
        return {"success": False, "message": "至少需要两位家庭成员才能计算谁欠谁。"}

    if event_tag:
        start = end = ""
    else:
        _, _, start, end = month_range_from_offset(0)

    paid_totals: dict[int, float] = {uid: 0.0 for uid in member_ids}
    with get_connection() as conn:
        expense_conditions = []
        expense_params: list[Any] = []
        placeholders = ",".join("?" for _ in member_ids)
        expense_conditions.append(f"user_id IN ({placeholders})")
        expense_params.extend(member_ids)
        if event_tag:
            expense_conditions.append("event_tag = ?")
            expense_params.append(event_tag)
        else:
            expense_conditions.append("ledger_type = 'regular'")
            expense_conditions.append("datetime(created_at) >= datetime(?)")
            expense_conditions.append("datetime(created_at) < datetime(?)")
            expense_params.extend([start, end])
        rows = conn.execute(
            "SELECT user_id, COALESCE(SUM(CASE WHEN amount_sgd > 0 THEN amount_sgd ELSE amount END), 0) AS total "
            f"FROM expenses WHERE {' AND '.join(expense_conditions)} GROUP BY user_id",
            expense_params,
        ).fetchall()
        for row in rows:
            paid_totals[int(row["user_id"])] = float(row["total"])

        settlement_conditions = [f"from_user_id IN ({placeholders})", f"to_user_id IN ({placeholders})"]
        settlement_params: list[Any] = [*member_ids, *member_ids]
        if event_tag:
            settlement_conditions.append("event_tag = ?")
            settlement_params.append(event_tag)
        else:
            settlement_conditions.append("datetime(created_at) >= datetime(?)")
            settlement_conditions.append("datetime(created_at) < datetime(?)")
            settlement_params.extend([start, end])
        settlement_rows = conn.execute(
            "SELECT from_user_id, to_user_id, amount_sgd, note, created_at FROM settlement_records "
            f"WHERE {' AND '.join(settlement_conditions)}",
            settlement_params,
        ).fetchall()

    total_paid = sum(paid_totals.values())
    split_each = total_paid / len(member_ids) if member_ids else 0.0
    net: dict[int, float] = {uid: round(paid_totals[uid] - split_each, 2) for uid in member_ids}
    settlements: list[dict[str, Any]] = []
    for row in settlement_rows:
        amount_sgd = float(row["amount_sgd"])
        from_uid = int(row["from_user_id"])
        to_uid = int(row["to_user_id"])
        net[from_uid] -= amount_sgd
        net[to_uid] += amount_sgd
        settlements.append(
            {
                "from_user_id": from_uid,
                "to_user_id": to_uid,
                "amount_sgd": amount_sgd,
                "note": row["note"],
                "created_at": row["created_at"],
            }
        )

    creditors = [[uid, amount] for uid, amount in net.items() if amount > 0.01]
    debtors = [[uid, -amount] for uid, amount in net.items() if amount < -0.01]
    suggestions: list[dict[str, Any]] = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        debtor_uid, debtor_amount = debtors[i]
        creditor_uid, creditor_amount = creditors[j]
        transfer = round(min(debtor_amount, creditor_amount), 2)
        suggestions.append(
            {
                "from_user_id": debtor_uid,
                "from_user_name": get_member_name(debtor_uid),
                "to_user_id": creditor_uid,
                "to_user_name": get_member_name(creditor_uid),
                "amount": transfer,
                "currency": CURRENCY,
            }
        )
        debtors[i][1] = round(debtor_amount - transfer, 2)
        creditors[j][1] = round(creditor_amount - transfer, 2)
        if debtors[i][1] <= 0.01:
            i += 1
        if creditors[j][1] <= 0.01:
            j += 1

    return {
        "success": True,
        "event_tag": event_tag or "",
        "total_paid": round(total_paid, 2),
        "split_each": round(split_each, 2),
        "currency": CURRENCY,
        "paid_totals": [
            {"user_id": uid, "user_name": get_member_name(uid), "paid": round(total, 2), "net": round(net[uid], 2)}
            for uid, total in paid_totals.items()
        ],
        "settlements": settlements,
        "suggested_transfers": suggestions,
    }


def get_spending_anomalies(
    *,
    request_user_id: int,
    scope: str,
    include_special: bool = False,
) -> dict[str, Any]:
    user_ids = resolve_user_ids(scope, request_user_id)
    current_year, current_month, current_start, current_end = month_range_from_offset(0)
    current_total = _range_total(
        user_ids=user_ids,
        start=current_start,
        end=current_end,
        include_special=include_special,
    )
    previous_totals: list[float] = []
    category_history: dict[str, list[float]] = defaultdict(list)
    for offset in (-1, -2, -3):
        _, _, start, end = month_range_from_offset(offset)
        previous_totals.append(
            _range_total(user_ids=user_ids, start=start, end=end, include_special=include_special)
        )
        for item in _range_summary(user_ids=user_ids, start=start, end=end, include_special=include_special):
            category_history[item["category"]].append(float(item["total"]))
    baseline_total = sum(previous_totals) / len(previous_totals) if previous_totals else 0.0
    anomalies: list[dict[str, Any]] = []
    if baseline_total > 0 and current_total > baseline_total * 1.4 and current_total - baseline_total >= 50:
        anomalies.append(
            {
                "type": "overall_spike",
                "label": "整体开销",
                "current_total": round(current_total, 2),
                "baseline_total": round(baseline_total, 2),
                "delta": round(current_total - baseline_total, 2),
            }
        )

    current_summary = _range_summary(
        user_ids=user_ids,
        start=current_start,
        end=current_end,
        include_special=include_special,
    )
    for item in current_summary:
        category = item["category"]
        current_value = float(item["total"])
        history = category_history.get(category, [])
        if not history:
            continue
        baseline = sum(history) / len(history)
        if baseline > 0 and current_value > baseline * 1.5 and current_value - baseline >= 30:
            anomalies.append(
                {
                    "type": "category_spike",
                    "label": category,
                    "current_total": round(current_value, 2),
                    "baseline_total": round(baseline, 2),
                    "delta": round(current_value - baseline, 2),
                }
            )

    return {
        "success": True,
        "scope": scope,
        "period": f"{current_year}-{current_month:02d}",
        "anomalies": anomalies[:5],
        "currency": CURRENCY,
    }


def upsert_spending_goal(
    *,
    request_user_id: int,
    category: str,
    target_amount: float,
    note: str = "",
    shared: bool = False,
    include_special: bool = False,
) -> dict[str, Any]:
    target_user_id = 0 if shared else request_user_id
    now = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO spending_goals
                (user_id, category, target_amount, currency, period, include_special, note, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'monthly', ?, ?, 1, ?, ?)
            ON CONFLICT(user_id, category, period) DO UPDATE SET
                target_amount = excluded.target_amount,
                currency = excluded.currency,
                include_special = excluded.include_special,
                note = excluded.note,
                is_active = 1,
                updated_at = excluded.updated_at
            """,
            (target_user_id, category, target_amount, CURRENCY, 1 if include_special else 0, note, now, now),
        )
        conn.commit()
    return {
        "success": True,
        "scope": "family" if target_user_id == 0 else "personal",
        "category": category,
        "target_amount": target_amount,
        "currency": CURRENCY,
        "include_special": include_special,
        "note": note,
    }


def get_goal_progress(request_user_id: int, *, year: int | None = None, month: int | None = None) -> dict[str, Any]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, user_id, category, target_amount, currency, period, include_special, note, updated_at
            FROM spending_goals
            WHERE user_id IN (?, 0) AND is_active = 1
            ORDER BY user_id ASC, category ASC
            """,
            (request_user_id,),
        ).fetchall()
    items: list[dict[str, Any]] = []
    if year is not None and month is not None:
        start, end = month_bounds(year, month)
    else:
        _, _, start, end = month_range_from_offset(0)
    for row in rows:
        owner_id = int(row["user_id"])
        user_ids = _family_member_ids(request_user_id) if owner_id == 0 else [owner_id]
        include_special = bool(row["include_special"])
        category = row["category"]
        spent = _range_total(
            user_ids=user_ids,
            start=start,
            end=end,
            include_special=include_special,
            category="" if category == "_total" else category,
        )
        target_amount = float(row["target_amount"])
        remaining = round(target_amount - spent, 2)
        progress_pct = 0.0 if target_amount <= 0 else (spent / target_amount * 100)
        items.append(
            {
                "id": row["id"],
                "scope": "family" if owner_id == 0 else "personal",
                "category": category,
                "target_amount": target_amount,
                "currency": row["currency"],
                "spent": round(spent, 2),
                "remaining": remaining,
                "progress_pct": round(progress_pct, 1),
                "include_special": include_special,
                "note": row["note"],
                "updated_at": row["updated_at"],
                "on_track": spent <= target_amount,
            }
        )
    return {"success": True, "items": items, "count": len(items)}
