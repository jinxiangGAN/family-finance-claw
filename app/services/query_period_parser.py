"""Shared helpers for parsing natural-language period query phrases."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Optional

from zoneinfo import ZoneInfo

from app.config import CATEGORIES, TIMEZONE

RECENT_DAYS_RE = re.compile(r"(?:最近|近)\s*(?P<days>\d+)\s*天")
DATE_RANGE_RE = re.compile(
    r"(?P<start>\d{4}-\d{1,2}-\d{1,2})\s*(?:到|至|—|–|-|~)\s*(?P<end>\d{4}-\d{1,2}-\d{1,2})"
)
SINGLE_DATE_RE = re.compile(r"(?<!\d)(?P<date>\d{4}-\d{1,2}-\d{1,2})(?!\d)")
CHINESE_DATE_RANGE_RE = re.compile(
    r"(?P<start>(?:(?:\d{4}年|今年|去年|明年)\s*)?\d{1,2}月\s*\d{1,2}[日号])\s*(?:到|至|—|–|-|~)\s*"
    r"(?P<end>(?:(?:\d{4}年|今年|去年|明年)\s*)?\d{1,2}月\s*\d{1,2}[日号])"
)
ABSOLUTE_CHINESE_DATE_RE = re.compile(r"(?P<year>\d{4})年\s*(?P<month>\d{1,2})月\s*(?P<day>\d{1,2})[日号]")
RELATIVE_CHINESE_DATE_RE = re.compile(r"(?P<relative>今年|去年|明年)\s*(?P<month>\d{1,2})月\s*(?P<day>\d{1,2})[日号]")
BARE_CHINESE_DATE_RE = re.compile(r"(?<!\d)(?P<month>\d{1,2})月\s*(?P<day>\d{1,2})[日号](?!\d)")
ABSOLUTE_MONTH_RE = re.compile(r"(?P<year>\d{4})年\s*(?P<month>\d{1,2})月(?:份)?")
RELATIVE_MONTH_RE = re.compile(r"(?P<relative>今年|去年|明年)\s*(?P<month>\d{1,2})月(?:份)?")
BARE_MONTH_RE = re.compile(r"(?<!\d)(?P<month>\d{1,2})月(?:份)?(?!\d)")

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

_MONTH_PATTERN_CANDIDATES = (
    ABSOLUTE_MONTH_RE,
    RELATIVE_MONTH_RE,
    BARE_MONTH_RE,
)
_DATE_PATTERN_CANDIDATES = (
    ABSOLUTE_CHINESE_DATE_RE,
    RELATIVE_CHINESE_DATE_RE,
    BARE_CHINESE_DATE_RE,
)


def normalize_date_literal(raw: str) -> str:
    try:
        year_text, month_text, day_text = raw.split("-", 2)
        normalized = date(int(year_text), int(month_text), int(day_text))
    except (TypeError, ValueError):
        return raw
    return normalized.isoformat()


def _local_today() -> date:
    return datetime.now(ZoneInfo(TIMEZONE)).date()


def _resolve_relative_year(relative: str, reference_year: int) -> int:
    if relative == "去年":
        return reference_year - 1
    if relative == "明年":
        return reference_year + 1
    return reference_year


def _normalize_cn_date_label(match: re.Match[str]) -> str:
    year = match.groupdict().get("year")
    relative = match.groupdict().get("relative")
    month = int(match.group("month"))
    day = int(match.group("day"))
    prefix = f"{year}年" if year else relative or ""
    return f"{prefix}{month}月{day}日"


def _normalize_cn_month_label(match: re.Match[str]) -> str:
    year = match.groupdict().get("year")
    relative = match.groupdict().get("relative")
    month = int(match.group("month"))
    prefix = f"{year}年" if year else relative or ""
    return f"{prefix}{month}月"


def _parse_cn_date_match(
    match: re.Match[str],
    *,
    today: date,
    default_year: int | None = None,
) -> tuple[date, str] | None:
    try:
        month = int(match.group("month"))
        day = int(match.group("day"))
        year_text = match.groupdict().get("year")
        relative = match.groupdict().get("relative")
        if year_text:
            year = int(year_text)
        elif relative:
            year = _resolve_relative_year(relative, today.year)
        else:
            year = default_year if default_year is not None else today.year
        return date(year, month, day), _normalize_cn_date_label(match)
    except (TypeError, ValueError):
        return None


def _search_cn_single_date(text: str) -> tuple[date, str] | None:
    today = _local_today()
    for pattern in _DATE_PATTERN_CANDIDATES:
        match = pattern.search(text)
        if not match:
            continue
        parsed = _parse_cn_date_match(match, today=today)
        if parsed is not None:
            return parsed
    return None


def _search_cn_date_range(text: str) -> tuple[date, date, str] | None:
    match = CHINESE_DATE_RANGE_RE.search(text)
    if not match:
        return None
    today = _local_today()
    start_info = _parse_cn_date_token(match.group("start"), today=today)
    if start_info is None:
        return None
    start_day, start_label = start_info
    end_info = _parse_cn_date_token(match.group("end"), today=today, default_year=start_day.year)
    if start_info is None or end_info is None:
        return None
    end_day, end_label = end_info
    if end_day < start_day:
        return None
    return start_day, end_day, f"{start_label} 到 {end_label}"


def _parse_cn_date_token(
    raw: str,
    *,
    today: date,
    default_year: int | None = None,
) -> tuple[date, str] | None:
    stripped = raw.strip()
    for pattern in _DATE_PATTERN_CANDIDATES:
        match = pattern.fullmatch(stripped)
        if not match:
            continue
        return _parse_cn_date_match(match, today=today, default_year=default_year)
    return None


def _month_end(year: int, month: int) -> date:
    if month == 12:
        return date(year + 1, 1, 1) - timedelta(days=1)
    return date(year, month + 1, 1) - timedelta(days=1)


def _search_cn_month(text: str) -> tuple[date, date, str] | None:
    today = _local_today()
    for pattern in _MONTH_PATTERN_CANDIDATES:
        match = pattern.search(text)
        if not match:
            continue
        try:
            month = int(match.group("month"))
            year_text = match.groupdict().get("year")
            relative = match.groupdict().get("relative")
            if year_text:
                year = int(year_text)
            elif relative:
                year = _resolve_relative_year(relative, today.year)
            else:
                year = today.year
            start_day = date(year, month, 1)
            end_day = _month_end(year, month)
        except (TypeError, ValueError):
            continue
        return start_day, end_day, _normalize_cn_month_label(match)
    return None


def infer_period_params(text: str, default_period: str) -> tuple[dict[str, Any], bool]:
    range_match = DATE_RANGE_RE.search(text)
    if range_match:
        return ({
            "period": "custom",
            "start_date": normalize_date_literal(range_match.group("start")),
            "end_date": normalize_date_literal(range_match.group("end")),
            "period_label": f"{normalize_date_literal(range_match.group('start'))} 到 {normalize_date_literal(range_match.group('end'))}",
        }, True)

    cn_range = _search_cn_date_range(text)
    if cn_range is not None:
        start_day, end_day, label = cn_range
        return ({
            "period": "custom",
            "start_date": start_day.isoformat(),
            "end_date": end_day.isoformat(),
            "period_label": label,
        }, True)

    single_date_match = SINGLE_DATE_RE.search(text)
    if single_date_match:
        normalized = normalize_date_literal(single_date_match.group("date"))
        return ({
            "period": "custom",
            "start_date": normalized,
            "end_date": normalized,
            "period_label": normalized,
        }, True)

    cn_single_date = _search_cn_single_date(text)
    if cn_single_date is not None:
        target_day, label = cn_single_date
        return ({
            "period": "custom",
            "start_date": target_day.isoformat(),
            "end_date": target_day.isoformat(),
            "period_label": label,
        }, True)

    cn_month = _search_cn_month(text)
    if cn_month is not None:
        start_day, end_day, label = cn_month
        return ({
            "period": "custom",
            "start_date": start_day.isoformat(),
            "end_date": end_day.isoformat(),
            "period_label": label,
        }, True)

    recent_days_match = RECENT_DAYS_RE.search(text)
    if recent_days_match:
        return ({
            "period": "recent_days",
            "days": int(recent_days_match.group("days")),
            "period_label": f"最近{int(recent_days_match.group('days'))}天",
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

    cn_range = _search_cn_date_range(text)
    if cn_range is not None:
        return cn_range[2]

    single_date_match = SINGLE_DATE_RE.search(text)
    if single_date_match:
        return normalize_date_literal(single_date_match.group("date"))

    cn_single_date = _search_cn_single_date(text)
    if cn_single_date is not None:
        return cn_single_date[1]

    cn_month = _search_cn_month(text)
    if cn_month is not None:
        return cn_month[2]

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
