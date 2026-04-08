"""Service layer for expense statistics and queries."""

import json
import logging
from datetime import date, datetime, timedelta
from typing import Optional

from zoneinfo import ZoneInfo

from app.config import CURRENCY, FAMILY_MEMBERS, TIMEZONE
from app.database import get_connection

logger = logging.getLogger(__name__)


def _start_of_day(value: datetime) -> datetime:
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def _month_range() -> tuple[str, str]:
    """Return (start, end) ISO strings for the current month in configured timezone."""
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if now.month == 12:
        end = start.replace(year=now.year + 1, month=1)
    else:
        end = start.replace(month=now.month + 1)
    return start.isoformat(), end.isoformat()


def resolve_period_range(
    *,
    period: str = "this_month",
    start_date: str = "",
    end_date: str = "",
    days: Optional[int] = None,
) -> dict[str, str]:
    """Resolve a named or explicit period into ISO start/end bounds."""
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    normalized_period = str(period or "this_month").strip().lower()

    if start_date or end_date:
        if not start_date or not end_date:
            raise ValueError("start_date and end_date must be provided together")
        try:
            start_day = date.fromisoformat(start_date)
            end_day = date.fromisoformat(end_date)
        except ValueError as exc:
            raise ValueError("start_date/end_date must use YYYY-MM-DD") from exc
        if end_day < start_day:
            raise ValueError("end_date must be on or after start_date")

        start = datetime(start_day.year, start_day.month, start_day.day, tzinfo=tz)
        end_exclusive = end_day + timedelta(days=1)
        end = datetime(end_exclusive.year, end_exclusive.month, end_exclusive.day, tzinfo=tz)
        label = start_date if start_date == end_date else f"{start_date} 到 {end_date}"
        return {
            "period": "custom",
            "label": label,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "start_date": start_day.isoformat(),
            "end_date": end_day.isoformat(),
        }

    today_start = _start_of_day(now)
    if normalized_period == "today":
        start = today_start
        end = start + timedelta(days=1)
        label = "今天"
    elif normalized_period == "yesterday":
        start = today_start - timedelta(days=1)
        end = today_start
        label = "昨天"
    elif normalized_period == "day_before_yesterday":
        start = today_start - timedelta(days=2)
        end = today_start - timedelta(days=1)
        label = "前天"
    elif normalized_period == "this_week":
        start = today_start - timedelta(days=today_start.weekday())
        end = start + timedelta(days=7)
        label = "本周"
    elif normalized_period == "last_week":
        end = today_start - timedelta(days=today_start.weekday())
        start = end - timedelta(days=7)
        label = "上周"
    elif normalized_period == "this_month":
        start = today_start.replace(day=1)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
        label = "本月"
    elif normalized_period == "last_month":
        this_month_start = today_start.replace(day=1)
        end = this_month_start
        last_month_last_day = this_month_start - timedelta(days=1)
        start = last_month_last_day.replace(day=1)
        label = "上个月"
    elif normalized_period == "recent_days":
        window_days = int(days or 0)
        if window_days <= 0:
            raise ValueError("days must be greater than 0 when period is recent_days")
        start = today_start - timedelta(days=max(window_days - 1, 0))
        end = now
        label = f"最近{window_days}天"
    else:
        raise ValueError(f"Unsupported period: {period}")

    return {
        "period": normalized_period,
        "label": label,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "start_date": start.date().isoformat(),
        "end_date": (end - timedelta(microseconds=1)).date().isoformat(),
    }


