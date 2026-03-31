"""Resident terminal workbench for Telegram command-style actions.

These actions are not free-form business turns, but they still benefit from a
shared resident execution surface so Telegram stays a thin terminal shell.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from zoneinfo import ZoneInfo

from app.config import CURRENCY, TIMEZONE
from app.core.agent import reset_agent_context
from app.core.observability import log_event, timed_event
from app.core.resident_agent import DEFAULT_RESIDENT_AGENT_SERVICE
from app.core.session import reset_session
from app.core.memory import get_recent_memories
from app.services.skills import execute_skill

logger = logging.getLogger(__name__)


def _render_runtime_status(payload: dict[str, Any]) -> str:
    status = "降级运行中" if payload.get("degraded_mode") else "正常"
    active_runtime = str(payload.get("active_runtime") or "unknown")
    transport = str(payload.get("transport") or "unknown")
    configured_mode = str(payload.get("configured_mode") or "unknown")
    provider = str(payload.get("provider") or "unknown")

    lines = [
        "小灰毛当前运行状态：",
        f"- Provider: {provider}",
        f"- Runtime mode: {configured_mode}",
        "- Action surface: resident registry",
        f"- Active runtime: {active_runtime}",
        f"- Transport: {transport}",
        f"- Status: {status}",
    ]
    if payload.get("fallback_active"):
        lines.append("- Fallback: 已启用备用慢路径")
    degraded_reason = str(payload.get("degraded_reason") or "").strip()
    if degraded_reason:
        lines.append(f"- Reason: {degraded_reason}")
    thread_id = str(payload.get("thread_id") or "").strip()
    if thread_id:
        lines.append(f"- Thread: {thread_id}")
    turn_count = payload.get("turn_count")
    if isinstance(turn_count, int) and turn_count > 0:
        lines.append(f"- Turns in this thread: {turn_count}")
    return "\n".join(lines)


def _usage_status(
    user_id: int,
    user_name: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    assistant_id = str(params.get("assistant_id") or "family-finance")
    chat_id = int(params.get("chat_id"))
    runtime_status = DEFAULT_RESIDENT_AGENT_SERVICE.get_runtime_status(
        assistant_id=assistant_id,
        user_id=user_id,
        chat_id=chat_id,
    )
    return {
        "success": True,
        "message": _render_runtime_status(runtime_status),
        "runtime_status": runtime_status,
    }


def _list_memories(user_id: int, user_name: str, params: dict[str, Any]) -> dict[str, Any]:
    limit = int(params.get("limit") or 15)
    include_archived = bool(params.get("include_archived", True))
    memories = get_recent_memories(user_id, limit=limit, include_archived=include_archived)
    if not memories:
        return {
            "success": True,
            "message": "小灰毛这边暂时还没有记住什么，之后多聊几句就会慢慢有啦。",
            "items": [],
        }

    lines = ["小灰毛记着这些：", ""]
    for memory in memories:
        scope = "家庭" if memory.get("scope") == "family" else "个人"
        status = "active" if memory.get("is_active", True) else "archived"
        lines.append(f"#{memory['id']} [{scope}/{memory['category']}/{status}] {memory['content']}")
    lines.append("")
    lines.append("说「归档记忆 #ID」会归档；说「把记忆 #ID 改成 ...」可以迭代更新。")
    return {
        "success": True,
        "message": "\n".join(lines).strip(),
        "items": memories,
    }


def _export_csv(user_id: int, user_name: str, params: dict[str, Any]) -> dict[str, Any]:
    scope = str(params.get("scope") or "me")
    result = execute_skill("export_csv", user_id, user_name, {"scope": scope})
    if not result.get("success"):
        return {
            "success": False,
            "message": str(result.get("message") or "这次没有导出成功。"),
            "payload": result,
        }

    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    csv_content = str(result.get("csv_content") or "")
    filename = f"expenses_{scope}_{now.strftime('%Y%m%d')}.csv"
    line_count = max(csv_content.count("\n"), 0)
    caption = f"📤 {scope} 账单导出完成（{line_count} 条记录）"
    return {
        "success": True,
        "message": "导出准备好了。",
        "csv_content": csv_content,
        "filename": filename,
        "caption": caption,
        "currency": CURRENCY,
    }


def _reset_context_action(user_id: int, user_name: str, params: dict[str, Any]) -> dict[str, Any]:
    assistant_id = str(params.get("assistant_id") or "family-finance")
    chat_id = int(params.get("chat_id"))
    is_group = bool(params.get("is_group"))
    reset_agent_context(
        user_id=user_id,
        chat_id=chat_id,
        assistant_id=assistant_id,
        is_group=is_group,
    )
    reset_session(user_id, chat_id)
    return {
        "success": True,
        "message": "好啦，这段聊天上下文已经清空了。账目、记忆和画像都还在，小灰毛只是把这段临时状态放下了。",
    }


_ACTIONS: dict[str, Any] = {
    "runtime_status": _usage_status,
    "list_memories": _list_memories,
    "export_csv": _export_csv,
    "reset_context": _reset_context_action,
}


def run_workbench_action(
    action: str,
    user_id: int,
    user_name: str,
    text: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if action not in _ACTIONS:
        raise ValueError(f"Unsupported terminal workbench action: {action}")
    action_params = dict(params or {})
    log_event(
        logger,
        "terminal_workbench.action_start",
        action=action,
        user_id=user_id,
    )
    with timed_event(
        logger,
        "terminal_workbench.action_complete",
        action=action,
        user_id=user_id,
    ):
        raw_result = _ACTIONS[action](user_id, user_name, action_params)
    success = bool(raw_result.get("success", False))
    result = {
        "success": success,
        "action": action,
        "params": action_params,
        "reply": str(raw_result.get("message") or ("操作完成。" if success else "这次操作失败了。")).strip(),
        "payload": raw_result,
    }
    log_event(
        logger,
        "terminal_workbench.action_result",
        action=action,
        user_id=user_id,
        success=success,
    )
    return result
