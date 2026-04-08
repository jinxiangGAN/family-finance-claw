"""Shared helpers for parsing natural-language period query phrases."""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Optional

from app.config import CATEGORIES

RECENT_DAYS_RE = re.compile(r"(?:最近|近)\s*(?P<days>\d+)\s*天")
DATE_RANGE_RE = re.compile(
    r"(?P<start>\d{4}-\d{1,2}-\d{1,2})\s*(?:到|至|—|–|-|~)\s*(?P<end>\d{4}-\d{1,2}-\d{1,2})"
)
SINGLE_DATE_RE = re.compile(r"(?<!\d)(?P<date>\d{4}-\d{1,2}-\d{1,2})(?!\d)")

_PERIOD_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("今天", "today"),
    ("今日", "today"),
    ("昨天", "yesterday"),
    ("昨日", "yesterday"),
    ("前天", "day_before_yesterday"),
    ("本周", "this_week"),
    ("这周", "this_week"),
    ("这星期", "this_week"),
    ("本星期", "this_week"),
    ("上周", "last_week"),
    ("上星期", "last_week"),
    ("本月", "this_month"),
    ("这个月", "this_month"),
    ("上个月", "last_month"),
    ("上月", "last_month"),
)

_PERIOD_PHRASE_LABELS: tuple[tuple[str, str], ...] = (
    ("今天", "今天"),
    ("今日", "今天"),
    ("昨天", "昨天"),
    ("昨日", "昨天"),
    ("前天", "前天"),
    ("本周", "本周"),
    ("这周", "本周"),
    ("这星期", "本周"),
    ("本星期", "本周"),
    ("上周", "上周"),
    ("上星期", "上周"),
    ("本月", "本月"),
    ("这个月", "本月"),
    ("上个月", "上个月"),
    ("上月", "上个月"),
)


def normalize_date_literal(raw: str) -> str:
    try:
        year_text, month_text, day_text = raw.split("-", 2)
        normalized = date(int(year_text), int(month_text), int(day_text))
    except (TypeError, ValueError):
        return raw
    return normalized.isoformat()


def infer_period_params(text: str, default_period: str) -> tuple[dict[str, Any], bool]:
    range_match = DATE_RANGE_RE.search(text)
    if range_match:
        return ({
            "period": "custom",
            "start_date": normalize_date_literal(range_match.group("start")),
            "end_date": normalize_date_literal(range_match.group("end")),
        }, True)

    single_date_match = SINGLE_DATE_RE.search(text)
    if single_date_match:
        normalized = normalize_date_literal(single_date_match.group("date"))
        return ({
            "period": "custom",
            "start_date": normalized,
            "end_date": normalized,
        }, True)

    recent_days_match = RECENT_DAYS_RE.search(text)
    if recent_days_match:
        return ({
            "period": "recent_days",
            "days": int(recent_days_match.group("days")),
        }, True)

    for token, period in _PERIOD_KEYWORDS:
        if token in text:
            return ({"period": period}, True)

    return ({"period": default_period}, False)


def extract_period_phrase(text: str) -> Optional[str]:
    range_match = DATE_RANGE_RE.search(text)
    if range_match:
        start = normalize_date_literal(range_match.group("start"))
        end = normalize_date_literal(range_match.group("end"))
        return f"{start} 到 {end}"

    single_date_match = SINGLE_DATE_RE.search(text)
    if single_date_match:
        return normalize_date_literal(single_date_match.group("date"))

    recent_days_match = RECENT_DAYS_RE.search(text)
    if recent_days_match:
        return f"最近{int(recent_days_match.group('days'))}天"

    for token, label in _PERIOD_PHRASE_LABELS:
        if token in text:
            return label
    return None


def extract_query_category(text: str) -> str:
    for category in CATEGORIES:
        if category in text:
            return category
    return ""


def is_detail_query_text(text: str) -> bool:
    return any(token in text for token in ("明细", "细则"))