def _range_summary(
    user_ids: Optional[list[int]] = None,
    include_special: bool = False,
    start: str = "",
    end: str = "",
) -> list[dict]:
    sum_expr = "COALESCE(SUM(CASE WHEN amount_sgd > 0 THEN amount_sgd ELSE amount END), 0)"
    ledger_clause = "" if include_special else "AND ledger_type = 'regular' "
    if user_ids:
        placeholders = ",".join("?" for _ in user_ids)
        sql = (
            f"SELECT category, {sum_expr} AS total FROM expenses "
            f"WHERE user_id IN ({placeholders}) "
            f"{ledger_clause}"
            f"AND datetime(created_at) >= datetime(?) AND datetime(created_at) < datetime(?) "
            f"GROUP BY category ORDER BY total DESC"
        )
        params = [*user_ids, start, end]
    else:
        sql = (
            f"SELECT category, {sum_expr} AS total FROM expenses "
            f"WHERE 1=1 {ledger_clause}"
            "AND datetime(created_at) >= datetime(?) AND datetime(created_at) < datetime(?) "
            "GROUP BY category ORDER BY total DESC"
        )
        params = [start, end]

    with get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [{"category": r["category"], "total": float(r["total"])} for r in rows]


def _range_total(
    user_ids: Optional[list[int]] = None,
    include_special: bool = False,
    start: str = "",
    end: str = "",
) -> float:
    sum_expr = "COALESCE(SUM(CASE WHEN amount_sgd > 0 THEN amount_sgd ELSE amount END), 0)"
    ledger_clause = "" if include_special else "AND ledger_type = 'regular' "
    if user_ids:
        placeholders = ",".join("?" for _ in user_ids)
        sql = (
            f"SELECT {sum_expr} AS total FROM expenses "
            f"WHERE user_id IN ({placeholders}) "
            f"{ledger_clause}"
            f"AND datetime(created_at) >= datetime(?) AND datetime(created_at) < datetime(?)"
        )
        params = [*user_ids, start, end]
    else:
        sql = (
            f"SELECT {sum_expr} AS total FROM expenses "
            f"WHERE 1=1 {ledger_clause}"
            "AND datetime(created_at) >= datetime(?) AND datetime(created_at) < datetime(?)"
        )
        params = [start, end]

    with get_connection() as conn:
        row = conn.execute(sql, params).fetchone()
    return float(row["total"])


def get_range_total(
    *,
    start: str,
    end: str,
    user_ids: Optional[list[int]] = None,
    include_special: bool = False,
    category: str = "",
) -> float:
    """Get total spending for an arbitrary time range."""
    if not category:
        return _range_total(user_ids=user_ids, include_special=include_special, start=start, end=end)

    sum_expr = "COALESCE(SUM(CASE WHEN amount_sgd > 0 THEN amount_sgd ELSE amount END), 0)"
    ledger_clause = "" if include_special else "AND ledger_type = 'regular' "
    if user_ids:
        placeholders = ",".join("?" for _ in user_ids)
        sql = (
            f"SELECT {sum_expr} AS total FROM expenses "
            f"WHERE user_id IN ({placeholders}) AND category = ? "
            f"{ledger_clause}"
            f"AND datetime(created_at) >= datetime(?) AND datetime(created_at) < datetime(?)"
        )
        params = [*user_ids, category, start, end]
    else:
        sql = (
            f"SELECT {sum_expr} AS total FROM expenses "
            f"WHERE category = ? {ledger_clause}"
            "AND datetime(created_at) >= datetime(?) AND datetime(created_at) < datetime(?)"
        )
        params = [category, start, end]

    with get_connection() as conn:
        row = conn.execute(sql, params).fetchone()
    return float(row["total"])


def get_range_summary(
    *,
    start: str,
    end: str,
    user_ids: Optional[list[int]] = None,
    include_special: bool = False,
) -> list[dict]:
    """Get category summary for an arbitrary time range."""
    return _range_summary(user_ids=user_ids, include_special=include_special, start=start, end=end)


def get_spouse_id(my_user_id: int) -> Optional[int]:
    """Return the other family member's user_id, or None if not configured."""
    for uid in FAMILY_MEMBERS:
        if uid != my_user_id:
            return uid
    return None


def get_member_name(user_id: int) -> str:
    """Return the display name for a family member."""
    return FAMILY_MEMBERS.get(user_id, str(user_id))


