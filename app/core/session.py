"""Session management — tracks per-user state and chat context.

Enhanced v4 features:
- Integrates with MemoryManager for working memory tracking
- Persona switching based on chat_type (private/group)
- Session timeout detection for working memory cleanup

Each session carries:
- chat_type: 'private' | 'group' | 'supergroup'
- user_id / user_name / display_name
- is_private flag for PromptBuilder
- interaction_count (for proactive engagement triggers)
"""

import logging
import json
import time as _time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from zoneinfo import ZoneInfo

from app.config import DEFAULT_ASSISTANT_ID, FAMILY_MEMBERS, PRIVATE_CHAT_ROUTE_STORE_PATH, TIMEZONE

logger = logging.getLogger(__name__)

# Working memory is cleared after this many seconds of inactivity
SESSION_TIMEOUT_SECONDS = 1800  # 30 minutes


@dataclass
class Session:
    """Represents a user's current session context."""

    user_id: int
    user_name: str
    chat_id: int
    chat_type: str  # "private" | "group" | "supergroup"
    display_name: str = ""
    assistant_id: str = DEFAULT_ASSISTANT_ID
    interaction_count: int = 0
    last_active: str = ""
    _last_active_ts: float = field(default_factory=_time.time, repr=False)

    def __post_init__(self):
        if not self.display_name:
            self.display_name = FAMILY_MEMBERS.get(self.user_id, self.user_name)
        if not self.last_active:
            tz = ZoneInfo(TIMEZONE)
            self.last_active = datetime.now(tz).isoformat()

    @property
    def is_private(self) -> bool:
        return self.chat_type == "private"

    @property
    def is_group(self) -> bool:
        return self.chat_type in ("group", "supergroup")

    def touch(self) -> None:
        """Update last-active timestamp and increment interaction count."""
        tz = ZoneInfo(TIMEZONE)
        self.last_active = datetime.now(tz).isoformat()
        self._last_active_ts = _time.time()
        self.interaction_count += 1

    def is_expired(self) -> bool:
        """Check if the session has been inactive beyond the timeout."""
        return (_time.time() - self._last_active_ts) > SESSION_TIMEOUT_SECONDS


# ═══════════════════════════════════════════
#  Session Store
# ═══════════════════════════════════════════

# Keyed by (user_id, chat_id) to support separate sessions per chat
_sessions: dict[tuple[int, int], Session] = {}
_private_chat_routes: dict[int, int] = {}


def _load_private_chat_routes() -> dict[int, int]:
    path = Path(PRIVATE_CHAT_ROUTE_STORE_PATH)
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        routes: dict[int, int] = {}
        for user_id, chat_id in data.items():
            routes[int(user_id)] = int(chat_id)
        return routes
    except Exception:
        logger.exception("Failed to load private chat routes from %s", path)
        return {}


def _save_private_chat_routes() -> None:
    path = Path(PRIVATE_CHAT_ROUTE_STORE_PATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        serializable = {str(user_id): chat_id for user_id, chat_id in _private_chat_routes.items()}
        path.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("Failed to save private chat routes to %s", path)


_private_chat_routes.update(_load_private_chat_routes())


def get_or_create_session(
    user_id: int,
    user_name: str,
    chat_id: int,
    chat_type: str,
) -> Session:
    """Get existing session or create a new one.

    If the session has expired (30 min idle), it's recreated
    and the caller should clear working memory.
    """
    key = (user_id, chat_id)
    session = _sessions.get(key)

    if session is None or session.is_expired():
        session = Session(
            user_id=user_id,
            user_name=user_name,
            chat_id=chat_id,
            chat_type=chat_type,
        )
        _sessions[key] = session
        if chat_type == "private":
            _private_chat_routes[user_id] = chat_id
            _save_private_chat_routes()
        logger.debug("New session for user %d in chat %d (type=%s)", user_id, chat_id, chat_type)
        return session

    # Update existing session
    session.chat_type = chat_type
    session.touch()
    if chat_type == "private":
        _private_chat_routes[user_id] = chat_id
        _save_private_chat_routes()
    return session


def get_active_session_count() -> int:
    """Return count of non-expired sessions."""
    return sum(1 for s in _sessions.values() if not s.is_expired())


def reset_session(user_id: int, chat_id: int) -> None:
    """Remove the current session so the next turn starts fresh."""
    _sessions.pop((user_id, chat_id), None)


def get_private_chat_route(user_id: int) -> Optional[int]:
    """Return the most recently seen private chat id for a user."""
    return _private_chat_routes.get(user_id)


def remember_private_chat_route(user_id: int, chat_id: int) -> None:
    """Persist a private chat route discovered during runtime."""
    _private_chat_routes[user_id] = chat_id
    _save_private_chat_routes()


# ═══════════════════════════════════════════
#  Legacy compatibility
# ═══════════════════════════════════════════

def build_system_prompt_for_session(session: Session, base_prompt: str, memories_text: str) -> str:
    """Legacy wrapper — kept for backward compat with old code paths.

    New code should use PromptBuilder directly.
    """
    parts = [base_prompt]

    if memories_text:
        parts.append(f"\n{memories_text}")

    if session.is_private:
        parts.append(
            f"\n当前对话场景：私聊（{session.display_name}）\n"
            "回复风格：温暖、贴心，可以用更感性的语气给出建议。"
            "可以主动关心对方的消费习惯，适当给出鼓励或温馨提醒。"
            f"称呼用户为「{session.display_name}」。"
        )
    elif session.is_group:
        parts.append(
            "\n当前对话场景：家庭群聊\n"
            "回复风格：客观、简洁。播报数据时用家庭视角。"
            "不要过于感性，保持中立和专业。"
            "如果涉及个人消费细节，注意隐私，不要在群里展示过多个人信息。"
        )

    return "\n".join(parts)
