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
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.config import CODEX_RUNTIME_MODE, CODEX_SERVICE_TIER, CODEX_TIMEOUT_SECONDS
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
                "-c",
                f'model_reasoning_effort="{config.reasoning_effort}"',
                "--full-auto",
                "--skip-git-repo-check",
                state.persistent_session_id,
            ]
        else:
            args = [
                config.codex_bin,
                "exec",
                "-c",
                f'model_reasoning_effort="{config.reasoning_effort}"',
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


def _extract_final_text_from_item(item: dict[str, Any]) -> Optional[str]:
    if item.get("type") != "agentMessage":
        return None
    text = item.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return text.strip()


def _extract_final_text_from_turn(turn: dict[str, Any]) -> Optional[str]:
    items = turn.get("items") or []
    last_text: Optional[str] = None
    for item in items:
        if not isinstance(item, dict):
            continue
        text = _extract_final_text_from_item(item)
        if not text:
            continue
        if item.get("phase") == "final_answer":
            return text
        last_text = text
    return last_text


class CodexAppServerClient:
    def __init__(self, config: AssistantConfig) -> None:
        self.config = config
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._stderr_task: Optional[asyncio.Task[None]] = None
        self._start_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._request_id = 0
        self._pending_requests: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._pending_turns: dict[str, asyncio.Future[str]] = {}
        self._turn_messages: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._completed_turns: dict[str, str] = {}
        self._failed_turns: dict[str, str] = {}
        self._loaded_threads: set[str] = set()
        self._initialized = False

    async def ensure_started(self) -> None:
        async with self._start_lock:
            if self._proc and self._proc.returncode is None and self._initialized:
                return

            self._proc = await asyncio.create_subprocess_exec(
                self.config.codex_bin,
                "app-server",
                "-c",
                f'model_reasoning_effort="{self.config.reasoning_effort}"',
                "--listen",
                "stdio://",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.config.workspace_path,
                env={**os.environ, "CODEX_HOME": self.config.codex_home},
            )
            self._pending_requests = {}
            self._pending_turns = {}
            self._turn_messages = defaultdict(list)
            self._completed_turns = {}
            self._failed_turns = {}
            self._loaded_threads = set()
            self._initialized = False
            self._reader_task = asyncio.create_task(self._reader_loop())
            self._stderr_task = asyncio.create_task(self._stderr_loop())

            try:
                init_response = await self._request_no_start(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "family-finance-claw",
                            "version": "0.1.0",
                        },
                        "capabilities": {
                            "experimentalApi": True,
                        },
                    },
                )
                logger.info("[APP_SERVER] initialized userAgent=%s", init_response.get("userAgent"))
                await self._notify("initialized")
                self._initialized = True
            except Exception:
                await self._invalidate("app-server initialization failed")
                raise

    async def ensure_thread(self, state: CodexSessionState) -> str:
        await self.ensure_started()
        if state.persistent_session_id and state.persistent_session_id in self._loaded_threads:
            return state.persistent_session_id

        if state.persistent_session_id:
            try:
                response = await self._request(
                    "thread/resume",
                    {
                        "threadId": state.persistent_session_id,
                        "approvalPolicy": "never",
                        "cwd": self.config.workspace_path,
                        "model": self.config.codex_model or None,
                        "serviceTier": CODEX_SERVICE_TIER,
                        "sandbox": "workspace-write",
                    },
                )
                thread = response.get("thread", {})
                thread_id = str(thread.get("id") or state.persistent_session_id)
                self._loaded_threads.add(thread_id)
                state.transport = "app-server"
                return thread_id
            except Exception:
                logger.warning("[APP_SERVER] Failed to resume thread %s, creating a fresh one", state.persistent_session_id)
                self._loaded_threads.discard(state.persistent_session_id)
                state.persistent_session_id = None

        response = await self._request(
            "thread/start",
            {
                "cwd": self.config.workspace_path,
                "approvalPolicy": "never",
                "sandbox": "workspace-write",
                "model": self.config.codex_model or None,
                "serviceTier": CODEX_SERVICE_TIER,
            },
        )
        thread = response.get("thread", {})
        thread_id = str(thread.get("id") or "")
        if not thread_id:
            raise RuntimeError("app-server thread/start returned no thread id")
        self._loaded_threads.add(thread_id)
        state.persistent_session_id = thread_id
        state.transport = "app-server"
        return thread_id

    async def run_turn(self, state: CodexSessionState, prompt: str, image_path: Optional[str] = None) -> str:
        thread_id = await self.ensure_thread(state)
        turn_response = await self._request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": self._build_turn_input(prompt, image_path=image_path),
            },
        )
        turn = turn_response.get("turn", {})
        turn_id = str(turn.get("id") or "")
        if not turn_id:
            raise RuntimeError("app-server turn/start returned no turn id")

        immediate_text = _extract_final_text_from_turn(turn)
        if immediate_text:
            return immediate_text
        if turn_id in self._completed_turns:
            return self._completed_turns.pop(turn_id)
        if turn_id in self._failed_turns:
            raise RuntimeError(self._failed_turns.pop(turn_id))

        future = asyncio.get_running_loop().create_future()
        self._pending_turns[turn_id] = future
        try:
            return await asyncio.wait_for(future, timeout=CODEX_TIMEOUT_SECONDS)
        finally:
            self._pending_turns.pop(turn_id, None)
            self._turn_messages.pop(turn_id, None)
            self._completed_turns.pop(turn_id, None)
            self._failed_turns.pop(turn_id, None)

    def _build_turn_input(self, prompt: str, image_path: Optional[str] = None) -> list[dict[str, str]]:
        items: list[dict[str, str]] = [{"type": "text", "text": prompt}]
        if image_path:
            items.append({"type": "localImage", "path": image_path})
        return items

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        await self.ensure_started()
        return await self._request_no_start(method, params)

    async def _request_no_start(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        async with self._request_lock:
            self._request_id += 1
            req_id = self._request_id
            future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            self._pending_requests[req_id] = future
            await self._send({"id": req_id, "method": method, "params": params})

        try:
            response = await asyncio.wait_for(future, timeout=CODEX_TIMEOUT_SECONDS)
        finally:
            self._pending_requests.pop(req_id, None)

        if "error" in response:
            raise RuntimeError(f"app-server {method} failed: {response['error']}")
        return response.get("result", {})

    async def _notify(self, method: str, params: Optional[dict[str, Any]] = None) -> None:
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        await self._send(payload)

    async def _send(self, payload: dict[str, Any]) -> None:
        if not self._proc or not self._proc.stdin:
            raise RuntimeError("app-server stdin unavailable")
        self._proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def _reader_loop(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    logger.warning("[APP_SERVER] Ignored non-JSON line")
                    continue
                await self._handle_message(message)
        finally:
            await self._invalidate("app-server stdout closed")

    async def _stderr_loop(self) -> None:
        assert self._proc and self._proc.stderr
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="ignore").rstrip()
            if text:
                logger.info("[APP_SERVER][stderr] %s", text)

    async def _handle_message(self, message: dict[str, Any]) -> None:
        if "id" in message:
            req_id = int(message["id"])
            future = self._pending_requests.get(req_id)
            if future and not future.done():
                future.set_result(message)
            return

        method = message.get("method")
        params = message.get("params") or {}

        if method == "thread/started":
            thread = params.get("thread", {})
            thread_id = thread.get("id")
            if isinstance(thread_id, str):
                self._loaded_threads.add(thread_id)
            return

        if method == "item/completed":
            turn_id = params.get("turnId")
            item = params.get("item") or {}
            if isinstance(turn_id, str) and isinstance(item, dict):
                self._turn_messages[turn_id].append(item)
            return

        if method == "turn/completed":
            turn_id = params.get("turnId")
            turn = params.get("turn") or {}
            if not isinstance(turn_id, str):
                turn_id = str(turn.get("id") or "")
            if not turn_id:
                return
            future = self._pending_turns.get(turn_id)
            if future and future.done():
                return
            final_text = _extract_final_text_from_turn(turn)
            if not final_text:
                for item in self._turn_messages.get(turn_id, []):
                    text = _extract_final_text_from_item(item)
                    if text:
                        if item.get("phase") == "final_answer":
                            final_text = text
                            break
                        final_text = text
            result_text = final_text or "操作完成。"
            if future:
                future.set_result(result_text)
            else:
                self._completed_turns[turn_id] = result_text
            return

        if method == "error":
            turn_id = params.get("turnId")
            will_retry = bool(params.get("willRetry"))
            if not will_retry and isinstance(turn_id, str):
                error = params.get("error") or {}
                message_text = str(error.get("message") or "app-server turn failed")
                future = self._pending_turns.get(turn_id)
                if future and not future.done():
                    future.set_exception(RuntimeError(message_text))
                else:
                    self._failed_turns[turn_id] = message_text

    async def _invalidate(self, reason: str) -> None:
        if reason:
            logger.warning("[APP_SERVER] Invalidating resident client: %s", reason)
        self._initialized = False
        self._loaded_threads.clear()

        for future in self._pending_requests.values():
            if not future.done():
                future.set_exception(RuntimeError(reason))
        self._pending_requests.clear()

        for future in self._pending_turns.values():
            if not future.done():
                future.set_exception(RuntimeError(reason))
        self._pending_turns.clear()

        if self._proc and self._proc.returncode is None:
            self._proc.kill()
            await self._proc.wait()

        self._proc = None
        for task_attr in ("_reader_task", "_stderr_task"):
            task = getattr(self, task_attr)
            if task and task is not asyncio.current_task() and not task.done():
                task.cancel()
            setattr(self, task_attr, None)


class CodexAppServerRuntime:
    def __init__(self) -> None:
        self._clients: dict[str, CodexAppServerClient] = {}

    def _get_client(self, config: AssistantConfig) -> CodexAppServerClient:
        client = self._clients.get(config.assistant_id)
        if client is None:
            client = CodexAppServerClient(config)
            self._clients[config.assistant_id] = client
        return client

    async def run(
        self,
        config: AssistantConfig,
        state: CodexSessionState,
        prompt: str,
        image_path: Optional[str] = None,
    ) -> str:
        client = self._get_client(config)
        logger.info(
            "[APP_SERVER] assistant=%s transport=%s chat=%s user=%s thread=%s",
            config.assistant_id,
            state.transport,
            state.key.chat_id,
            state.key.user_id,
            state.persistent_session_id or "new",
        )
        try:
            message = await client.run_turn(state, prompt, image_path=image_path)
            state.last_error = ""
            return message
        except Exception as exc:
            state.last_error = f"app-server:{exc}"
            raise
        finally:
            state.touch()


class CompositeCodexRuntime:
    def __init__(self) -> None:
        self.exec_runtime = CodexExecRuntime()
        self.app_server_runtime = CodexAppServerRuntime()

    async def run(
        self,
        config: AssistantConfig,
        state: CodexSessionState,
        prompt: str,
        image_path: Optional[str] = None,
    ) -> str:
        mode = (CODEX_RUNTIME_MODE or "auto").lower()
        if mode == "exec":
            return await self.exec_runtime.run(config, state, prompt, image_path=image_path)

        if mode in {"auto", "app-server", "app_server"}:
            try:
                return await self.app_server_runtime.run(config, state, prompt, image_path=image_path)
            except Exception:
                logger.exception("[APP_SERVER] Falling back to exec runtime")
                if mode in {"app-server", "app_server"}:
                    return "常驻 Codex 服务当前失败了，请稍后再试。"
                return await self.exec_runtime.run(config, state, prompt, image_path=image_path)

        return await self.exec_runtime.run(config, state, prompt, image_path=image_path)


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
