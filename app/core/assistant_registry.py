"""Assistant registry for Codex-backed Telegram assistants.

This keeps the current repo usable as a single assistant while preserving the
shape we need for a future outer orchestration layer that routes one Codex
service across multiple repositories and assistant personas.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.config import (
    ASSISTANT_REGISTRY_PATH,
    CODEX_BIN,
    CODEX_HOME,
    CODEX_MODEL,
    CODEX_PROFILE,
    CODEX_SESSION_STORE_PATH,
    CODEX_WORKDIR,
    DATABASE_PATH,
    DEFAULT_ASSISTANT_ID,
    DEFAULT_ASSISTANT_NAME,
    PYTHON_BIN,
)


@dataclass(frozen=True)
class AssistantConfig:
    assistant_id: str
    display_name: str
    workspace_path: str
    codex_bin: str
    codex_home: str
    python_bin: str
    database_path: str
    session_store_path: str
    codex_model: str = ""
    codex_profile: str = ""
    bridge_module: str = "app.bridge_ops"
    soul_file: str = "AGENTS.md"
    aliases: tuple[str, ...] = ()
    writable_dirs: tuple[str, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def workspace(self) -> Path:
        return Path(self.workspace_path).resolve()

    @property
    def soul_file_path(self) -> Path:
        return (self.workspace / self.soul_file).resolve()

    @property
    def database_dir(self) -> Path:
        return Path(self.database_path).resolve().parent

    @property
    def all_writable_dirs(self) -> tuple[str, ...]:
        paths = {str(self.database_dir)}
        paths.update(self.writable_dirs)
        return tuple(sorted(paths))

    @classmethod
    def from_dict(cls, data: dict[str, object], fallback: "AssistantConfig | None" = None) -> "AssistantConfig":
        base = fallback
        assistant_id = str(data.get("assistant_id") or (base.assistant_id if base else ""))
        display_name = str(data.get("display_name") or (base.display_name if base else assistant_id))
        if not assistant_id:
            raise ValueError("assistant_id is required")
        return cls(
            assistant_id=assistant_id,
            display_name=display_name,
            workspace_path=str(data.get("workspace_path") or (base.workspace_path if base else "")),
            codex_bin=str(data.get("codex_bin") or (base.codex_bin if base else "codex")),
            codex_home=str(data.get("codex_home") or (base.codex_home if base else "")),
            python_bin=str(data.get("python_bin") or (base.python_bin if base else "python3")),
            database_path=str(data.get("database_path") or (base.database_path if base else "data/expenses.db")),
            session_store_path=str(
                data.get("session_store_path") or (base.session_store_path if base else "data/codex_sessions.json")
            ),
            codex_model=str(data.get("codex_model") or (base.codex_model if base else "")),
            codex_profile=str(data.get("codex_profile") or (base.codex_profile if base else "")),
            bridge_module=str(data.get("bridge_module") or (base.bridge_module if base else "app.bridge_ops")),
            soul_file=str(data.get("soul_file") or (base.soul_file if base else "AGENTS.md")),
            aliases=tuple(str(item) for item in (data.get("aliases") or (base.aliases if base else ()))),
            writable_dirs=tuple(str(item) for item in (data.get("writable_dirs") or (base.writable_dirs if base else ()))),
            metadata={str(k): str(v) for k, v in (data.get("metadata") or (base.metadata if base else {})).items()},
        )


class AssistantRegistry:
    """Small in-process registry for assistant definitions.

    Today we only register the family finance assistant. Later, an outer router
    can inject more assistants or replace this registry altogether.
    """

    def __init__(self) -> None:
        self._assistants: dict[str, AssistantConfig] = {}

    def register(self, config: AssistantConfig) -> None:
        self._assistants[config.assistant_id] = config

    def get(self, assistant_id: str) -> AssistantConfig:
        return self._assistants[assistant_id]

    def resolve(self, identifier: str) -> AssistantConfig | None:
        if identifier in self._assistants:
            return self._assistants[identifier]
        normalized = identifier.strip().lower()
        for assistant in self._assistants.values():
            if assistant.display_name.lower() == normalized:
                return assistant
            if any(alias.lower() == normalized for alias in assistant.aliases):
                return assistant
        return None

    def has(self, assistant_id: str) -> bool:
        return assistant_id in self._assistants

    def all(self) -> list[AssistantConfig]:
        return list(self._assistants.values())


def build_default_registry() -> AssistantRegistry:
    registry = AssistantRegistry()
    default_assistant = AssistantConfig(
        assistant_id=DEFAULT_ASSISTANT_ID,
        display_name=DEFAULT_ASSISTANT_NAME,
        workspace_path=CODEX_WORKDIR,
        codex_bin=CODEX_BIN,
        codex_home=CODEX_HOME,
        python_bin=PYTHON_BIN,
        database_path=DATABASE_PATH,
        session_store_path=CODEX_SESSION_STORE_PATH,
        codex_model=CODEX_MODEL,
        codex_profile=CODEX_PROFILE,
        aliases=("小灰毛", "finance", "family-finance"),
        metadata={
            "domain": "family-finance",
            "bridge_mode": "strict",
        },
    )
    registry.register(default_assistant)

    if not ASSISTANT_REGISTRY_PATH:
        return registry

    config_path = Path(ASSISTANT_REGISTRY_PATH)
    if not config_path.exists():
        return registry

    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return registry

    assistant_items = payload if isinstance(payload, list) else payload.get("assistants", [])
    if not isinstance(assistant_items, list):
        return registry

    for item in assistant_items:
        if not isinstance(item, dict):
            continue
        try:
            fallback = default_assistant if item.get("assistant_id") == DEFAULT_ASSISTANT_ID else None
            registry.register(AssistantConfig.from_dict(item, fallback=fallback))
        except ValueError:
            continue

    return registry


DEFAULT_ASSISTANT_REGISTRY = build_default_registry()
