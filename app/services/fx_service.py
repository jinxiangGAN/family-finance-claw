"""Live FX lookup with SQLite cache and static fallback rates."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import httpx

from zoneinfo import ZoneInfo

from app.config import CURRENCY, FX_API_BASE_URL, FX_CACHE_TTL_SECONDS, TIMEZONE
from app.database import get_connection

logger = logging.getLogger(__name__)

REFERENCE_SGD_RATES: dict[str, float] = {
    "SGD": 1.0,
    "CNY": 0.19,
    "RMB": 0.19,
    "USD": 1.35,
    "AUD": 0.88,
    "JPY": 0.009,
    "MYR": 0.30,
    "EUR": 1.45,
    "GBP": 1.70,
    "THB": 0.038,
    "KRW": 0.001,
}

_CURRENCY_ALIASES: dict[str, str] = {
    "SGD": "SGD",
    "新币": "SGD",
    "新加坡元": "SGD",
    "新加坡币": "SGD",
    "CNY": "CNY",
    "RMB": "CNY",
    "人民币": "CNY",
    "USD": "USD",
    "美元": "USD",
    "AUD": "AUD",
    "澳元": "AUD",
    "JPY": "JPY",
    "日元": "JPY",
    "MYR": "MYR",
    "马币": "MYR",
    "令吉": "MYR",
    "EUR": "EUR",
    "欧元": "EUR",
    "GBP": "GBP",
    "英镑": "GBP",
    "THB": "THB",
    "泰铢": "THB",
    "KRW": "KRW",
    "韩元": "KRW",
}


def normalize_currency_code(raw: str | None, *, default: str = CURRENCY) -> str:
    """Normalize user-facing currency aliases into ISO-like codes."""
    if raw is None:
        return default.upper()
    text = raw.strip()
    if not text:
        return default.upper()
    upper = text.upper()
    if upper in {"元", "块"}:
        return default.upper()
    return _CURRENCY_ALIASES.get(text, _CURRENCY_ALIASES.get(upper, upper))


def _parse_timestamp(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _now_iso() -> str:
    return datetime.now(ZoneInfo(TIMEZONE)).isoformat()


def _cache_row(base_currency: str, quote_currency: str) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT base_currency, quote_currency, rate, effective_date, source, fetched_at
            FROM fx_rates
            WHERE base_currency = ? AND quote_currency = ?
            """,
            (base_currency, quote_currency),
        ).fetchone()
    if row is None:
        return None
    return {
        "base_currency": row["base_currency"],
        "quote_currency": row["quote_currency"],
        "rate": float(row["rate"]),
        "effective_date": row["effective_date"] or "",
        "source": row["source"] or "cache",
        "fetched_at": row["fetched_at"] or "",
    }


