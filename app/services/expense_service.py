"""Service layer for expense CRUD operations."""

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from zoneinfo import ZoneInfo

from app.config import CURRENCY, TIMEZONE
from app.database import get_connection
from app.models.expense import Expense
from app.services.stats_service import get_spouse_id, resolve_user_ids

logger = logging.getLogger(__name__)


def _reset_expense_sequence_if_empty(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT COUNT(*) AS count FROM expenses").fetchone()
    if row is None or int(row["count"]) != 0:
        return
    try:
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'expenses'")
    except sqlite3.OperationalError:
        logger.debug("sqlite_sequence is unavailable; skipping expense id reset")


def save_expense(expense: Expense) -> int:
    """Insert an expense record and return the new row id."""
    sql = """
        INSERT INTO expenses (user_id, user_name, category, amount, currency, amount_sgd, note, event_tag, ledger_type, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    with get_connection() as conn:
        cursor = conn.execute(
            sql,
            (
                expense.user_id,
                expense.user_name,
                expense.category,
                expense.amount,
                expense.currency,
                expense.amount_sgd,
                expense.note,
                expense.event_tag,
                expense.ledger_type,
                expense.created_at,
            ),
        )
        conn.commit()
        row_id = cursor.lastrowid
    logger.info("Saved expense id=%s for user=%s", row_id, expense.user_id)
    return row_id  # type: ignore[return-value]


def delete_last_expense(user_id: int) -> Optional[Expense]:
    """Delete the most recent expense for a user. Returns the deleted record or None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM expenses WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if row is None:
            return None
        expense = _row_to_expense(row)
        conn.execute("DELETE FROM expenses WHERE id = ?", (row["id"],))
        _reset_expense_sequence_if_empty(conn)
        conn.commit()
    logger.info("Deleted expense id=%s for user=%s", expense.id, user_id)
    return expense


def get_expense_by_id(
    expense_id: int,
    allowed_user_ids: Optional[list[int]] = None,
) -> Optional[Expense]:
    """Get a single expense by id, optionally restricted to allowed user ids."""
    conditions = ["id = ?"]
    params: list = [expense_id]

    if allowed_user_ids:
        placeholders = ",".join("?" for _ in allowed_user_ids)
        conditions.append(f"user_id IN ({placeholders})")
        params.extend(allowed_user_ids)

    sql = f"SELECT * FROM expenses WHERE {' AND '.join(conditions)} LIMIT 1"
    with get_connection() as conn:
        row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    return _row_to_expense(row)


def delete_expense_by_id(
    expense_id: int,
    allowed_user_ids: Optional[list[int]] = None,
) -> Optional[Expense]:
    """Delete one expense by id, optionally restricted to allowed user ids."""
    with get_connection() as conn:
        conditions = ["id = ?"]
        params: list = [expense_id]

        if allowed_user_ids:
            placeholders = ",".join("?" for _ in allowed_user_ids)
            conditions.append(f"user_id IN ({placeholders})")
            params.extend(allowed_user_ids)

        sql = f"SELECT * FROM expenses WHERE {' AND '.join(conditions)} LIMIT 1"
        row = conn.execute(sql, params).fetchone()
        if row is None:
            return None
        expense = _row_to_expense(row)
        conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
        _reset_expense_sequence_if_empty(conn)
        conn.commit()
    logger.info("Deleted expense id=%s", expense_id)
    return expense


def get_recent_expenses(user_id: int, limit: int = 10) -> list[Expense]:
    """Get the most recent expenses for a user."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM expenses WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [_row_to_expense(r) for r in rows]


def get_today_total(
    *,
    user_id: int,
    scope: str = "me",
    include_special: bool = False,
) -> dict[str, object]:
    """Get today's total in default currency for me/spouse/family."""
    if scope == "spouse" and get_spouse_id(user_id) is None:
        return {"success": False, "message": "未配置配偶账号，无法查询配偶账单"}

    user_ids = resolve_user_ids(scope, user_id)
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)

    conditions = ["datetime(created_at) >= datetime(?)", "datetime(created_at) < datetime(?)"]
    params: list[object] = [start.isoformat(), end.isoformat()]

    if user_ids:
        placeholders = ",".join("?" for _ in user_ids)
        conditions.append(f"user_id IN ({placeholders})")
        params.extend(user_ids)
    if not include_special:
        conditions.append("ledger_type = 'regular'")

    sql = (
        "SELECT COALESCE(SUM(CASE WHEN amount_sgd > 0 THEN amount_sgd ELSE amount END), 0) AS total "
        f"FROM expenses WHERE {' AND '.join(conditions)}"
    )
    with get_connection() as conn:
        row = conn.execute(sql, params).fetchone()
    total = float(row["total"]) if row is not None else 0.0
    return {
        "success": True,
        "scope": scope,
        "total": total,
        "currency": CURRENCY,
        "includes_special": include_special,
        "date": start.date().isoformat(),
    }


