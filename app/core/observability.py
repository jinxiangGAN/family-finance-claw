"""Lightweight structured logging helpers for runtime tracing."""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from typing import Any, Iterator


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    payload = {"event": event, **fields}
    logger.info("[OBS] %s", json.dumps(payload, ensure_ascii=False, sort_keys=True))


@contextmanager
def timed_event(logger: logging.Logger, event: str, **fields: Any) -> Iterator[dict[str, Any]]:
    started_at = time.perf_counter()
    context = {"event": event, **fields}
    try:
        yield context
    finally:
        elapsed_ms = round((time.perf_counter() - started_at) * 1000, 1)
        log_event(logger, event, elapsed_ms=elapsed_ms, **fields)