def _upsert_cache(
    *,
    base_currency: str,
    quote_currency: str,
    rate: float,
    effective_date: str,
    source: str,
) -> None:
    fetched_at = _now_iso()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO fx_rates
                (base_currency, quote_currency, rate, effective_date, source, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(base_currency, quote_currency) DO UPDATE SET
                rate = excluded.rate,
                effective_date = excluded.effective_date,
                source = excluded.source,
                fetched_at = excluded.fetched_at
            """,
            (base_currency, quote_currency, rate, effective_date, source, fetched_at),
        )
        conn.commit()


def _is_cache_fresh(cached: dict[str, Any]) -> bool:
    fetched_at = _parse_timestamp(str(cached.get("fetched_at") or ""))
    if fetched_at is None:
        return False
    return fetched_at >= datetime.now(fetched_at.tzinfo) - timedelta(seconds=FX_CACHE_TTL_SECONDS)


def _reference_rate(base_currency: str, quote_currency: str) -> float | None:
    base_rate = REFERENCE_SGD_RATES.get(base_currency)
    quote_rate = REFERENCE_SGD_RATES.get(quote_currency)
    if base_rate is None or quote_rate is None:
        return None
    return round(base_rate / quote_rate, 6)


def _live_exchange_rate(base_currency: str, quote_currency: str) -> dict[str, Any]:
    response = httpx.get(
        f"{FX_API_BASE_URL.rstrip('/')}/latest",
        params={"from": base_currency, "to": quote_currency},
        timeout=8.0,
        follow_redirects=True,
    )
    response.raise_for_status()
    payload = response.json()
    rates = payload.get("rates") or {}
    rate = rates.get(quote_currency)
    if rate is None:
        raise ValueError(f"No FX rate returned for {base_currency}->{quote_currency}")
    return {
        "success": True,
        "base_currency": base_currency,
        "quote_currency": quote_currency,
        "rate": float(rate),
        "effective_date": str(payload.get("date") or ""),
        "source": "live",
    }


def get_exchange_rate(
    base_currency: str,
    quote_currency: str,
    *,
    prefer_live: bool = True,
) -> dict[str, Any]:
    """Get an exchange rate with online lookup, cache, and static fallback."""
    base_currency = normalize_currency_code(base_currency)
    quote_currency = normalize_currency_code(quote_currency)

    if base_currency == quote_currency:
        return {
            "success": True,
            "base_currency": base_currency,
            "quote_currency": quote_currency,
            "rate": 1.0,
            "effective_date": datetime.now(ZoneInfo(TIMEZONE)).date().isoformat(),
            "source": "local",
        }

    cached = _cache_row(base_currency, quote_currency)
    if cached and _is_cache_fresh(cached) and not prefer_live:
        return {"success": True, **cached, "source": "cache"}
    if cached and _is_cache_fresh(cached):
        return {"success": True, **cached, "source": "cache"}

    if prefer_live:
        try:
            live = _live_exchange_rate(base_currency, quote_currency)
            _upsert_cache(
                base_currency=base_currency,
                quote_currency=quote_currency,
                rate=float(live["rate"]),
                effective_date=str(live.get("effective_date") or ""),
                source="live",
            )
            return live
        except Exception:
            logger.exception("Live FX lookup failed for %s -> %s", base_currency, quote_currency)

    if cached:
        return {"success": True, **cached, "source": "cache"}

    reference_rate = _reference_rate(base_currency, quote_currency)
    if reference_rate is not None:
        return {
            "success": True,
            "base_currency": base_currency,
            "quote_currency": quote_currency,
            "rate": reference_rate,
            "effective_date": "",
            "source": "reference",
        }

    return {
        "success": False,
        "base_currency": base_currency,
        "quote_currency": quote_currency,
        "message": f"暂时拿不到 {base_currency} -> {quote_currency} 的汇率。",
    }


def convert_amount(amount: float, from_currency: str, to_currency: str = CURRENCY) -> dict[str, Any]:
    """Convert an amount and return conversion metadata."""
    from_currency = normalize_currency_code(from_currency)
    to_currency = normalize_currency_code(to_currency)
    rate_result = get_exchange_rate(from_currency, to_currency)
    if not rate_result.get("success"):
        return {
            "success": False,
            "amount": amount,
            "converted_amount": amount,
            "from_currency": from_currency,
            "to_currency": to_currency,
            "rate": 1.0,
            "source": "fallback",
            "message": str(rate_result.get("message") or "汇率转换失败。"),
        }

    rate = float(rate_result["rate"])
    return {
        "success": True,
        "amount": amount,
        "converted_amount": round(amount * rate, 2),
        "from_currency": from_currency,
        "to_currency": to_currency,
        "rate": rate,
        "effective_date": str(rate_result.get("effective_date") or ""),
        "source": str(rate_result.get("source") or "reference"),
    }


def fx_source_label(source: str) -> str:
    return {
        "live": "实时汇率",
        "cache": "缓存汇率",
        "reference": "参考汇率",
        "local": "同币种",
    }.get(source, "参考汇率")
