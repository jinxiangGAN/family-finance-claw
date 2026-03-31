"""Assistant routing helpers for future multi-assistant orchestration."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.config import DEFAULT_ASSISTANT_ID
from app.core.assistant_registry import AssistantRegistry, DEFAULT_ASSISTANT_REGISTRY
from app.core.session import Session

_INLINE_ROUTE_RE = re.compile(r"^\s*@([A-Za-z0-9._-]+)\s+(.+?)\s*$")
_LABEL_ROUTE_RE = re.compile(r"^\s*(?:助手|assistant)\s*[:：]\s*([A-Za-z0-9._\-\u4e00-\u9fff]+)\s+(.+?)\s*$", re.IGNORECASE)
_COMMAND_ROUTE_RE = re.compile(r"^\s*/assistant\s+([A-Za-z0-9._-]+)\s+(.+?)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class AssistantRoute:
    assistant_id: str
    message_text: str
    explicit: bool = False
    source: str = "default"
    unknown_identifier: str = ""


class AssistantRouter:
    def __init__(self, registry: AssistantRegistry | None = None) -> None:
        self.registry = registry or DEFAULT_ASSISTANT_REGISTRY

    def resolve(self, text: str, session: Session) -> AssistantRoute:
        stripped = text.strip()
        if not stripped:
            return AssistantRoute(assistant_id=session.assistant_id, message_text=text)

        for pattern, source in (
            (_COMMAND_ROUTE_RE, "command"),
            (_LABEL_ROUTE_RE, "label"),
            (_INLINE_ROUTE_RE, "inline"),
        ):
            match = pattern.match(stripped)
            if not match:
                continue
            identifier, remainder = match.group(1).strip(), match.group(2).strip()
            assistant = self.registry.resolve(identifier)
            if assistant is None:
                return AssistantRoute(
                    assistant_id=session.assistant_id,
                    message_text=remainder,
                    explicit=True,
                    source=source,
                    unknown_identifier=identifier,
                )
            session.assistant_id = assistant.assistant_id
            return AssistantRoute(
                assistant_id=assistant.assistant_id,
                message_text=remainder,
                explicit=True,
                source=source,
            )

        if not self.registry.has(session.assistant_id):
            session.assistant_id = DEFAULT_ASSISTANT_ID
        return AssistantRoute(
            assistant_id=session.assistant_id,
            message_text=text,
            explicit=False,
            source="sticky",
        )


DEFAULT_ASSISTANT_ROUTER = AssistantRouter()
