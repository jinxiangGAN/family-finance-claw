"""Application configuration loaded from environment variables."""

import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")

# ─── LLM Provider ───
# Supported: openai, minimax, deepseek, qwen, gemini, custom
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "gemini")
LLM_API_KEY: str = os.getenv("LLM_API_KEY", "") or os.getenv("MINIMAX_API_KEY", "")
LLM_MODEL: str = os.getenv("LLM_MODEL", "") or os.getenv("MINIMAX_MODEL", "gemini-3-flash-preview")
LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "")
# Vision model (for receipt OCR) — defaults to same as LLM_MODEL
LLM_VISION_MODEL: str = os.getenv("LLM_VISION_MODEL", "")
# Embedding model — for semantic memory vector search
# Leave empty to use FTS5 fallback (no embedding API call)
LLM_EMBEDDING_MODEL: str = os.getenv("LLM_EMBEDDING_MODEL", "")

# Backward compat
MINIMAX_API_KEY: str = LLM_API_KEY
MINIMAX_MODEL: str = LLM_MODEL

# Monthly token limit (0 = unlimited)
LLM_MONTHLY_TOKEN_LIMIT: int = int(os.getenv("LLM_MONTHLY_TOKEN_LIMIT", "0") or os.getenv("MINIMAX_MONTHLY_TOKEN_LIMIT", "500000"))

# Database
DATABASE_PATH: str = os.getenv("DATABASE_PATH", "data/expenses.db")

# Allowed Telegram user IDs (comma-separated)
_allowed = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS: list[int] = [int(uid.strip()) for uid in _allowed.split(",") if uid.strip()]

# Family members: FAMILY_MEMBERS="user_id:显示名,user_id:显示名"
_members_raw = os.getenv("FAMILY_MEMBERS", "")
FAMILY_MEMBERS: dict[int, str] = {}
for _m in _members_raw.split(","):
    _m = _m.strip()
    if ":" in _m:
        _uid, _name = _m.split(":", 1)
        FAMILY_MEMBERS[int(_uid.strip())] = _name.strip()

# Timezone & Location
TIMEZONE: str = os.getenv("TIMEZONE", "Asia/Singapore")
LOCATION: str = os.getenv("LOCATION", "Singapore")

# Currency (default display currency)
CURRENCY: str = os.getenv("CURRENCY", "SGD")

# Expense categories
CATEGORIES: list[str] = ["餐饮", "交通", "购物", "娱乐", "生活", "医疗", "其他"]

# Weekly summary: day of week (0=Monday, 6=Sunday)
WEEKLY_SUMMARY_DAY: int = int(os.getenv("WEEKLY_SUMMARY_DAY", "6"))
WEEKLY_SUMMARY_HOUR: int = int(os.getenv("WEEKLY_SUMMARY_HOUR", "20"))

# Memory settings
MEMORY_MAX_WORKING: int = int(os.getenv("MEMORY_MAX_WORKING", "10"))  # max conversation turns
MEMORY_RECALL_TOP_K: int = int(os.getenv("MEMORY_RECALL_TOP_K", "3"))  # top-K episodic recall
