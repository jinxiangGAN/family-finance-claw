"""MCP Tool: Enhanced memory system — 3-tier architecture.

Tools:
  - store_memory       → save an episodic memory (Tier 3)
  - recall_memories    → search episodic memories
  - forget_memory      → delete a specific memory
  - update_user_profile → upsert core profile (Tier 1) — Agent auto-calls this
  - get_user_profile   → read user's core profile
"""

from app.memory import delete_memory, get_memory_manager, recall_memories, store_memory


# ═══════════════════════════════════════════
#  Handlers
# ═══════════════════════════════════════════

def _handle_store_memory(user_id: int, user_name: str, params: dict) -> dict:
    """Store a new episodic memory about the user/family."""
    content = params.get("content", "").strip()
    if not content:
        return {"success": False, "message": "记忆内容不能为空"}

    category = params.get("category", "general")
    importance = int(params.get("importance", 5))
    target_uid = 0 if params.get("shared", False) else user_id

    memory_id = store_memory(target_uid, content, category, importance)
    return {
        "success": True,
        "memory_id": memory_id,
        "message": f"已记住：{content[:50]}{'...' if len(content) > 50 else ''}",
    }


def _handle_recall_memories(user_id: int, user_name: str, params: dict) -> dict:
    """Recall relevant episodic memories."""
    query = params.get("query", "").strip()
    if not query:
        return {"success": False, "memories": [], "message": "请提供检索关键词"}

    memories = recall_memories(user_id, query, limit=5)
    return {
        "success": True,
        "memories": [m["content"] for m in memories],
        "count": len(memories),
    }


def _handle_forget_memory(user_id: int, user_name: str, params: dict) -> dict:
    """Delete a specific memory."""
    memory_id = int(params.get("memory_id", 0))
    if memory_id <= 0:
        return {"success": False, "message": "请提供记忆ID"}
    deleted = delete_memory(memory_id)
    if deleted:
        return {"success": True, "message": f"已遗忘记忆 #{memory_id}"}
    return {"success": False, "message": f"未找到记忆 #{memory_id}"}


def _handle_update_user_profile(user_id: int, user_name: str, params: dict) -> dict:
    """Update the user's core profile (Tier 1).

    Called by the Agent when it detects a change in the user's
    financial goals, preferences, or personality traits.
    """
    key = params.get("key", "").strip()
    value = params.get("value", "").strip()
    if not key or not value:
        return {"success": False, "message": "key 和 value 不能为空"}

    mm = get_memory_manager()
    mm.update_profile(user_id, key, value)
    return {
        "success": True,
        "message": f"已更新用户画像：{key} = {value[:60]}",
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


# ═══════════════════════════════════════════
#  Tool definitions (OpenAI function-calling schema)
# ═══════════════════════════════════════════

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "store_memory",
            "description": (
                "记住一条重要信息（存入长期记忆）。当用户表达偏好、设定目标、做出决定、或提到重要家庭事项时调用。"
                "例如：'这个月要减少打车'、'周末不在外面吃饭'、'下个月要存3000'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "要记住的内容"},
                    "category": {
                        "type": "string",
                        "description": "分类",
                        "enum": ["preference", "goal", "decision", "habit", "reminder", "general"],
                    },
                    "importance": {
                        "type": "integer",
                        "description": "重要程度 1-10（10=非常重要）",
                    },
                    "shared": {
                        "type": "boolean",
                        "description": "是否为家庭共享记忆（true=两人都能看到）",
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
            "description": "回忆相关的历史信息。当需要参考之前的讨论、决定或偏好时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget_memory",
            "description": "忘记一条记忆。用户说'忘掉XX'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "integer", "description": "记忆的ID"},
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
                "更新用户的核心画像。当你察觉到用户的财务目标、消费偏好、或生活方式发生了变化时主动调用。"
                "例如：用户说'我们决定年底去日本旅行'→ key='近期目标', value='为年底日本旅行存钱'。"
                "用户说'我最近开始自己做饭了'→ key='饮食偏好', value='偏好自己做饭，减少外食'。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": (
                            "画像维度（如：'近期目标'、'饮食偏好'、'交通习惯'、'存钱计划'、"
                            "'收入水平'、'风险偏好'、'家庭状况'）"
                        ),
                    },
                    "value": {"type": "string", "description": "画像描述"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_profile",
            "description": "查看用户的核心画像（财务目标、偏好等）。",
            "parameters": {
                "type": "object",
                "properties": {},
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
}