def resolve_user_ids(scope: str, my_user_id: int) -> Optional[list[int]]:
    """Resolve scope to a list of user_ids.

    - "me"     → [my_user_id]
    - "spouse" → [spouse_id] (or None if unknown)
    - "family" → None (meaning all users, no filter)
    """
    if scope == "me":
        return [my_user_id]
    elif scope == "spouse":
        spouse = get_spouse_id(my_user_id)
        if spouse is not None:
            return [spouse]
        return None
    else:  # "family"
        return None


def get_month_total(user_ids: Optional[list[int]] = None, include_special: bool = False) -> float:
    """Get total expense amount (in default currency) for the current month.

    Uses amount_sgd for multi-currency support, falling back to amount for old data.
    """
    start, end = _month_range()
    return _range_total(user_ids=user_ids, include_special=include_special, start=start, end=end)


def get_category_total(
    category: str,
    user_ids: Optional[list[int]] = None,
    include_special: bool = False,
) -> float:
    """Get total expense amount for a specific category in the current month."""
    start, end = _month_range()
    sum_expr = "COALESCE(SUM(CASE WHEN amount_sgd > 0 THEN amount_sgd ELSE amount END), 0)"
    ledger_clause = "" if include_special else "AND ledger_type = 'regular' "
    if user_ids:
        placeholders = ",".join("?" for _ in user_ids)
        sql = (
            f"SELECT {sum_expr} AS total FROM expenses "
            f"WHERE user_id IN ({placeholders}) AND category = ? "
            f"{ledger_clause}"
            f"AND datetime(created_at) >= datetime(?) AND datetime(created_at) < datetime(?)"
        )
        params = [*user_ids, category, start, end]
    else:
        sql = (
            f"SELECT {sum_expr} AS total FROM expenses "
            f"WHERE category = ? {ledger_clause}"
            "AND datetime(created_at) >= datetime(?) AND datetime(created_at) < datetime(?)"
        )
        params = [category, start, end]

    with get_connection() as conn:
        row = conn.execute(sql, params).fetchone()
    return float(row["total"])


def get_month_summary(user_ids: Optional[list[int]] = None, include_special: bool = False) -> list[dict]:
    """Get per-category summary for the current month.

    Returns a list of {"category": str, "total": float} sorted by total descending.
    """
    start, end = _month_range()
    return _range_summary(user_ids=user_ids, include_special=include_special, start=start, end=end)


def get_last_n_days_total(
    days: int,
    user_ids: Optional[list[int]] = None,
    include_special: bool = False,
) -> float:
    """Get total expense amount for the trailing N-day window."""
    period_info = resolve_period_range(period="recent_days", days=days)
    return _range_total(
        user_ids=user_ids,
        include_special=include_special,
        start=period_info["start"],
        end=period_info["end"],
    )


def get_last_n_days_summary(
    days: int,
    user_ids: Optional[list[int]] = None,
    include_special: bool = False,
) -> list[dict]:
    """Get category summary for the trailing N-day window."""
    period_info = resolve_period_range(period="recent_days", days=days)
    return _range_summary(
        user_ids=user_ids,
        include_special=include_special,
        start=period_info["start"],
        end=period_info["end"],
    )


# ═══════════════════════════════════════════
#  Monthly archive (snapshot)
# ═══════════════════════════════════════════

def _month_range_for(year: int, month: int) -> tuple[str, str]:
    """Return (start, end) ISO strings for a given year/month."""
    tz = ZoneInfo(TIMEZONE)
    start = datetime(year, month, 1, tzinfo=tz)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=tz)
    else:
        end = datetime(year, month + 1, 1, tzinfo=tz)
    return start.isoformat(), end.isoformat()


