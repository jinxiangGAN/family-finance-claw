"""Three-tier Memory Manager — inspired by human cognitive architecture.

Architecture:
  Tier 1 · core_profile    — persistent user identity, goals, preferences (DB: core_profiles)
  Tier 2 · working_memory  — current conversation context, ephemeral (in-memory)
  Tier 3 · episodic_memory — past events, decisions, patterns (DB: episodic_memories + embedding)

Recall strategy:
  1. core_profile: always injected (it defines "who the user is")
  2. working_memory: always injected (recent turns keep coherence)
  3. episodic_memory: recalled by semantic similarity (vector cosine) with FTS5 fallback
"""

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from zoneinfo import ZoneInfo

from app.config import MEMORY_MAX_WORKING, MEMORY_RECALL_TOP_K, TIMEZONE
from app.database import get_connection
from app.core.llm_provider import (
    LLMProvider,
    cosine_similarity,
    pack_embedding,
    unpack_embedding,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════
#  Data structures
# ═══════════════════════════════════════════

@dataclass
class CoreProfile:
    """User's persistent identity profile (key-value pairs)."""
    user_id: int
    data: dict[str, str] = field(default_factory=dict)

    def get(self, key: str, default: str = "") -> str:
        return self.data.get(key, default)

    def to_prompt(self) -> str:
        """Render core profile as a prompt fragment."""
        if not self.data:
            return ""
        lines = ["[Core Profile]"]
        for k, v in self.data.items():
            lines.append(f"- {k}: {v}")
        return "\n".join(lines)


@dataclass
class WorkingMemory:
    """Ephemeral conversation context (in-memory, not persisted)."""
    messages: list[dict] = field(default_factory=list)
    max_turns: int = MEMORY_MAX_WORKING

    def add_turn(self, role: str, content: str) -> None:
        """Add a conversation turn, auto-trim old messages."""
        self.messages.append({"role": role, "content": content})
        # Keep only the last N turns (user + assistant pairs)
        if len(self.messages) > self.max_turns * 2:
            self.messages = self.messages[-(self.max_turns * 2):]

    def get_messages(self) -> list[dict]:
        return list(self.messages)

    def clear(self) -> None:
        self.messages.clear()

    def to_prompt(self) -> str:
        """Summarize working memory for context injection."""
        if not self.messages:
            return ""
        lines = ["[Working Context — Recent Conversation]"]
        for msg in self.messages[-6:]:  # Last 3 turns
            role_label = "User" if msg["role"] == "user" else "Assistant"
            lines.append(f"{role_label}: {msg['content'][:100]}{'...' if len(msg['content']) > 100 else ''}")
        return "\n".join(lines)


@dataclass
class EpisodicMemory:
    """A single episodic memory record."""
    id: int
    user_id: int
    content: str
    category: str
    importance: int
    created_at: str
    similarity: float = 0.0  # set during recall


# ═══════════════════════════════════════════
#  MemoryManager
# ═══════════════════════════════════════════

class MemoryManager:
    """Orchestrates the 3-tier memory system.

    Usage:
        mm = MemoryManager(provider)              # provider = LLMProvider (for embeddings)
        profile = mm.load_profile(uid)             # Tier 1: core_profile
        wm = mm.get_working_memory(uid, chat_id)  # Tier 2: working_memory
        episodes = await mm.recall(uid, q)         # Tier 3: episodic vector recall
    """

    def __init__(self, provider: Optional[LLMProvider] = None, embedding_model: str = ""):
        self._provider = provider
        self._embedding_model = embedding_model
        # Per-(user, chat) working memory — isolates private vs group context
        self._working: dict[tuple[int, int], WorkingMemory] = defaultdict(WorkingMemory)

    # ─── Tier 1: Core Profile ───

    def load_profile(self, user_id: int) -> CoreProfile:
        """Load persistent profile from DB."""
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT key, value FROM core_profiles WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        data = {r["key"]: r["value"] for r in rows}
        return CoreProfile(user_id=user_id, data=data)

    def update_profile(self, user_id: int, key: str, value: str) -> None:
        """Upsert a core profile entry."""
        tz = ZoneInfo(TIMEZONE)
        now = datetime.now(tz).isoformat()
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO core_profiles (user_id, key, value, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (user_id, key, value, now),
            )
            conn.commit()
        logger.info("Profile updated: user=%d key=%s value=%s", user_id, key, value[:60])

    def delete_profile_key(self, user_id: int, key: str) -> bool:
        """Delete a profile entry."""
        with get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM core_profiles WHERE user_id = ? AND key = ?",
                (user_id, key),
            )
            conn.commit()
        return cursor.rowcount > 0

    def get_all_profile_keys(self, user_id: int) -> list[dict]:
        """List all profile keys for a user."""
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT key, value, updated_at FROM core_profiles WHERE user_id = ? ORDER BY key",
                (user_id,),
            ).fetchall()
        return [{"key": r["key"], "value": r["value"], "updated_at": r["updated_at"]} for r in rows]

    # ─── Tier 2: Working Memory ───

    def get_working_memory(self, user_id: int, chat_id: int = 0) -> WorkingMemory:
        """Get (or create) in-memory working buffer for a (user, chat) pair.

        Using (user_id, chat_id) as key ensures private chat context
        never leaks into group chat and vice versa.
        """
        return self._working[(user_id, chat_id)]

    def add_working_turn(self, user_id: int, chat_id: int, role: str, content: str) -> None:
        self._working[(user_id, chat_id)].add_turn(role, content)

    def clear_working_memory(self, user_id: int, chat_id: int = 0) -> None:
        key = (user_id, chat_id)
        if key in self._working:
            self._working[key].clear()

    # ─── Tier 3: Episodic Memory ───

    async def store_episode(
        self,
        user_id: int,
        content: str,
        category: str = "general",
        importance: int = 5,
    ) -> int:
        """Store an episodic memory with optional embedding."""
        tz = ZoneInfo(TIMEZONE)
        now = datetime.now(tz).isoformat()
        importance = min(max(importance, 1), 10)

        # Try to generate embedding
        embedding_blob: Optional[bytes] = None
        if self._provider and self._embedding_model:
            vec = await self._provider.embed(content, model=self._embedding_model)
            if vec:
                embedding_blob = pack_embedding(vec)

        with get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO episodic_memories (user_id, content, category, importance, embedding, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, content, category, importance, embedding_blob, now),
            )
            memory_id = cursor.lastrowid
            # Also index in FTS5
            try:
                conn.execute(
                    "INSERT INTO episodic_fts (rowid, content) VALUES (?, ?)",
                    (memory_id, content),
                )
            except Exception:
                logger.debug("FTS insert failed for memory #%d", memory_id)
            conn.commit()

        logger.info("Stored episode #%d for user %d: %s", memory_id, user_id, content[:60])
        return memory_id  # type: ignore[return-value]

    async def recall_episodes(
        self,
        user_id: int,
        query: str,
        limit: int = MEMORY_RECALL_TOP_K,
        include_shared: bool = True,
    ) -> list[EpisodicMemory]:
        """Recall episodic memories by semantic similarity.

        Strategy:
        1. If embedding model is available → vector cosine search
        2. Fallback → FTS5 full-text search
        3. Last resort → LIKE search
        """
        user_filter_ids = [user_id]
        if include_shared:
            user_filter_ids.append(0)
        placeholders = ",".join("?" * len(user_filter_ids))

        # ── Vector recall ──
        if self._provider and self._embedding_model:
            query_vec = await self._provider.embed(query, model=self._embedding_model)
            if query_vec:
                return self._vector_recall(query_vec, user_filter_ids, placeholders, limit)

        # ── FTS5 recall ──
        fts_results = self._fts_recall(query, user_filter_ids, placeholders, limit)
        if fts_results:
            return fts_results

        # ── LIKE fallback ──
        return self._like_recall(query, user_filter_ids, placeholders, limit)

    def _vector_recall(
        self,
        query_vec: list[float],
        user_ids: list[int],
        placeholders: str,
        limit: int,
    ) -> list[EpisodicMemory]:
        """Brute-force cosine similarity search over stored embeddings."""
        with get_connection() as conn:
            rows = conn.execute(
                f"SELECT id, user_id, content, category, importance, embedding, created_at "
                f"FROM episodic_memories WHERE user_id IN ({placeholders}) AND embedding IS NOT NULL "
                f"ORDER BY importance DESC",
                user_ids,
            ).fetchall()

        scored: list[tuple[float, dict]] = []
        for r in rows:
            stored_vec = unpack_embedding(r["embedding"])
            sim = cosine_similarity(query_vec, stored_vec)
            scored.append((sim, dict(r)))

        scored.sort(key=lambda x: x[0], reverse=True)

        results = []
        for sim, row in scored[:limit]:
            results.append(EpisodicMemory(
                id=row["id"],
                user_id=row["user_id"],
                content=row["content"],
                category=row["category"],
                importance=row["importance"],
                created_at=row["created_at"],
                similarity=sim,
            ))
        logger.debug("Vector recall: %d results (top sim=%.3f)", len(results), results[0].similarity if results else 0)
        return results

    def _fts_recall(
        self,
        query: str,
        user_ids: list[int],
        placeholders: str,
        limit: int,
    ) -> list[EpisodicMemory]:
        """Full-text search recall via FTS5."""
        terms = [t.strip() for t in query.split() if len(t.strip()) >= 2]
        if not terms:
            return []
        fts_query = " OR ".join(terms)

        try:
            with get_connection() as conn:
                rows = conn.execute(
                    f"SELECT m.id, m.user_id, m.content, m.category, m.importance, m.created_at, rank "
                    f"FROM episodic_fts f "
                    f"JOIN episodic_memories m ON m.id = f.rowid "
                    f"WHERE episodic_fts MATCH ? AND m.user_id IN ({placeholders}) "
                    f"ORDER BY m.importance DESC, rank "
                    f"LIMIT ?",
                    (fts_query, *user_ids, limit),
                ).fetchall()

            return [
                EpisodicMemory(
                    id=r["id"],
                    user_id=r["user_id"],
                    content=r["content"],
                    category=r["category"],
                    importance=r["importance"],
                    created_at=r["created_at"],
                )
                for r in rows
            ]
        except Exception:
            logger.debug("FTS5 recall failed", exc_info=True)
            return []

    def _like_recall(
        self,
        query: str,
        user_ids: list[int],
        placeholders: str,
        limit: int,
    ) -> list[EpisodicMemory]:
        """Last-resort LIKE search."""
        terms = [t.strip() for t in query.split() if len(t.strip()) >= 2]
        if not terms:
            return []

        like_clauses = " OR ".join(["m.content LIKE ?" for _ in terms])
        like_params = [f"%{t}%" for t in terms]

        with get_connection() as conn:
            rows = conn.execute(
                f"SELECT m.id, m.user_id, m.content, m.category, m.importance, m.created_at "
                f"FROM episodic_memories m "
                f"WHERE ({like_clauses}) AND m.user_id IN ({placeholders}) "
                f"ORDER BY m.importance DESC, m.created_at DESC "
                f"LIMIT ?",
                (*like_params, *user_ids, limit),
            ).fetchall()

        return [
            EpisodicMemory(
                id=r["id"],
                user_id=r["user_id"],
                content=r["content"],
                category=r["category"],
                importance=r["importance"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def delete_episode(self, memory_id: int) -> bool:
        """Delete an episodic memory."""
        with get_connection() as conn:
            try:
                conn.execute("DELETE FROM episodic_fts WHERE rowid = ?", (memory_id,))
            except Exception:
                pass
            cursor = conn.execute("DELETE FROM episodic_memories WHERE id = ?", (memory_id,))
            conn.commit()
        return cursor.rowcount > 0

    def get_recent_episodes(self, user_id: int, limit: int = 10) -> list[EpisodicMemory]:
        """Get most recent episodic memories (for /memory listing)."""
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT id, user_id, content, category, importance, created_at "
                "FROM episodic_memories "
                "WHERE user_id IN (?, 0) "
                "ORDER BY importance DESC, created_at DESC "
                "LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [
            EpisodicMemory(
                id=r["id"],
                user_id=r["user_id"],
                content=r["content"],
                category=r["category"],
                importance=r["importance"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # ─── Tier 3 intent gate ───
    # Only trigger expensive Embedding / FTS recall when the query
    # looks like a lookup, analysis, or reflection.  Pure expense
    # recording ("吃饭 20") and trivial filler ("哈哈") skip Tier 3,
    # keeping latency low and API costs down.
    _RECALL_TRIGGERS = re.compile(
        r"(上次|之前|以前|过去|历史|总共|花了多少|汇总|分析|趋势|对比|统计"
        r"|记得|回忆|目标|计划|省钱|节约|建议|规划|怎么样|如何"
        r"|预算|超支|习惯|偏好|消费模式|最近|这段时间"
        r"|你还记得|帮我想想|忘掉|忘记)"
    )

    # ─── Assembly: combine all tiers for prompt ───

    async def assemble_memory_context(self, user_id: int, query: str, chat_id: int = 0) -> str:
        """Assemble a combined memory prompt from all three tiers."""
        parts: list[str] = []

        # Tier 1: Core Profile — always injected (cheap, local DB read)
        profile = self.load_profile(user_id)
        profile_text = profile.to_prompt()
        if profile_text:
            parts.append(profile_text)

        # Tier 2: Working Memory — always injected (in-memory, zero cost)
        wm = self.get_working_memory(user_id, chat_id)
        wm_text = wm.to_prompt()
        if wm_text:
            parts.append(wm_text)

        # Tier 3: Episodic recall — only when the query signals a lookup intent.
        # This avoids calling the Embedding API + vector search for trivial
        # messages like "哈哈", "好的", or simple expense recordings.
        if self._RECALL_TRIGGERS.search(query):
            episodes = await self.recall_episodes(user_id, query)
            if episodes:
                lines = ["[Relevant Episodic Memories]"]
                for ep in episodes:
                    prefix = "🔴" if ep.importance >= 8 else "🟡" if ep.importance >= 5 else "🟢"
                    sim_tag = f" (similarity:{ep.similarity:.0%})" if ep.similarity > 0 else ""
                    lines.append(f"{prefix} [{ep.category}] {ep.content}{sim_tag}")
                parts.append("\n".join(lines))
                logger.debug("Tier 3 recall triggered for: %s", query[:60])
        else:
            logger.debug("Tier 3 recall skipped (no recall intent): %s", query[:60])

        return "\n\n".join(parts) if parts else ""


# ═══════════════════════════════════════════
#  Legacy compatibility wrappers
#  (called by old code paths / fallback)
# ═══════════════════════════════════════════

_default_manager: Optional[MemoryManager] = None


def get_memory_manager() -> MemoryManager:
    """Get or create the global MemoryManager singleton."""
    global _default_manager
    if _default_manager is None:
        _default_manager = MemoryManager()
    return _default_manager


def set_memory_manager(manager: MemoryManager) -> None:
    """Replace the global MemoryManager (called during agent init with provider)."""
    global _default_manager
    _default_manager = manager


def store_memory(user_id: int, content: str, category: str = "general", importance: int = 5) -> int:
    """Legacy sync wrapper — stores into episodic_memories (without embedding)."""
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz).isoformat()
    importance = min(max(importance, 1), 10)
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO episodic_memories (user_id, content, category, importance, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, content, category, importance, now),
        )
        memory_id = cursor.lastrowid
        try:
            conn.execute("INSERT INTO episodic_fts (rowid, content) VALUES (?, ?)", (memory_id, content))
        except Exception:
            pass
        conn.commit()
    return memory_id  # type: ignore[return-value]


def recall_memories(user_id: int, query: str, limit: int = 5) -> list[dict]:
    """Legacy sync wrapper — FTS-based recall."""
    mm = get_memory_manager()
    # sync path: use FTS directly
    episodes = mm._fts_recall(
        query,
        [user_id, 0],
        "?,?",
        limit,
    )
    if not episodes:
        episodes = mm._like_recall(query, [user_id, 0], "?,?", limit)
    return [
        {"id": e.id, "content": e.content, "category": e.category, "importance": e.importance}
        for e in episodes
    ]


def delete_memory(memory_id: int) -> bool:
    """Legacy wrapper."""
    return get_memory_manager().delete_episode(memory_id)


def get_recent_memories(user_id: int, limit: int = 10) -> list[dict]:
    """Legacy wrapper — returns recent episodic memories as dicts."""
    mm = get_memory_manager()
    episodes = mm.get_recent_episodes(user_id, limit)
    return [
        {"id": e.id, "content": e.content, "category": e.category, "importance": e.importance}
        for e in episodes
    ]


def format_memories_for_prompt(memories: list[dict]) -> str:
    """Legacy wrapper."""
    if not memories:
        return ""
    lines = ["以下是你记住的关于这个家庭的重要信息："]
    for m in memories:
        prefix = "🔴" if m.get("importance", 5) >= 8 else "🟡" if m.get("importance", 5) >= 5 else "🟢"
        lines.append(f"{prefix} [{m.get('category', 'general')}] {m['content']}")
    return "\n".join(lines)
