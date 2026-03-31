"""Shared Telegram sending bridge for resident workbenches.

This lets resident actions reuse the main bot process transport instead of
opening a second raw HTTP sending path.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from telegram import Bot

_bot: Bot | None = None
_loop: asyncio.AbstractEventLoop | None = None
_lock = threading.Lock()


def register_telegram_sender(bot: Bot, loop: asyncio.AbstractEventLoop) -> None:
    with _lock:
        global _bot, _loop
        _bot = bot
        _loop = loop


def clear_telegram_sender() -> None:
    with _lock:
        global _bot, _loop
        _bot = None
        _loop = None


async def _send_message(chat_id: int, text: str) -> dict[str, Any]:
    if _bot is None:
        raise RuntimeError("telegram sender is not registered")
    message = await _bot.send_message(chat_id=chat_id, text=text)
    return {
        "chat_id": message.chat_id,
        "message_id": message.message_id,
    }


def send_message_via_bot(chat_id: int, text: str, timeout_seconds: float = 15.0) -> dict[str, Any]:
    with _lock:
        bot = _bot
        loop = _loop
    if bot is None or loop is None:
        raise RuntimeError("telegram sender is not registered")
    future = asyncio.run_coroutine_threadsafe(_send_message(chat_id, text), loop)
    return future.result(timeout=timeout_seconds)
