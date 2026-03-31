"""Resident agent service entrypoint.

This is the first step toward a future multi-assistant service layer. Today it
still uses one-shot exec under the hood, but Telegram handlers now talk to this
service abstraction instead of calling the Codex CLI directly.
"""

from __future__ import annotations

from typing import Optional

from app.config import DEFAULT_ASSISTANT_ID
from app.core.assistant_registry import AssistantRegistry, DEFAULT_ASSISTANT_REGISTRY
from app.core.codex_session import CodexExecRuntime, CodexSessionManager


class ResidentAgentService:
    def __init__(
        self,
        registry: Optional[AssistantRegistry] = None,
        session_manager: Optional[CodexSessionManager] = None,
        runtime: Optional[CodexExecRuntime] = None,
    ) -> None:
        self.registry = registry or DEFAULT_ASSISTANT_REGISTRY
        default_assistant = self.registry.get(DEFAULT_ASSISTANT_ID)
        self.session_manager = session_manager or CodexSessionManager(
            session_store_path=default_assistant.session_store_path
        )
        self.runtime = runtime or CodexExecRuntime()

    async def run(
        self,
        prompt: str,
        *,
        assistant_id: str = DEFAULT_ASSISTANT_ID,
        user_id: int,
        chat_id: int,
        image_path: Optional[str] = None,
    ) -> str:
        assistant = self.registry.get(assistant_id)
        state = self.session_manager.get_or_create(
            assistant_id=assistant_id,
            user_id=user_id,
            chat_id=chat_id,
        )
        reply = await self.runtime.run(
            assistant,
            state,
            prompt,
            image_path=image_path,
        )
        self.session_manager.save()
        return reply

    def reset(self, user_id: int, chat_id: int, assistant_id: str = DEFAULT_ASSISTANT_ID) -> None:
        self.session_manager.reset(assistant_id=assistant_id, user_id=user_id, chat_id=chat_id)


DEFAULT_RESIDENT_AGENT_SERVICE = ResidentAgentService()
