"""Application configuration loaded from environment variables."""

import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")

# MiniMax
MINIMAX_API_KEY: str = os.getenv("MINIMAX_API_KEY", "")
MINIMAX_GROUP_ID: str = os.getenv("MINIMAX_GROUP_ID", "")
MINIMAX_MODEL: str = os.getenv("MINIMAX_MODEL", "abab6.5s-chat")

# MiniMax API cost control
# Monthly token limit (0 = unlimited). abab6.5s ~ ¥0.001/1k tokens
MINIMAX_MONTHLY_TOKEN_LIMIT: int = int(os.getenv("MINIMAX_MONTHLY_TOKEN_LIMIT", "500000"))

# Database
DATABASE_PATH: str = os.getenv("DATABASE_PATH", "data/expenses.db")

# Allowed Telegram user IDs (comma-separated)
_allowed = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS: list[int] = [int(uid.strip()) for uid in _allowed.split(",") if uid.strip()]

# Family members: FAMILY_MEMBERS="user_id:显示名,user_id:显示名"
# Example: FAMILY_MEMBERS="123456:老公,789012:老婆"
_members_raw = os.getenv("FAMILY_MEMBERS", "")
FAMILY_MEMBERS: dict[int, str] = {}
for _m in _members_raw.split(","):
    _m = _m.strip()
    if ":" in _m:
        _uid, _name = _m.split(":", 1)
        FAMILY_MEMBERS[int(_uid.strip())] = _name.strip()

# Timezone
TIMEZONE: str = os.getenv("TIMEZONE", "Asia/Singapore")

# Currency
CURRENCY: str = os.getenv("CURRENCY", "SGD")

# Expense categories
CATEGORIES: list[str] = ["餐饮", "交通", "购物", "娱乐", "生活", "医疗", "其他"]

# Weekly summary: day of week (0=Monday, 6=Sunday)
WEEKLY_SUMMARY_DAY: int = int(os.getenv("WEEKLY_SUMMARY_DAY", "6"))  # Sunday
WEEKLY_SUMMARY_HOUR: int = int(os.getenv("WEEKLY_SUMMARY_HOUR", "20"))  # 8 PM
