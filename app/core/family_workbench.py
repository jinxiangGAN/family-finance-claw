"""Simple family workbench for latency-sensitive household actions."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Any

from app.core.observability import log_event, timed_event
from app.core.session import get_private_chat_route, remember_private_chat_route
from app.core.telegram_sender import send_message_via_bot, send_message_via_bot_async
from app.services.family_message_parser import parse_forward_message

logger = logging.getLogger(__name__)


def _parse_forward_message(text: str, *, sender_user_id: int) -> dict[str, Any]:
    parsed = parse_forward_message(text, sender_user_id=sender_user_id)
    if parsed is None:
        raise ValueError("Could not parse a family forwarding request.")
    return dict(parsed)


def _render_forward_reply(result: dict[str, Any]) -> str:
    return str(result.get("message") or "小灰毛已经把话带到了。")


def _deliver_forward_message(
    *,
    sender_name: str,
    target_id: int,
    target_name: str,
    body: str,
) -> dict[str, Any]:
    route_chat_id = get_private_chat_route(target_id)
    target_chat_id = route_chat_id if route_chat_id is not None else target_id
    forwarded_text = f"📨 小灰毛帮忙转一句 {sender_name} 的话：\n\n{body}"
    try:
        with timed_event(
            logger,
            "family_workbench.telegram_send",
            target_id=target_id,
            target_name=target_name,
            target_chat_id=target_chat_id,
            route_source="cached_route" if route_chat_id is not None else "direct_user_id",
        ):
            payload = send_message_via_bot(target_chat_id, forwarded_text, timeout_seconds=15.0)
    except Exception as exc:
        return {
            "success": False,
            "message": (
                f"这次小灰毛没能把话带给 {target_name}。"
                f"如果 {target_name} 明明已经和小灰毛聊过，通常是 Telegram 那边没让这次投递落进去，"
                "可以让对方再发一句 /start 或普通消息后再试一次。"
            ),
            "error": str(exc),
            "target_chat_id": target_chat_id,
            "route_source": "cached_route" if route_chat_id is not None else "direct_user_id",
        }
    remember_private_chat_route(target_id, target_chat_id)
    return {
        "success": True,
        "message": f"好呀，小灰毛已经把话带给 {target_name} 了。",
        "target_chat_id": target_chat_id,
        "message_id": payload.get("message_id"),
        "route_source": "cached_route" if route_chat_id is not None else "direct_user_id",
    }


async def _deliver_forward_message_async(
    *,
    sender_name: str,
    target_id: int,
    target_name: str,
    body: str,
) -> dict[str, Any]:
    route_chat_id = get_private_chat_route(target_id)
    target_chat_id = route_chat_id if route_chat_id is not None else target_id
    forwarded_text = f"📨 小灰毛帮忙转一句 {sender_name} 的话：\n\n{body}"
    try:
        with timed_event(
            logger,
            "family_workbench.telegram_send",
            target_id=target_id,
            target_name=target_name,
            target_chat_id=target_chat_id,
            route_source="cached_route" if route_chat_id is not None else "direct_user_id",
        ):
            payload = await send_message_via_bot_async(target_chat_id, forwarded_text)
    except Exception as exc:
        return {
            "success": False,
            "message": (
                f"这次小灰毛没能把话带给 {target_name}。"
                f"如果 {target_name} 明明已经和小灰毛聊过，通常是 Telegram 那边没让这次投递落进去，"
                "可以让对方再发一句 /start 或普通消息后再试一次。"
            ),
            "error": str(exc),
            "target_chat_id": target_chat_id,
            "route_source": "cached_route" if route_chat_id is not None else "direct_user_id",
        }
    remember_private_chat_route(target_id, target_chat_id)
    return {
        "success": True,
        "message": f"好呀，小灰毛已经把话带给 {target_name} 了。",
        "target_chat_id": target_chat_id,
        "message_id": payload.get("message_id"),
        "route_source": "cached_route" if route_chat_id is not None else "direct_user_id",
    }


def run_workbench_action(action: str, user_id: int, user_name: str, text: str) -> dict[str, Any]:
    if action != "forward_message":
        raise ValueError(f"Unsupported family workbench action: {action}")
    params = _parse_forward_message(text, sender_user_id=user_id)
    log_event(
        logger,
        "family_workbench.action_start",
        action=action,
        user_id=user_id,
        target_id=int(params["target_id"]),
        target_name=str(params["target_name"]),
    )
    with timed_event(
        logger,
        "family_workbench.action_complete",
        action=action,
        user_id=user_id,
        target_id=int(params["target_id"]),
        target_name=str(params["target_name"]),
    ):
        raw_result = _deliver_forward_message(
            sender_name=user_name,
            target_id=int(params["target_id"]),
            target_name=str(params["target_name"]),
            body=str(params["body"]),
        )
    result = {
        "success": bool(raw_result.get("success", False)),
        "action": action,
        "params": params,
        "reply": _render_forward_reply(raw_result).strip(),
        "payload": raw_result,
    }
    log_event(
        logger,
        "family_workbench.action_result",
        action=action,
        user_id=user_id,
        target_id=int(params["target_id"]),
        target_name=str(params["target_name"]),
        success=bool(raw_result.get("success", False)),
        target_chat_id=raw_result.get("target_chat_id"),
        message_id=raw_result.get("message_id"),
    )
    return result


async def run_workbench_action_async(action: str, user_id: int, user_name: str, text: str) -> dict[str, Any]:
    if action != "forward_message":
        return await asyncio.to_thread(run_workbench_action, action, user_id, user_name, text)
    params = _parse_forward_message(text, sender_user_id=user_id)
    log_event(
        logger,
        "family_workbench.action_start",
        action=action,
        user_id=user_id,
        target_id=int(params["target_id"]),
        target_name=str(params["target_name"]),
    )
    with timed_event(
        logger,
        "family_workbench.action_complete",
        action=action,
        user_id=user_id,
        target_id=int(params["target_id"]),
        target_name=str(params["target_name"]),
    ):
        raw_result = await _deliver_forward_message_async(
            sender_name=user_name,
            target_id=int(params["target_id"]),
            target_name=str(params["target_name"]),
            body=str(params["body"]),
        )
    result = {
        "success": bool(raw_result.get("success", False)),
        "action": action,
        "params": params,
        "reply": _render_forward_reply(raw_result).strip(),
        "payload": raw_result,
    }
    log_event(
        logger,
        "family_workbench.action_result",
        action=action,
        user_id=user_id,
        target_id=int(params["target_id"]),
        target_name=str(params["target_name"]),
        success=bool(raw_result.get("success", False)),
        target_chat_id=raw_result.get("target_chat_id"),
        message_id=raw_result.get("message_id"),
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Simple family workbench")
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--user-name", required=True)
    parser.add_argument("--action", required=True, choices=["forward_message"])
    parser.add_argument("--text", required=True)
    args = parser.parse_args()
    result = run_workbench_action(args.action, args.user_id, args.user_name, args.text)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
