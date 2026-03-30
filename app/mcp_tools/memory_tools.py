"""MCP Tool: Enhanced memory system — 3-tier architecture.

Tools:
  - store_memory       → save an episodic memory (Tier 3) with embedding
  - recall_memories    → search episodic memories (vector + FTS)
  - forget_memory      → delete a specific memory
  - update_user_profile → upsert core profile (Tier 1) — Agent auto-calls this
  - get_user_profile   → read user's core profile
"""

from app.core.memory import delete_memory, get_memory_manager, get_recent_memories


# ═══════════════════════════════════════════
#  Handlers (async where embedding is needed)
# ═══════════════════════════════════════════

async def _handle_store_memory(user_id: int, user_name: str, params: dict) -> dict:
    """Store a new episodic memory WITH embedding (async)."""
    content = params.get("content", "").strip()
    if not content:
        return {"success": False, "message": "Memory content cannot be empty"}

    category = params.get("category", "general")
    importance = int(params.get("importance", 5))
    target_uid = 0 if params.get("shared", False) else user_id

    mm = get_memory_manager()
    memory_id = await mm.store_episode(target_uid, content, category, importance)
    return {
        "success": True,
        "memory_id": memory_id,
        "message": f"Stored: {content[:50]}{'...' if len(content) > 50 else ''}",
    }


async def _handle_recall_memories(user_id: int, user_name: str, params: dict) -> dict:
    """Recall relevant episodic memories (vector similarity + FTS fallback, async)."""
    query = params.get("query", "").strip()
    if not query:
        return {"success": False, "memories": [], "message": "Please provide a search query"}

    mm = get_memory_manager()
    episodes = await mm.recall_episodes(user_id, query, limit=5)
    return {
        "success": True,
        "memories": [ep.content for ep in episodes],
        "count": len(episodes),
    }


def _handle_forget_memory(user_id: int, user_name: str, params: dict) -> dict:
    """Delete a specific memory."""
    memory_id = int(params.get("memory_id", 0))
    if memory_id <= 0:
        return {"success": False, "message": "Please provide a valid memory_id"}
    deleted = delete_memory(memory_id)
    if deleted:
        return {"success": True, "message": f"Deleted memory #{memory_id}"}
    return {"success": False, "message": f"Memory #{memory_id} not found"}


def _handle_update_user_profile(user_id: int, user_name: str, params: dict) -> dict:
    """Update the user's core profile (Tier 1).

    Called by the Agent when it detects a change in the user's
    financial goals, preferences, or personality traits.
    """
    key = params.get("key", "").strip()
    value = params.get("value", "").strip()
    shared = bool(params.get("shared", False))
    if not key or not value:
        return {"success": False, "message": "Both key and value are required"}

    mm = get_memory_manager()
    mm.update_profile(user_id, key, value, shared=shared)
    return {
        "success": True,
        "message": f"Updated {'family' if shared else 'personal'} profile: {key} = {value[:60]}",
    }


def _handle_get_user_profile(user_id: int, user_name: str, params: dict) -> dict:
    """Read the user's core profile."""
    mm = get_memory_manager()
    entries = mm.get_all_profile_keys(user_id)
    return {
        "success": True,
        "profile": entries,
        "count": len(entries),
    }


def _handle_get_recent_memories(user_id: int, user_name: str, params: dict) -> dict:
    """Read recent episodic memories from the database."""
    limit = min(max(int(params.get("limit", 10)), 1), 30)
    memories = get_recent_memories(user_id, limit=limit)
    return {
        "success": True,
        "memories": memories,
        "count": len(memories),
    }


# ═══════════════════════════════════════════
#  Tool definitions (OpenAI function-calling schema)
# ═══════════════════════════════════════════

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "store_memory",
            "description": (
                "Store an important piece of information into long-term episodic memory (Tier 3). "
                "Call this proactively when the user expresses preferences, sets financial goals, "
                "makes family decisions, or mentions important life events. "
                "Examples: 'reduce taxi this month', 'no eating out on weekdays', 'save 3000 next month'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "The information to remember"},
                    "category": {
                        "type": "string",
                        "description": "Memory category",
                        "enum": ["preference", "goal", "decision", "habit", "reminder", "general"],
                    },
                    "importance": {
                        "type": "integer",
                        "description": "Importance level 1-10 (10 = critical)",
                    },
                    "shared": {
                        "type": "boolean",
                        "description": "If true, this memory is shared across both family members",
                    },
                },
                "required": ["content", "category", "importance"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_memories",
            "description": "Search and retrieve relevant episodic memories. Call when you need to reference past discussions, decisions, or preferences.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query keywords"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget_memory",
            "description": "Delete a specific episodic memory by its ID. Call when the user explicitly asks to forget something.",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "integer", "description": "The ID of the memory to delete"},
                },
                "required": ["memory_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_user_profile",
            "description": (
                "Update the user's core profile (Tier 1 — persistent identity). "
                "Call this proactively when you detect a shift in the user's financial goals, spending preferences, or lifestyle. "
                "Example: User says 'We decided to travel to Japan at year-end' → key='recent_goal', value='Save for year-end Japan trip'. "
                "Example: User says 'I started cooking at home' → key='diet_preference', value='Prefers home cooking, reducing eating out'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": (
                            "Profile dimension key (e.g., 'recent_goal', 'diet_preference', 'transport_habit', "
                            "'saving_plan', 'income_level', 'risk_preference', 'family_status')"
                        ),
                    },
                    "value": {"type": "string", "description": "Profile value description"},
                    "shared": {"type": "boolean", "description": "If true, write this into the family-shared profile"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_profile",
            "description": "Retrieve the user's core profile including financial goals, preferences, and traits.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_memories",
            "description": "Retrieve the most recent episodic memories already stored in the database.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "How many recent memories to return, default 10"},
                },
            },
        },
    },
]

HANDLERS = {
    "store_memory": _handle_store_memory,
    "recall_memories": _handle_recall_memories,
    "forget_memory": _handle_forget_memory,
    "update_user_profile": _handle_update_user_profile,
    "get_user_profile": _handle_get_user_profile,
    "get_recent_memories": _handle_get_recent_memories,
}
