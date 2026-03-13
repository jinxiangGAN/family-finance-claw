"""MiniMax API usage tracking and cost control."""

import logging
from datetime import datetime

from zoneinfo import ZoneInfo

from app.config import MINIMAX_MONTHLY_TOKEN_LIMIT, TIMEZONE
from app.database import get_connection

logger = logging.getLogger(__name__)


def record_usage(
    user_id: int,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    model: str,
) -> None:
    """Record a single API call's token usage."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO api_usage (user_id, prompt_tokens, completion_tokens, total_tokens, model, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, prompt_tokens, completion_tokens, total_tokens, model, datetime.now(ZoneInfo(TIMEZONE)).isoformat()),
        )
        conn.commit()
    logger.debug(
        "API usage recorded: user=%s tokens=%d (prompt=%d, completion=%d)",
        user_id, total_tokens, prompt_tokens, completion_tokens,
    )


def get_monthly_token_usage() -> int:
    """Get total tokens used this month across all users."""
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

    with get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(total_tokens), 0) AS total FROM api_usage "
            "WHERE created_at >= ?",
            (start,),
        ).fetchone()
    return int(row["total"])


def is_within_limit() -> bool:
    """Check if current monthly usage is within the configured limit.

    Returns True if usage is OK (under limit or no limit set).
    """
    if MINIMAX_MONTHLY_TOKEN_LIMIT <= 0:
        return True  # No limit
    used = get_monthly_token_usage()
    return used < MINIMAX_MONTHLY_TOKEN_LIMIT


def get_usage_stats() -> dict:
    """Get usage statistics for the current month."""
    used = get_monthly_token_usage()
    limit = MINIMAX_MONTHLY_TOKEN_LIMIT
    return {
        "monthly_used": used,
        "monthly_limit": limit,
        "remaining": max(0, limit - used) if limit > 0 else -1,
        "usage_pct": (used / limit * 100) if limit > 0 else 0.0,
    }
