"""Codex runtime/session abstractions.

The first implementation still uses one-shot `codex exec`, but the public
surface is intentionally shaped around resident sessions so we can later swap in
interactive `resume`-backed sessions without rewriting the Telegram layer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.config import CODEX_TIMEOUT_SECONDS
from app.core.assistant_registry import AssistantConfig

logger = logging.getLogger(__name__)

SESSION_IDLE_TIMEOUT_SECONDS = 1800


@dataclass(frozen=True)
class CodexSessionKey:
    assistant_id: str
    user_id: int
    chat_id: int


@dataclass
class CodexSessionState:
    key: CodexSessionKey
    created_at: float = field(default_factory=time.time)
    last_active_at: float = field(default_factory=time.time)
    turn_count: int = 0
    transport: str = "exec"
    persistent_session_id: Optional[str] = None
    last_error: str = ""

    def touch(self) -> None:
        self.last_active_at = time.time()
        self.turn_count += 1

    def is_idle(self, timeout_seconds: int = SESSION_IDLE_TIMEOUT_SECONDS) -> bool:
        return (time.time() - self.last_active_at) > timeout_seconds


class CodexExecRuntime:
    """Current runtime adapter backed by one-shot `codex exec`.

    It keeps the future hooks we need (`persistent_session_id`, `transport`) so
    a later `resume`/interactive implementation can fit underneath the same
    interface.
    """

    async def run(
        self,
        config: AssistantConfig,
        state: CodexSessionState,
        prompt: str,
        image_path: Optional[str] = None,
    ) -> str:
        return await self._run_with_recovery(
            config,
            state,
            prompt,
            image_path=image_path,
        )

    async def _run_with_recovery(
        self,
        config: AssistantConfig,
        state: CodexSessionState,
        prompt: str,
        image_path: Optional[str] = None,
    ) -> str:
        args, output_path = self._build_args(config, state, prompt, image_path=image_path)
        should_capture_thread = state.persistent_session_id is None
        started_at = int(time.time())
        prior_thread_id = state.persistent_session_id

        logger.info(
            "[CODEX] assistant=%s transport=%s chat=%s user=%s thread=%s",
            config.assistant_id,
            state.transport,
            state.key.chat_id,
            state.key.user_id,
            prior_thread_id or "new",
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=config.workspace_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "CODEX_HOME": config.codex_home},
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=CODEX_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                logger.error("[CODEX] Timed out after %ss", CODEX_TIMEOUT_SECONDS)
                state.last_error = "timeout"
                return "这次处理超时了，请稍后再试一次。"

            if proc.returncode != 0:
                stderr_text = stderr.decode("utf-8", errors="ignore")
                logger.error(
                    "[CODEX] Exit=%s stdout=%s stderr=%s",
                    proc.returncode,
                    stdout.decode("utf-8", errors="ignore")[:500],
                    stderr_text[:500],
                )
                if prior_thread_id:
                    logger.warning("[CODEX] Resume failed, dropping stored thread id and retrying fresh")
                    state.persistent_session_id = None
                    state.transport = "exec"
                    state.last_error = f"resume-exit:{proc.returncode}"
                    return await self._run_with_recovery(config, state, prompt, image_path=image_path)
                state.last_error = f"exit:{proc.returncode}"
                return "本地 Codex 处理失败了，请稍后再试。"

            try:
                with open(output_path, "r", encoding="utf-8") as fh:
                    message = fh.read().strip()
            except FileNotFoundError:
                logger.error("[CODEX] Output file missing")
                state.last_error = "missing-output"
                return "本地 Codex 没有返回结果，请稍后再试。"

            state.last_error = ""
            if should_capture_thread:
                thread_id = self._find_latest_thread_id(config, started_at=started_at)
                if thread_id:
                    state.persistent_session_id = thread_id
                    state.transport = "resume"
            return message or "操作完成。"
        finally:
            state.touch()
            try:
                os.unlink(output_path)
            except FileNotFoundError:
                pass

    def _build_args(
        self,
        config: AssistantConfig,
        state: CodexSessionState,
        prompt: str,
        *,
        image_path: Optional[str] = None,
    ) -> tuple[list[str], str]:
        if state.persistent_session_id:
            args = [
                config.codex_bin,
                "exec",
                "resume",
                "--full-auto",
                "--skip-git-repo-check",
                state.persistent_session_id,
            ]
        else:
            args = [
                config.codex_bin,
                "exec",
                "--full-auto",
                "--sandbox",
                "workspace-write",
                "--cd",
                config.workspace_path,
            ]

        if not state.persistent_session_id:
            for writable_dir in config.all_writable_dirs:
                args.extend(["--add-dir", writable_dir])

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        output_path = tmp.name
        tmp.close()
        args.extend(["--output-last-message", output_path])
        if not state.persistent_session_id:
            args.extend(["--color", "never"])

        if config.codex_profile and not state.persistent_session_id:
            args.extend(["--profile", config.codex_profile])
        if config.codex_model:
            args.extend(["--model", config.codex_model])
        if image_path:
            args.extend(["--image", image_path])

        args.append(prompt)
        return args, output_path

    def _find_latest_thread_id(self, config: AssistantConfig, *, started_at: int) -> Optional[str]:
        state_db = _resolve_codex_state_db(config.codex_home)
        if state_db is None:
            return None
        try:
            conn = sqlite3.connect(str(state_db))
            try:
                row = conn.execute(
                    """
                    SELECT id
                    FROM threads
                    WHERE cwd = ?
                      AND updated_at >= ?
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (config.workspace_path, started_at),
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            logger.exception("[CODEX] Failed to inspect state db for thread id")
            return None
        if not row:
            return None
        return str(row[0])


