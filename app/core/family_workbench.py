"""Simple family workbench for latency-sensitive household actions."""

from __future__ import annotations

import argparse
import json
import re
from typing import Any

import httpx

from app.config import FAMILY_MEMBERS, TELEGRAM_BOT_TOKEN
from app.core.session import get_private_chat_route

_FORWARD_MESSAGE_PATTERNS = [
    re.compile(
        r"^\s*(?:帮我)?给\s*(?P<target>[^\s,，:：]+)\s*(?:发消息|发|带句话|说一声|说)\s*[:：,，]?\s*(?P<body>.+?)\s*$"
    ),
    re.compile(
        r"^\s*(?:发消息给|发给|转告|转发给|跟)\s*(?P<target>[^\s,，:：]+)\s*(?:说|讲一下|带句话)?\s*[:：,，]?\s*(?P<body>.+?)\s*$"
    ),
]


def _resolve_family_member_id(identifier: str, *, exclude_user_id: int | None = None) -> int | None:
    normalized = identifier.strip().lower()
    alias_map: dict[str, int] = {}
    for uid, name in FAMILY_MEMBERS.items():
        alias_map[name.lower()] = uid
    for uid, name in FAMILY_MEMBERS.items():
        if "小白" in name:
            alias_map.setdefault("老婆", uid)
            alias_map.setdefault("妻子", uid)
        if "小鸡毛" in name:
            alias_map.setdefault("老公", uid)
            alias_map.setdefault("丈夫", uid)
    target_id = alias_map.get(normalized)
    if target_id is None:
        return None
    if exclude_user_id is not None and target_id == exclude_user_id:
        return None
    return target_id


def _parse_forward_message(text: str, *, sender_user_id: int) -> dict[str, Any]:
    match = None
    stripped = text.strip()
    for pattern in _FORWARD_MESSAGE_PATTERNS:
        match = pattern.match(stripped)
        if match:
            break
    if not match:
        raise ValueError("Could not parse a family forwarding request.")
    target_id = _resolve_family_member_id(match.group("target"), exclude_user_id=sender_user_id)
    if target_id is None:
        raise ValueError("Could not identify a valid family member target.")
    body = match.group("body").strip()
    if not body:
        raise ValueError("Forwarded message body is empty.")
    return {
        "target_id": target_id,
        "target_name": FAMILY_MEMBERS.get(target_id, str(target_id)),
        "body": body,
    }


def _render_forward_reply(result: dict[str, Any]) -> str:
    return str(result.get("message") or "小灰毛已经把话带到了。")


def _deliver_forward_message(
    *,
    sender_name: str,
    target_id: int,
    target_name: str,
    body: str,
) -> dict[str, Any]:
    if not TELEGRAM_BOT_TOKEN:
        return {"success": False, "message": "小灰毛现在还没有连上 Telegram token，所以暂时没法代发。"}
    target_chat_id = get_private_chat_route(target_id)
    if target_chat_id is None:
        return {
            "success": False,
            "message": f"小灰毛这边还没连上 {target_name} 的私聊入口。先让 {target_name} 私聊小灰毛发一句话，再来让小灰毛代发就行。",
        }
    forwarded_text = f"📨 小灰毛帮忙转一句 {sender_name} 的话：\n\n{body}"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        response = httpx.post(url, json={"chat_id": target_chat_id, "text": forwarded_text}, timeout=15.0)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return {
            "success": False,
            "message": f"这次小灰毛没能把话带给 {target_name}。先让 {target_name} 再私聊小灰毛发一句话，或者稍后再试一次会更稳。",
            "error": str(exc),
            "target_chat_id": target_chat_id,
        }
    if not payload.get("ok"):
        return {
            "success": False,
            "message": f"这次小灰毛没能把话带给 {target_name}。先让 {target_name} 再私聊小灰毛发一句话，或者稍后再试一次会更稳。",
            "payload": payload,
            "target_chat_id": target_chat_id,
        }
    result = payload.get("result") or {}
    return {
        "success": True,
        "message": f"好呀，小灰毛已经把话带给 {target_name} 了。",
        "target_chat_id": target_chat_id,
        "message_id": result.get("message_id"),
    }


def run_workbench_action(action: str, user_id: int, user_name: str, text: str) -> dict[str, Any]:
    if action != "forward_message":
        raise ValueError(f"Unsupported family workbench action: {action}")
    params = _parse_forward_message(text, sender_user_id=user_id)
    raw_result = _deliver_forward_message(
        sender_name=user_name,
        target_id=int(params["target_id"]),
        target_name=str(params["target_name"]),
        body=str(params["body"]),
    )
    return {
        "success": bool(raw_result.get("success", False)),
        "action": action,
        "params": params,
        "reply": _render_forward_reply(raw_result).strip(),
        "payload": raw_result,
    }


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
