"""Provider-facing runtime adapter layer.

This keeps the current implementation on Codex while making the service layer
less tied to a single CLI/runtime forever.
"""

from __future__ import annotations

from typing import Optional, Protocol

from app.core.assistant_registry import AssistantConfig
from app.core.codex_session import CodexSessionState, CompositeCodexRuntime


class AgentRuntime(Protocol):
    async def run(
        self,
        config: AssistantConfig,
        state: CodexSessionState,
        prompt: str,
        image_path: Optional[str] = None,
    ) -> str: ...


class ProviderRuntimeRouter:
    def __init__(self) -> None:
        self._providers: dict[str, AgentRuntime] = {
            "codex": CompositeCodexRuntime(),
        }

    async def run(
        self,
        config: AssistantConfig,
        state: CodexSessionState,
        prompt: str,
        image_path: Optional[str] = None,
    ) -> str:
        provider = (config.runtime_provider or "codex").strip().lower()
        runtime = self._providers.get(provider)
        if runtime is None:
            return f"当前还没有接入 `{provider}` runtime，请先切回 Codex 或补上对应 adapter。"
        return await runtime.run(
            config,
            state,
            prompt,
            image_path=image_path,
        )
