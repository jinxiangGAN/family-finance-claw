"""Application configuration loaded from environment variables."""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

# Telegram
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")

# ─── Local Codex bridge ───
BOT_BACKEND: str = os.getenv("BOT_BACKEND", "codex")
RUNTIME_PROVIDER: str = os.getenv("RUNTIME_PROVIDER", "codex")
CODEX_BIN: str = os.getenv("CODEX_BIN", "codex")
CODEX_MODEL: str = os.getenv("CODEX_MODEL", "gpt-5.4-mini")
CODEX_PROFILE: str = os.getenv("CODEX_PROFILE", "")
CODEX_REASONING_EFFORT: str = os.getenv("CODEX_REASONING_EFFORT", "low")
CODEX_HOME: str = os.getenv("CODEX_HOME", os.path.expanduser("~/.codex"))
CODEX_TIMEOUT_SECONDS: int = int(os.getenv("CODEX_TIMEOUT_SECONDS", "180"))
CODEX_WORKDIR: str = os.getenv("CODEX_WORKDIR", os.getcwd())
PYTHON_BIN: str = os.getenv("PYTHON_BIN", sys.executable)
CODEX_RUNTIME_MODE: str = os.getenv("CODEX_RUNTIME_MODE", "auto")
CODEX_SERVICE_TIER: str = os.getenv("CODEX_SERVICE_TIER", "fast")
ACTION_REGISTRY_SOCKET_PATH: str = os.getenv("ACTION_REGISTRY_SOCKET_PATH", "data/action_registry.sock")
DEFAULT_ASSISTANT_ID: str = os.getenv("DEFAULT_ASSISTANT_ID", "family-finance")
DEFAULT_ASSISTANT_NAME: str = os.getenv("DEFAULT_ASSISTANT_NAME", "小灰毛")
ASSISTANT_REGISTRY_PATH: str = os.getenv("ASSISTANT_REGISTRY_PATH", "")

# Legacy LLM settings kept for backward-compatible imports in helper modules.
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "codex")
LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
LLM_MODEL: str = os.getenv("LLM_MODEL", "")
LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "")
LLM_VISION_MODEL: str = os.getenv("LLM_VISION_MODEL", "")
LLM_EMBEDDING_MODEL: str = os.getenv("LLM_EMBEDDING_MODEL", "")
LLM_MONTHLY_TOKEN_LIMIT: int = int(os.getenv("LLM_MONTHLY_TOKEN_LIMIT", "0"))

# Database
DATABASE_PATH: str = os.getenv("DATABASE_PATH", "data/expenses.db")
CODEX_SESSION_STORE_PATH: str = os.getenv("CODEX_SESSION_STORE_PATH", "data/codex_sessions.json")
PRIVATE_CHAT_ROUTE_STORE_PATH: str = os.getenv("PRIVATE_CHAT_ROUTE_STORE_PATH", "data/private_chat_routes.json")

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

if len(FAMILY_MEMBERS) == 2:
    _sorted_ids = sorted(FAMILY_MEMBERS.keys())
    if not FAMILY_MEMBERS[_sorted_ids[0]]:
        FAMILY_MEMBERS[_sorted_ids[0]] = "小鸡毛"
    if not FAMILY_MEMBERS[_sorted_ids[1]]:
        FAMILY_MEMBERS[_sorted_ids[1]] = "小白"

# Timezone & Location
TIMEZONE: str = os.getenv("TIMEZONE", "Asia/Singapore")
LOCATION: str = os.getenv("LOCATION", "Singapore")

# Currency (default display currency)
CURRENCY: str = os.getenv("CURRENCY", "SGD")
FX_API_BASE_URL: str = os.getenv("FX_API_BASE_URL", "https://api.frankfurter.app")
FX_CACHE_TTL_SECONDS: int = int(os.getenv("FX_CACHE_TTL_SECONDS", "43200"))

# Speech-to-text
VOICE_TRANSCRIPTION_ENABLED: bool = os.getenv("VOICE_TRANSCRIPTION_ENABLED", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
VOICE_MAX_DURATION_SECONDS: int = int(os.getenv("VOICE_MAX_DURATION_SECONDS", "45"))
VOICE_TRANSCRIPTION_MODEL: str = os.getenv("VOICE_TRANSCRIPTION_MODEL", "base")
VOICE_TRANSCRIPTION_LANGUAGE: str = os.getenv("VOICE_TRANSCRIPTION_LANGUAGE", "zh")
VOICE_TRANSCRIPTION_DEVICE: str = os.getenv("VOICE_TRANSCRIPTION_DEVICE", "cpu")
VOICE_TRANSCRIPTION_COMPUTE_TYPE: str = os.getenv("VOICE_TRANSCRIPTION_COMPUTE_TYPE", "int8")

# Expense categories
CATEGORIES: list[str] = ["餐饮", "交通", "超市", "购物", "房租", "水电网", "娱乐", "医疗", "其他"]

# Weekly summary: day of week (0=Monday, 6=Sunday)
WEEKLY_SUMMARY_DAY: int = int(os.getenv("WEEKLY_SUMMARY_DAY", "6"))
WEEKLY_SUMMARY_HOUR: int = int(os.getenv("WEEKLY_SUMMARY_HOUR", "20"))

# Memory settings
MEMORY_MAX_WORKING: int = int(os.getenv("MEMORY_MAX_WORKING", "10"))
MEMORY_RECALL_TOP_K: int = int(os.getenv("MEMORY_RECALL_TOP_K", "3"))