def archive_month(year: int, month: int) -> int:
    """Archive per-category totals for a given month.

    Stores one row per (user_id, category) PLUS family totals (user_id=0).
    Uses UPSERT so it's safe to run repeatedly.
    Returns the number of rows written.
    """
    start, end = _month_range_for(year, month)
    sum_expr = "COALESCE(SUM(CASE WHEN amount_sgd > 0 THEN amount_sgd ELSE amount END), 0)"

    with get_connection() as conn:
        # ── Per-user breakdown ──
        rows = conn.execute(
            f"SELECT user_id, category, {sum_expr} AS total FROM expenses "
            "WHERE ledger_type = 'regular' "
            "AND datetime(created_at) >= datetime(?) AND datetime(created_at) < datetime(?) "
            "GROUP BY user_id, category",
            (start, end),
        ).fetchall()

        count = 0
        for r in rows:
            conn.execute(
                "INSERT INTO monthly_summaries (year, month, user_id, category, total, currency) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(year, month, user_id, category) DO UPDATE SET total = ?, currency = ?",
                (year, month, r["user_id"], r["category"], float(r["total"]), CURRENCY,
                 float(r["total"]), CURRENCY),
            )
            count += 1

        # ── Family total (user_id=0) ──
        fam_rows = conn.execute(
            f"SELECT category, {sum_expr} AS total FROM expenses "
            "WHERE ledger_type = 'regular' "
            "AND datetime(created_at) >= datetime(?) AND datetime(created_at) < datetime(?) "
            "GROUP BY category",
            (start, end),
        ).fetchall()

        for r in fam_rows:
            conn.execute(
                "INSERT INTO monthly_summaries (year, month, user_id, category, total, currency) "
                "VALUES (?, ?, 0, ?, ?, ?) "
                "ON CONFLICT(year, month, user_id, category) DO UPDATE SET total = ?, currency = ?",
                (year, month, r["category"], float(r["total"]), CURRENCY,
                 float(r["total"]), CURRENCY),
            )
            count += 1

        conn.commit()

    logger.info("Archived %d summary rows for %04d-%02d", count, year, month)
    return count


def get_monthly_archive(
    year: int,
    month: int,
    user_id: Optional[int] = None,
) -> list[dict]:
    """Retrieve archived monthly summary.

    Args:
        year, month: target period.
        user_id: specific user, or None → family (user_id=0).

    Returns list of {"category": str, "total": float, "currency": str}.
    """
    uid = user_id if user_id is not None else 0
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT category, total, currency FROM monthly_summaries "
            "WHERE year = ? AND month = ? AND user_id = ? "
            "ORDER BY total DESC",
            (year, month, uid),
        ).fetchall()
    return [
        {"category": r["category"], "total": float(r["total"]), "currency": r["currency"]}
        for r in rows
    ]


def upsert_monthly_report(
    year: int,
    month: int,
    user_id: int,
    total: float,
    currency: str,
    report_text: str,
    report_payload: dict,
) -> None:
    payload_json = json.dumps(report_payload, ensure_ascii=False)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO monthly_reports
                (year, month, user_id, total, currency, report_text, report_payload, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(year, month, user_id) DO UPDATE SET
                total = excluded.total,
                currency = excluded.currency,
                report_text = excluded.report_text,
                report_payload = excluded.report_payload,
                updated_at = CURRENT_TIMESTAMP
            """,
            (year, month, user_id, total, currency, report_text, payload_json),
        )
        conn.commit()


def get_monthly_report(
    year: int,
    month: int,
    user_id: Optional[int] = None,
) -> Optional[dict]:
    uid = user_id if user_id is not None else 0
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT total, currency, report_text, report_payload, created_at, updated_at
            FROM monthly_reports
            WHERE year = ? AND month = ? AND user_id = ?
            """,
            (year, month, uid),
        ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row["report_payload"] or "{}")
    except json.JSONDecodeError:
        payload = {}
    return {
        "total": float(row["total"]),
        "currency": row["currency"],
        "report_text": row["report_text"],
        "report_payload": payload,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_archived_months() -> list[dict]:
    """List all archived year/month pairs (family totals only)."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT year, month FROM monthly_summaries "
            "WHERE user_id = 0 ORDER BY year DESC, month DESC"
        ).fetchall()
    return [{"year": r["year"], "month": r["month"]} for r in rows]
