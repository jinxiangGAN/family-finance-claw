"""Resident action registry for latency-sensitive local actions.

This keeps hot-path actions in the main Python process so Codex can call a
small resident surface instead of spawning a new Python interpreter for every
simple finance/family turn.
"""

from __future__ import annotations

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
from app.core.family_workbench import run_workbench_action as run_family_workbench_action
from app.core.finance_workbench import run_workbench_action as run_finance_workbench_action
from app.core.observability import log_event, timed_event

logger = logging.getLogger(__name__)

_ACTION_MAP: dict[str, tuple[str, str]] = {
    "finance.record_expense": ("finance", "record_expense"),
    "finance.recent_expenses": ("finance", "recent_expenses"),
    "finance.month_total": ("finance", "month_total"),
    "finance.budget_query": ("finance", "budget_query"),
    "finance.budget_set": ("finance", "budget_set"),
    "finance.delete_by_id": ("finance", "delete_by_id"),
    "family.forward_message": ("family", "forward_message"),
}


def run_action(action: str, user_id: int, user_name: str, text: str) -> dict[str, Any]:
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
        return run_family_workbench_action(workbench_action, user_id, user_name, text)


class _ThreadingUnixStreamHTTPServer(ThreadingMixIn, UnixStreamServer):
    daemon_threads = True


class _ActionRegistryHandler(BaseHTTPRequestHandler):
    server_version = "ActionRegistry/0.1"

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return
        self._send_json(HTTPStatus.OK, {"ok": True})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/run":
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return

        try:
            payload = self._parse_payload()
            action = str(payload.get("action") or "").strip()
            user_id = int(payload.get("user_id"))
            user_name = str(payload.get("user_name") or "")
            text = str(payload.get("text") or "")
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
            result = run_action(action, user_id, user_name, text)
        except Exception as exc:
            logger.exception("Action registry run failed")
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": f"internal_error: {exc}"},
            )
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
