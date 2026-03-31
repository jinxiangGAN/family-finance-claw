"""Resident action registry for latency-sensitive local actions.

This keeps hot-path actions in the main Python process so Codex can call a
small resident surface instead of spawning a new Python interpreter for every
simple finance/family turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn, UnixStreamServer
from typing import Any
from urllib.parse import parse_qs

from app.config import ACTION_REGISTRY_SOCKET_PATH
from app.bridge_ops import run_skill as run_bridge_skill
from app.bridge_ops import snapshot as bridge_snapshot
from app.bridge_ops import store_memory_entry
from app.core.family_workbench import (
    run_workbench_action as run_family_workbench_action,
    run_workbench_action_async as run_family_workbench_action_async,
)
from app.core.finance_workbench import run_workbench_action as run_finance_workbench_action
from app.core.observability import log_event, timed_event
from app.core.terminal_workbench import run_workbench_action as run_terminal_workbench_action

logger = logging.getLogger(__name__)

_ACTION_MAP: dict[str, tuple[str, str]] = {
    "finance.record_expense": ("finance", "record_expense"),
    "finance.recent_expenses": ("finance", "recent_expenses"),
    "finance.month_total": ("finance", "month_total"),
    "finance.today_total": ("finance", "today_total"),
    "finance.exchange_rate": ("finance", "exchange_rate"),
    "finance.budget_query": ("finance", "budget_query"),
    "finance.budget_set": ("finance", "budget_set"),
    "finance.delete_by_id": ("finance", "delete_by_id"),
    "family.forward_message": ("family", "forward_message"),
    "terminal.runtime_status": ("terminal", "runtime_status"),
    "terminal.list_memories": ("terminal", "list_memories"),
    "terminal.export_csv": ("terminal", "export_csv"),
    "terminal.reset_context": ("terminal", "reset_context"),
}


def run_bridge_snapshot_action(user_id: int) -> dict[str, Any]:
    return bridge_snapshot(user_id)


def run_bridge_skill_action(user_id: int, user_name: str, name: str, params: dict[str, Any]) -> dict[str, Any]:
    return run_bridge_skill(user_id, user_name, name, params)


def run_bridge_store_memory_action(
    *,
    user_id: int,
    content: str,
    category: str = "general",
    importance: int = 5,
    shared: bool = False,
) -> dict[str, Any]:
    return store_memory_entry(
        user_id=user_id,
        content=content,
        category=category,
        importance=importance,
        shared=shared,
    )


def run_action(
    action: str,
    user_id: int,
    user_name: str,
    text: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target = _ACTION_MAP.get(action)
    if target is None:
        return {
            "success": False,
            "action": action,
            "reply": f"当前还不支持动作 `{action}`。",
            "payload": {"message": f"Unsupported action: {action}"},
        }

    namespace, workbench_action = target
    with timed_event(logger, "action_registry.run_action", action=action, user_id=user_id):
        if namespace == "finance":
            return run_finance_workbench_action(workbench_action, user_id, user_name, text)
        if namespace == "family":
            return run_family_workbench_action(workbench_action, user_id, user_name, text)
        return run_terminal_workbench_action(workbench_action, user_id, user_name, text, params=params)


async def run_action_async(
    action: str,
    user_id: int,
    user_name: str,
    text: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target = _ACTION_MAP.get(action)
    if target is None:
        return {
            "success": False,
            "action": action,
            "reply": f"当前还不支持动作 `{action}`。",
            "payload": {"message": f"Unsupported action: {action}"},
        }

    namespace, workbench_action = target
    with timed_event(logger, "action_registry.run_action_async", action=action, user_id=user_id):
        if namespace == "family":
            return await run_family_workbench_action_async(workbench_action, user_id, user_name, text)
        return await asyncio.to_thread(run_action, action, user_id, user_name, text, params)


async def run_bridge_snapshot_async(user_id: int) -> dict[str, Any]:
    with timed_event(logger, "action_registry.run_bridge_snapshot_async", user_id=user_id):
        return await asyncio.to_thread(run_bridge_snapshot_action, user_id)


async def run_bridge_skill_async(
    user_id: int,
    user_name: str,
    name: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    with timed_event(logger, "action_registry.run_bridge_skill_async", user_id=user_id, skill_name=name):
        return await asyncio.to_thread(run_bridge_skill_action, user_id, user_name, name, params)


async def run_bridge_store_memory_async(
    *,
    user_id: int,
    content: str,
    category: str = "general",
    importance: int = 5,
    shared: bool = False,
) -> dict[str, Any]:
    with timed_event(
        logger,
        "action_registry.run_bridge_store_memory_async",
        user_id=user_id,
        category=category,
        shared=shared,
    ):
        return await asyncio.to_thread(
            run_bridge_store_memory_action,
            user_id=user_id,
            content=content,
            category=category,
            importance=importance,
            shared=shared,
        )


class _ThreadingUnixStreamHTTPServer(ThreadingMixIn, UnixStreamServer):
    daemon_threads = True


class _ActionRegistryHandler(BaseHTTPRequestHandler):
    server_version = "ActionRegistry/0.1"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if self.path.startswith("/bridge/snapshot"):
            self._handle_bridge_snapshot()
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/run":
            self._handle_run()
            return
        if self.path == "/bridge/skill":
            self._handle_bridge_skill()
            return
        if self.path == "/bridge/store-memory":
            self._handle_bridge_store_memory()
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

    def _handle_run(self) -> None:
        try:
            payload = self._parse_payload()
            action = str(payload.get("action") or "").strip()
            user_id = int(payload.get("user_id"))
            user_name = str(payload.get("user_name") or "")
            text = str(payload.get("text") or "")
            params = payload.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("params must be an object")
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"bad_request: {exc}"})
            return

        log_event(
            logger,
            "action_registry.request",
            action=action,
            user_id=user_id,
            text_preview=text[:80],
        )
        try:
            result = run_action(action, user_id, user_name, text, params=params)
        except Exception as exc:
            logger.exception("Action registry run failed")
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": f"internal_error: {exc}"},
            )
            return
        self._send_json(HTTPStatus.OK, result)

    def _handle_bridge_snapshot(self) -> None:
        try:
            _, _, query = self.path.partition("?")
            params = parse_qs(query, keep_blank_values=True)
            user_id = int((params.get("user_id") or [""])[-1])
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"bad_request: {exc}"})
            return

        log_event(logger, "action_registry.bridge_snapshot", user_id=user_id)
        try:
            result = bridge_snapshot(user_id)
        except Exception as exc:
            logger.exception("Bridge snapshot failed")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": f"internal_error: {exc}"})
            return
        self._send_json(HTTPStatus.OK, result)

    def _handle_bridge_skill(self) -> None:
        try:
            payload = self._parse_payload()
            user_id = int(payload.get("user_id"))
            user_name = str(payload.get("user_name") or "")
            name = str(payload.get("name") or "").strip()
            params = payload.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("params must be an object")
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"bad_request: {exc}"})
            return

        log_event(logger, "action_registry.bridge_skill", user_id=user_id, skill_name=name)
        try:
            result = run_bridge_skill(user_id, user_name, name, params)
        except Exception as exc:
            logger.exception("Bridge skill failed")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": f"internal_error: {exc}"})
            return
        self._send_json(HTTPStatus.OK, result)

    def _handle_bridge_store_memory(self) -> None:
        try:
            payload = self._parse_payload()
            user_id = int(payload.get("user_id"))
            content = str(payload.get("content") or "")
            category = str(payload.get("category") or "general")
            importance = int(payload.get("importance") or 5)
            shared = bool(payload.get("shared"))
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"bad_request: {exc}"})
            return

        log_event(logger, "action_registry.bridge_store_memory", user_id=user_id, category=category, shared=shared)
        try:
            result = store_memory_entry(
                user_id=user_id,
                content=content,
                category=category,
                importance=importance,
                shared=shared,
            )
        except Exception as exc:
            logger.exception("Bridge store-memory failed")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": f"internal_error: {exc}"})
            return
        self._send_json(HTTPStatus.OK, result)

    def log_message(self, format: str, *args: object) -> None:
        logger.debug("Action registry HTTP: " + format, *args)

    def _parse_payload(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length)
        content_type = (self.headers.get("Content-Type") or "").lower()
        if "application/json" in content_type:
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("JSON payload must be an object")
            return payload

        form = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        return {key: values[-1] for key, values in form.items()}

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ActionRegistryServer:
    def __init__(self, socket_path: str = ACTION_REGISTRY_SOCKET_PATH) -> None:
        self.socket_path = Path(socket_path)
        self._server: _ThreadingUnixStreamHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._server is not None:
            return
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        self._server = _ThreadingUnixStreamHTTPServer(str(self.socket_path), _ActionRegistryHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="action-registry", daemon=True)
        self._thread.start()
        os.chmod(self.socket_path, 0o600)
        log_event(logger, "action_registry.started", socket_path=str(self.socket_path))

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        if self._thread is not None:
            self._thread.join(timeout=1)
            self._thread = None
        try:
            if self.socket_path.exists():
                self.socket_path.unlink()
        finally:
            log_event(logger, "action_registry.stopped", socket_path=str(self.socket_path))


DEFAULT_ACTION_REGISTRY_SERVER = ActionRegistryServer()