def _resolve_codex_state_db(codex_home: str) -> Optional[Path]:
    home = Path(codex_home)
    candidates = sorted(home.glob("state_*.sqlite"))
    if not candidates:
        return None
    return candidates[-1]


class CodexSessionManager:
    """Tracks per-assistant, per-chat runtime state.

    The state is intentionally generic so we can later attach a persistent
    Codex session id or a long-lived interactive subprocess.
    """

    def __init__(self, session_store_path: str = "data/codex_sessions.json") -> None:
        self._sessions: dict[CodexSessionKey, CodexSessionState] = {}
        self._session_store_path = Path(session_store_path)
        self._load_persistent_state()

    def get_or_create(self, assistant_id: str, user_id: int, chat_id: int) -> CodexSessionState:
        key = CodexSessionKey(assistant_id=assistant_id, user_id=user_id, chat_id=chat_id)
        session = self._sessions.get(key)
        if session is None or session.is_idle():
            session = self._restore_or_create(key)
            self._sessions[key] = session
        return session

    def reset(self, assistant_id: str, user_id: int, chat_id: int) -> None:
        key = CodexSessionKey(assistant_id=assistant_id, user_id=user_id, chat_id=chat_id)
        self._sessions.pop(key, None)
        self._persist()

    def reap_idle(self) -> int:
        keys_to_remove = [key for key, session in self._sessions.items() if session.is_idle()]
        for key in keys_to_remove:
            self._sessions.pop(key, None)
        if keys_to_remove:
            self._persist()
        return len(keys_to_remove)

    def save(self) -> None:
        self._persist()

    def _restore_or_create(self, key: CodexSessionKey) -> CodexSessionState:
        session = self._sessions.get(key)
        if session is not None:
            return session
        return CodexSessionState(key=key)

    def _load_persistent_state(self) -> None:
        if not self._session_store_path.exists():
            return
        try:
            data = json.loads(self._session_store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("[CODEX] Failed to load session store")
            return

        for raw_key, value in data.items():
            try:
                assistant_id, user_id, chat_id = raw_key.split(":", 2)
                key = CodexSessionKey(
                    assistant_id=assistant_id,
                    user_id=int(user_id),
                    chat_id=int(chat_id),
                )
            except ValueError:
                continue
            self._sessions[key] = CodexSessionState(
                key=key,
                created_at=float(value.get("created_at", time.time())),
                last_active_at=float(value.get("last_active_at", time.time())),
                turn_count=int(value.get("turn_count", 0)),
                transport=str(value.get("transport", "exec")),
                persistent_session_id=value.get("persistent_session_id"),
                last_error=str(value.get("last_error", "")),
            )

    def _persist(self) -> None:
        payload: dict[str, dict[str, object]] = {}
        for key, session in self._sessions.items():
            if not session.persistent_session_id:
                continue
            payload[f"{key.assistant_id}:{key.user_id}:{key.chat_id}"] = {
                "created_at": session.created_at,
                "last_active_at": session.last_active_at,
                "turn_count": session.turn_count,
                "transport": session.transport,
                "persistent_session_id": session.persistent_session_id,
                "last_error": session.last_error,
            }

        try:
            self._session_store_path.parent.mkdir(parents=True, exist_ok=True)
            self._session_store_path.write_text(
                json.dumps(payload, ensure_ascii=True, indent=2),
                encoding="utf-8",
            )
        except OSError:
            logger.exception("[CODEX] Failed to persist session store")