def get_expenses(
    user_ids: Optional[list[int]] = None,
    category: str = "",
    event_tag: str = "",
    ledger_type: str = "",
    start: str = "",
    end: str = "",
    limit: int = 20,
) -> list[Expense]:
    """Query expenses with optional user/category/month filters."""
    conditions = []
    params: list = []

    if user_ids:
        placeholders = ",".join("?" for _ in user_ids)
        conditions.append(f"user_id IN ({placeholders})")
        params.extend(user_ids)
    if category:
        conditions.append("category = ?")
        params.append(category)
    if event_tag:
        conditions.append("event_tag = ?")
        params.append(event_tag)
    if ledger_type:
        conditions.append("ledger_type = ?")
        params.append(ledger_type)
    if start:
        conditions.append("datetime(created_at) >= datetime(?)")
        params.append(start)
    if end:
        conditions.append("datetime(created_at) < datetime(?)")
        params.append(end)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = f"SELECT * FROM expenses {where} ORDER BY datetime(created_at) DESC, id DESC LIMIT ?"
    params.append(limit)

    with get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_expense(r) for r in rows]


def export_expenses_csv(user_id: Optional[int] = None, event_tag: str = "") -> str:
    """Export expenses to CSV string. Optionally filter by user_id and/or event_tag."""
    conditions = []
    params: list = []
    if user_id is not None:
        conditions.append("user_id = ?")
        params.append(user_id)
    if event_tag:
        conditions.append("event_tag = ?")
        params.append(event_tag)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = f"SELECT * FROM expenses {where} ORDER BY created_at ASC"

    with get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()

    lines = ["id,user_name,category,amount,currency,amount_sgd,note,event_tag,ledger_type,created_at"]
    for r in rows:
        note_escaped = str(r["note"]).replace('"', '""')
        tag = r["event_tag"] if "event_tag" in r.keys() else ""
        currency = r["currency"] if "currency" in r.keys() else "SGD"
        amount_sgd = r["amount_sgd"] if "amount_sgd" in r.keys() else r["amount"]
        lines.append(
            f'{r["id"]},"{r["user_name"]}","{r["category"]}",{r["amount"]},"{currency}",'
            f'{amount_sgd},"{note_escaped}","{tag}","{r["ledger_type"] if "ledger_type" in r.keys() else "regular"}","{r["created_at"]}"'
        )
    return "\n".join(lines)


def _row_to_expense(row) -> Expense:
    # Handle both old (no currency/event_tag columns) and new schemas
    keys = row.keys() if hasattr(row, "keys") else []
    return Expense(
        id=row["id"],
        user_id=row["user_id"],
        user_name=row["user_name"],
        category=row["category"],
        amount=row["amount"],
        currency=row["currency"] if "currency" in keys else "SGD",
        amount_sgd=row["amount_sgd"] if "amount_sgd" in keys else row["amount"],
        note=row["note"],
        event_tag=row["event_tag"] if "event_tag" in keys else "",
        ledger_type=row["ledger_type"] if "ledger_type" in keys else "regular",
        created_at=row["created_at"],
    )
