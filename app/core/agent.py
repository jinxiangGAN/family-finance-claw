"""Local Codex bridge for Telegram messages.

This replaces the old remote LLM provider flow. Every Telegram message is
forwarded to the local Codex CLI, which can inspect this repository and use the
existing Python skills/database helpers to manage expenses.
"""

import json
import logging
import re
from datetime import datetime
from typing import Any, Optional

from zoneinfo import ZoneInfo

from app.core.memory import delete_memory, get_memory_manager, get_recent_memories, store_memory, update_memory
from app.config import (
    ACTION_REGISTRY_SOCKET_PATH,
    CURRENCY,
    DATABASE_PATH,
    FAMILY_MEMBERS,
    LOCATION,
    PYTHON_BIN,
    TIMEZONE,
)
from app.core.resident_agent import DEFAULT_RESIDENT_AGENT_SERVICE
from app.core.observability import log_event
from app.core.session import Session
from app.mcp_tools.registry import execute_tool
from app.services.expense_service import get_expenses, get_recent_expenses

logger = logging.getLogger(__name__)

_SESSION_HISTORY: dict[tuple[int, int], list[dict[str, str]]] = {}
_PENDING_MEMORY: dict[tuple[int, int], dict[str, object]] = {}
_PENDING_ACTION: dict[tuple[int, int], dict[str, str]] = {}
_MAX_HISTORY_TURNS = 6
_MAX_DB_RECENT_EXPENSES = 5
_MAX_DB_RECENT_MEMORIES = 5

_RECORD_LIKE_RE = re.compile(
    r"^\s*(?P<note>.*?\D)\s*(?P<amount>\d+(?:\.\d+)?)(?:\s*(?P<currency>[A-Za-z]{3}|元|块|人民币))?\s*$"
)
_RECORD_PREFIX_RE = re.compile(r"^\s*(?:记一笔|记账|入账|记下|记一下)\s*[，,:：]?\s*")
_RECORD_OWNER_SUFFIX_RE = re.compile(
    r"\s*[，,]?\s*(?:我花的|我付的|我出的|是我花的|小鸡毛花的|小鸡毛付的|小鸡毛出的|小白花的|小白付的|小白出的)\s*$"
)
_MEMORY_PATTERNS: list[tuple[re.Pattern[str], str, int]] = [
    (re.compile(r"(目标|计划|打算|争取|要存|想存|少花|省钱|控制预算|减少开销)"), "goal", 7),
    (re.compile(r"(喜欢|不喜欢|偏好|习惯|通常|尽量|以后|不再|少坐|少点外卖|多做饭)"), "preference", 6),
    (re.compile(r"(决定|商量好了|定了|以后我们|这个月我们|接下来我们)"), "decision", 7),
]
_FINANCE_HINT_TOKENS = (
    "花",
    "开销",
    "消费",
    "支出",
    "预算",
    "记账",
    "报销",
    "固定账单",
    "每月",
    "对比",
    "上个月",
    "异常",
    "欠谁",
    "AA",
    "垫付",
    "结算",
    "目标",
    "进度",
    "房租",
    "机票",
    "签证",
    "明细",
    "汇总",
    "汇率",
    "统计",
    "账单",
    "删除",
    "撤销",
    "多少钱",
    "多少",
    "总共",
    "最近",
    "上个月",
    "这个月",
    "旅行",
    "计划",
)
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_ARCHIVE_MEMORY_RE = re.compile(r"^\s*(?:忘掉|忘记|归档)\s*(?:记忆)?\s*#?(\d+)\s*$")
_UPDATE_MEMORY_RE = re.compile(r"^\s*(?:把)?记忆\s*#?(\d+)\s*(?:改成|更新成|修改为|替换为)\s*(.+?)\s*$")
_DELETE_BY_DESC_RE = re.compile(
    r"^\s*删除\s*(\d+(?:\.\d+)?)\s*(?:块|元|rmb|cny|sgd|usd)?\s*([^#\n\r]*)?(?:那笔|这一笔|那条|这条)?\s*$",
    re.IGNORECASE,
)
_BUDGET_SET_RE = re.compile(
    r"^\s*([\u4e00-\u9fffA-Za-z_]+)\s*预算(?:\s*(?:设为|改成|改为|调整为))?\s*(\d+(?:\.\d+)?)(?:\s*(?:[A-Za-z]{3}|元|块|人民币))?\s*$"
)
_WRITE_ACTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?:^|\s)(?:删除|删掉|撤销)(?:最近一笔|上一笔|#?\d+|这笔|那笔)?"), "delete an expense record"),
    (re.compile(r"(?:开始|创建|开启).*(?:旅行|计划|专项|事件)"), "create or activate a special plan"),
    (re.compile(r"(?:结束|关闭).*(?:旅行|计划|专项|事件)"), "close a special plan"),
]
_ACTION_FUNCTION_HINTS: dict[str, tuple[str, str]] = {
    "record an expense": ("record_expense", "create a new expense record in the database"),
    "record an expense from an image": ("record_expense", "extract receipt details and create a new expense record"),
    "delete an expense record": ("delete_last_expense / delete_expense_by_id", "remove or roll back an existing expense record"),
    "update a budget": ("set_budget", "create or update a monthly family budget"),
    "create or activate a special plan": ("start_event", "create or activate a special event/project plan"),
    "close a special plan": ("stop_event", "close the current special event/project plan"),
    "archive a memory": ("forget_memory", "archive an old memory so it no longer participates as active memory"),
    "update a memory": ("update_memory", "archive the old memory and create a new replacement version"),
}

_DELETE_BY_ID_RE = re.compile(r"^\s*删除\s*#?(\d+)\s*$")
_RECENT_EXPENSES_RE = re.compile(r"^\s*(?:看看|看下|查看)?最近\s*(\d+)?\s*笔(?:账|开销|消费|记录)?\s*$")
_MONTH_TOTAL_RE = re.compile(
    r"^\s*(?:查看|看看|查下|查一下)?(?:[\u4e00-\u9fffA-Za-z_]+的?)?(?:这个月|本月)(?:[\u4e00-\u9fffA-Za-z_]+的?)?(?:我|我们|家庭|全家)?(?:总共)?(?:花费|花销|开销|支出|消费|花了多少|一共花了多少|多少)\s*[？?]?\s*$"
)
_TODAY_TOTAL_RE = re.compile(
    r"^\s*(?:查看|看看|查下|查一下)?(?:[\u4e00-\u9fffA-Za-z_]+的?)?(?:今日|今天)(?:[\u4e00-\u9fffA-Za-z_]+的?)?(?:我|我们|家庭|全家)?(?:所有)?(?:花费|花销|开销|支出|消费|花了多少|一共花了多少|多少)\s*[？?]?\s*$"
)
_DETAIL_QUERY_RE = re.compile(
    r"^\s*(?:查看|看看|查下|查一下)?(?:[\u4e00-\u9fffA-Za-z_]+的?)?(?:今天|今日|本月|这个月|最近)?(?:花费|花销|开销|支出|消费)?(?:明细|细则)\s*[？?]?\s*$"
)
_BUDGET_QUERY_RE = re.compile(
    r"^\s*(?:"
    r"(?:看看|看下|查看|查下|查一下)?(?:当前)?预算(?:还剩多少|剩多少|情况|怎么样|列表)?"
    r"|有哪些预算"
    r"|我(?:们)?的预算(?:情况|呢|有哪些)?"
    r"|(?:[\u4e00-\u9fffA-Za-z_]+)预算(?:是多少|多少|还剩多少|怎么样|情况)?"
    r")\s*[？?]?\s*$"
)
_FORWARD_MESSAGE_PATTERNS = [
    re.compile(
        r"^\s*(?:帮我)?给\s*(?P<target>[^\s,，:：]+)\s*(?:发消息|发|带句话|说一声|说)\s*[:：,，]?\s*(?P<body>.+?)\s*$"
    ),
    re.compile(
        r"^\s*(?:发消息给|发给|转告|转发给|跟)\s*(?P<target>[^\s,，:：]+)\s*(?:说|讲一下|带句话)?\s*[:：,，]?\s*(?P<body>.+?)\s*$"
    ),
]
_FAST_WORKBENCH_INTENTS = {
    "exchange_rate",
    "delete_by_id",
    "forward_message",
}

_FAST_INTENT_ACTIONS: dict[str, str] = {
    "exchange_rate": "finance.exchange_rate",
    "delete_by_id": "finance.delete_by_id",
    "forward_message": "family.forward_message",
}
_SHORT_QUERY_SKILL_NAMES = {
    "query_today_total",
    "query_today_items",
    "query_monthly_total",
    "query_category_total",
    "query_category_items",
    "query_recent_expenses",
    "query_summary",
    "query_budget",
}
_EXCHANGE_RATE_HINTS = (
    "人民币",
    "美元",
    "欧元",
    "英镑",
    "日元",
    "澳元",
    "马币",
    "令吉",
    "泰铢",
    "韩元",
    "新币",
    "新加坡元",
    "SGD",
    "USD",
    "CNY",
    "RMB",
    "EUR",
    "GBP",
    "JPY",
    "AUD",
    "MYR",
    "THB",
    "KRW",
)
_ACTION_TAG_RE = re.compile(r"<ACTION>\s*(\{.*?\})\s*</ACTION>", re.DOTALL)
_FINAL_TAG_RE = re.compile(r"<FINAL>\s*(.*?)\s*</FINAL>", re.DOTALL)
_DEFAULT_FULL_PATH_ACTION_STEPS = 8
_WRITE_FULL_PATH_ACTION_STEPS = 10
_IMAGE_FULL_PATH_ACTION_STEPS = 10
_MEMORY_FULL_PATH_ACTION_STEPS = 9
_BUDGET_FULL_PATH_ACTION_STEPS = 12


def _remember_turn(user_id: int, chat_id: int, role: str, content: str) -> None:
    key = (user_id, chat_id)
    history = _SESSION_HISTORY.setdefault(key, [])
    history.append({"role": role, "content": content})
    if len(history) > _MAX_HISTORY_TURNS * 2:
        _SESSION_HISTORY[key] = history[-(_MAX_HISTORY_TURNS * 2):]


def _get_recent_history(user_id: int, chat_id: int) -> list[dict[str, str]]:
    return list(_SESSION_HISTORY.get((user_id, chat_id), []))


def _reset_session_history(user_id: int, chat_id: int, assistant_id: str) -> None:
    _SESSION_HISTORY.pop((user_id, chat_id), None)
    _PENDING_MEMORY.pop((user_id, chat_id), None)
    _PENDING_ACTION.pop((user_id, chat_id), None)
    DEFAULT_RESIDENT_AGENT_SERVICE.reset(user_id=user_id, chat_id=chat_id, assistant_id=assistant_id)


def _thread_owner_id(user_id: int, session: Session) -> int:
    return 0 if session.is_group else user_id


def _normalize_record_like_text(text: str) -> str:
    stripped = _RECORD_PREFIX_RE.sub("", text.strip())
    stripped = _RECORD_OWNER_SUFFIX_RE.sub("", stripped)
    return stripped.strip()


def _looks_like_record_expense(text: str) -> bool:
    normalized = _normalize_record_like_text(text)
    if not normalized:
        return False
    if "?" in normalized or "？" in normalized:
        return False
    if _looks_like_budget_write(normalized):
        return False
    return bool(_RECORD_LIKE_RE.match(normalized))


def _looks_like_budget_write(text: str) -> bool:
    stripped = text.strip()
    if "预算" not in stripped:
        return False
    if _looks_like_budget_query(stripped):
        return False
    return bool(re.search(r"\d+(?:\.\d+)?", stripped))


def _looks_like_budget_query(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    return bool(_BUDGET_QUERY_RE.match(stripped))


def _looks_like_short_query_turn(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if _looks_like_record_expense(stripped) or _looks_like_budget_write(stripped):
        return False
    if any(
        pattern.match(stripped)
        for pattern in (_RECENT_EXPENSES_RE, _TODAY_TOTAL_RE, _MONTH_TOTAL_RE, _DETAIL_QUERY_RE, _BUDGET_QUERY_RE)
    ):
        return True
    if any(token in stripped for token in ("花费", "花销", "开销", "支出", "消费", "明细", "细则", "汇总", "预算")) and any(
        token in stripped for token in ("今天", "今日", "本月", "这个月", "最近", "小白", "小鸡毛", "全家", "家庭", "多少", "看看", "查看", "查")
    ):
        return True
    return False


def reset_agent_context(user_id: int, chat_id: int, *, assistant_id: str = "family-finance", is_group: bool = False) -> None:
    """Public helper to clear short-term bridge context for one chat."""
    owner_id = 0 if is_group else user_id
    _reset_session_history(owner_id, chat_id, assistant_id)


def _format_history(history: list[dict[str, str]]) -> str:
    if not history:
        return "None"
    lines: list[str] = []
    for item in history:
        role = "User" if item["role"] == "user" else "Assistant"
        lines.append(f"- {role}: {item['content']}")
    return "\n".join(lines)


def _format_db_snapshot(user_id: int) -> str:
    mm = get_memory_manager()
    profile_entries = mm.get_all_profile_keys(user_id)
    recent_memories = get_recent_memories(user_id, limit=_MAX_DB_RECENT_MEMORIES)
    recent_expenses = get_recent_expenses(user_id, limit=_MAX_DB_RECENT_EXPENSES)

    lines = ["[Database Snapshot]"]

    if recent_expenses:
        lines.append("Recent expense records:")
        for exp in recent_expenses:
            lines.append(
                f"- #{exp.id} {exp.category} {exp.amount:.2f} {exp.currency} "
                f"[{exp.ledger_type}] note:{exp.note or 'none'} "
                f"event:{exp.event_tag or 'none'} created_at:{exp.created_at}"
            )
    else:
        lines.append("Recent expense records: none")

    if recent_memories:
        lines.append("Recent memories:")
        for memory in recent_memories:
            scope = "family" if memory.get("scope") == "family" else "personal"
            lines.append(
                f"- #{memory['id']} [{scope}/{memory['category']}] {memory['content']} "
                f"(importance={memory['importance']})"
            )
    else:
        lines.append("Recent memories: none")

    if profile_entries:
        lines.append("Current profile:")
        for entry in profile_entries:
            scope = "family" if entry.get("scope") == "family" else "personal"
            lines.append(f"- [{scope}] {entry['key']}: {entry['value']}")
    else:
        lines.append("Current profile: none")

    return "\n".join(lines)


def _build_prompt_context(
    *,
    user_id: int,
    thread_owner_id: int,
    text: str,
    session: Session,
    image_path: Optional[str],
) -> tuple[str, str, str]:
    chat_mode = _detect_chat_mode(text, image_path=image_path)
    history_items = _get_recent_history(thread_owner_id, session.chat_id)

    if image_path or _looks_like_record_expense(text):
        history = "None"
        db_snapshot = "Omitted for fast expense handling. Use the resident action registry for any facts you need."
        return chat_mode, history, db_snapshot

    if chat_mode == "chat":
        history = _format_history(history_items[-4:])
        db_snapshot = "Omitted in chat mode unless you explicitly need finance facts from the resident action registry."
        return chat_mode, history, db_snapshot

    history = _format_history(history_items[-4:])
    mm = get_memory_manager()
    profile_entries = mm.get_all_profile_keys(user_id)[:3]
    recent_memories = get_recent_memories(user_id, limit=2)
    recent_expenses = get_recent_expenses(user_id, limit=2)
    lines = ["[Compact Database Snapshot]"]

    if recent_expenses:
        lines.append("Recent expense records:")
        for exp in recent_expenses:
            lines.append(f"- #{exp.id} {exp.category} {exp.amount:.2f} {exp.currency} [{exp.ledger_type}]")
    else:
        lines.append("Recent expense records: none")

    if recent_memories:
        lines.append("Recent memories:")
        for memory in recent_memories:
            scope = "family" if memory.get("scope") == "family" else "personal"
            lines.append(f"- #{memory['id']} [{scope}/{memory['category']}] {memory['content']}")
    else:
        lines.append("Recent memories: none")

    if profile_entries:
        lines.append("Current profile:")
        for entry in profile_entries:
            scope = "family" if entry.get("scope") == "family" else "personal"
            lines.append(f"- [{scope}] {entry['key']}: {entry['value']}")

    return chat_mode, history, "\n".join(lines)


def _looks_like_query(text: str) -> bool:
    return any(
        token in text
        for token in (
            "多少",
            "明细",
            "汇总",
            "分析",
            "预算",
            "汇率",
            "记得",
            "上次",
            "之前",
            "查询",
            "导出",
            "还剩",
            "统计",
            "回顾",
            "看下",
        )
    )


def _detect_chat_mode(text: str, image_path: Optional[str] = None) -> str:
    stripped = text.strip()
    if image_path:
        return "finance"
    if not stripped:
        return "chat"
    if _looks_like_record_expense(stripped):
        return "finance"
    if any(token in stripped for token in _FINANCE_HINT_TOKENS):
        return "finance"
    if re.search(r"\d+(?:\.\d+)?", stripped):
        return "finance"
    return "chat"


def _detect_write_action(text: str, image_path: Optional[str] = None) -> Optional[str]:
    stripped = text.strip()
    if not stripped:
        return None
    if _looks_like_budget_write(stripped):
        return "update a budget"
    if _ARCHIVE_MEMORY_RE.match(stripped):
        return "archive a memory"
    if _UPDATE_MEMORY_RE.match(stripped):
        return "update a memory"
    for pattern, label in _WRITE_ACTION_PATTERNS:
        if pattern.search(stripped):
            return label
    return None


def _family_user_ids_for_chat(user_id: int) -> list[int]:
    member_ids = list(FAMILY_MEMBERS.keys())
    if not member_ids:
        return [user_id]
    if user_id not in member_ids:
        member_ids.append(user_id)
    return member_ids


def _matches_delete_hint(expense, hint: str) -> bool:
    normalized = hint.strip().lower()
    if not normalized:
        return True
    haystacks = [
        expense.note.lower(),
        expense.category.lower(),
        expense.user_name.lower(),
        expense.event_tag.lower(),
    ]
    return any(normalized in hay for hay in haystacks if hay)


def _find_delete_candidates(text: str, user_id: int) -> list[dict[str, object]]:
    match = _DELETE_BY_DESC_RE.match(text.strip())
    if not match:
        return []

    amount = float(match.group(1))
    hint = (match.group(2) or "").strip()
    candidates = []
    expenses = get_expenses(user_ids=_family_user_ids_for_chat(user_id), limit=30)
    for expense in expenses:
        if abs(float(expense.amount) - amount) > 0.01:
            continue
        if not _matches_delete_hint(expense, hint):
            continue
        candidates.append({
            "id": expense.id,
            "user_name": expense.user_name,
            "category": expense.category,
            "amount": expense.amount,
            "currency": expense.currency,
            "note": expense.note,
            "created_at": expense.created_at,
        })
    return candidates


def _looks_like_memory_candidate(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 6 or len(stripped) > 120:
        return False
    if "?" in stripped or "？" in stripped:
        return False
    if _looks_like_record_expense(stripped):
        return False
    if _looks_like_query(stripped):
        return False
    return any(pattern.search(stripped) for pattern, _, _ in _MEMORY_PATTERNS)


def _detect_memory_candidate(user_id: int, text: str) -> Optional[dict[str, object]]:
    stripped = text.strip()
    if not _looks_like_memory_candidate(stripped):
        return None

    recent_memories = get_recent_memories(user_id, limit=10)
    if any(m["content"].strip() == stripped for m in recent_memories):
        return {
            "duplicate": True,
            "content": stripped,
        }

    for pattern, category, importance in _MEMORY_PATTERNS:
        if not pattern.search(stripped):
            continue
        target_user_id = 0 if any(token in stripped for token in ("我们", "全家", "家里", "老婆", "老公")) else user_id
        return {
            "content": stripped,
            "category": category,
            "importance": importance,
            "target_user_id": target_user_id,
            "shared": target_user_id == 0,
        }
    return None


def _is_yes_confirmation(text: str) -> bool:
    stripped = text.strip().lower()
    return stripped in {
        "是",
        "好",
        "好的",
        "好呀",
        "好啊",
        "行",
        "行啊",
        "可以",
        "记吧",
        "记住",
        "要",
        "yes",
        "y",
    }


def _is_no_confirmation(text: str) -> bool:
    stripped = text.strip().lower()
    return stripped in {
        "不",
        "不用",
        "不要",
        "先不用",
        "不需要",
        "no",
        "n",
    }


def _remember_and_reply(user_id: int, chat_id: int, user_text: str, reply: str) -> str:
    _remember_turn(user_id, chat_id, "user", user_text)
    _remember_turn(user_id, chat_id, "assistant", reply)
    return reply


def _contains_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text))


async def _normalize_memory_to_english(content: str, category: str) -> Optional[str]:
    """Rewrite a memory candidate into concise English for storage."""
    stripped = content.strip()
    if not stripped:
        return None
    if not _contains_cjk(stripped):
        return stripped

    prompt = f"""Rewrite the following memory into concise natural English for database storage.

Requirements:
- Output only the English memory sentence.
- Keep the meaning intact.
- Keep it short and specific.
- Do not add quotes, labels, bullets, explanations, or markdown.
- Category: {category}

Original memory:
{stripped}
"""
    rewritten = (await _run_codex(prompt, user_id=0, chat_id=0, assistant_id="family-finance")).strip()
    if not rewritten:
        return None
    if _contains_cjk(rewritten):
        return None
    return rewritten


async def _maybe_handle_memory_admin(text: str, user_id: int) -> Optional[str]:
    """Handle direct memory archive/update commands before Codex routing."""
    archive_match = _ARCHIVE_MEMORY_RE.match(text.strip())
    if archive_match:
        memory_id = int(archive_match.group(1))
        deleted = delete_memory(memory_id)
        if deleted:
            return f"好，我已经把记忆 #{memory_id} 归档了。"
        return f"我没有找到仍处于 active 状态的记忆 #{memory_id}。"

    update_match = _UPDATE_MEMORY_RE.match(text.strip())
    if not update_match:
        return None

    memory_id = int(update_match.group(1))
    new_content_raw = update_match.group(2).strip()
    normalized_content = await _normalize_memory_to_english(new_content_raw, "general")
    if not normalized_content:
        return "这条记忆我这次没能稳定转换成英文摘要，所以先没有更新。你可以换个说法再试一次。"

    new_memory_id = await update_memory(memory_id, normalized_content)
    if new_memory_id is None:
        return f"我没有找到仍处于 active 状态的记忆 #{memory_id}。"

    return (
        f"好，我已经更新这条记忆了。\n"
        f"旧版本：#{memory_id} -> archived\n"
        f"新版本：#{new_memory_id} -> {normalized_content}"
    )


def _maybe_build_delete_candidate_reply(text: str, user_id: int) -> Optional[str]:
    candidates = _find_delete_candidates(text, user_id)
    if not candidates:
        return None
    if len(candidates) == 1:
        item = candidates[0]
        return (
            "我找到 1 条匹配的账目：\n"
            f"#{item['id']} {item['user_name']} / {item['category']} {item['amount']:.2f} {item['currency']} "
            f"/ 备注：{item['note'] or '无'}\n"
            "如果要删除它，请回复：`删除 #"
            f"{item['id']}`"
        )

    lines = ["我找到几条可能匹配的账目："]
    for item in candidates[:5]:
        lines.append(
            f"#{item['id']} {item['user_name']} / {item['category']} {item['amount']:.2f} {item['currency']} "
            f"/ 备注：{item['note'] or '无'}"
        )
    lines.append("请回复要删除的 ID，例如：`删除 #123`")
    return "\n".join(lines)


def _build_action_confirmation(action_label: str, original_text: str) -> str:
    function_name, function_purpose = _ACTION_FUNCTION_HINTS.get(
        action_label,
        ("relevant skill", "apply the requested write operation"),
    )
    return (
        f"小灰毛理解到这次是想：{action_label}。\n"
        f"预计会调用：`{function_name}`\n"
        f"这个函数会负责：{function_purpose}\n"
        f"原始内容：{original_text}\n"
        "要继续的话，回“小灰毛，继续”或者直接回“是”；"
        "如果先不做，回“不要”就行。"
    )


def _detect_fast_finance_intent(text: str, image_path: Optional[str] = None) -> Optional[str]:
    stripped = text.strip()
    if image_path:
        return None
    if any(pattern.match(stripped) for pattern in _FORWARD_MESSAGE_PATTERNS):
        return "forward_message"
    if _DELETE_BY_ID_RE.match(stripped):
        return "delete_by_id"
    if _looks_like_exchange_rate_query(stripped):
        return "exchange_rate"
    return None


def _rewrite_scope_followup(text: str, history: list[dict[str, str]]) -> str:
    stripped = text.strip()
    if not stripped:
        return stripped
    if any(token in stripped for token in ("包括小白", "算上小白", "加上小白", "带上小白")):
        for item in reversed(history):
            if item.get("role") != "user":
                continue
            previous = str(item.get("content") or "").strip()
            if _TODAY_TOTAL_RE.match(previous):
                return "查看今天全家的花费"
            if _MONTH_TOTAL_RE.match(previous):
                return "查看本月全家的花费"
    if any(token in stripped for token in ("包括小鸡毛", "算上小鸡毛", "加上小鸡毛", "带上小鸡毛")):
        for item in reversed(history):
            if item.get("role") != "user":
                continue
            previous = str(item.get("content") or "").strip()
            if _TODAY_TOTAL_RE.match(previous):
                return "查看今天全家的花费"
            if _MONTH_TOTAL_RE.match(previous):
                return "查看本月全家的花费"
    if any(token in stripped for token in ("只看小白", "只查小白", "只要小白", "不要全家，只看小白", "不要全家只看小白")):
        for item in reversed(history):
            if item.get("role") != "user":
                continue
            previous = str(item.get("content") or "").strip()
            if _TODAY_TOTAL_RE.match(previous):
                return "查看小白今天花费"
            if _MONTH_TOTAL_RE.match(previous):
                return "查看小白本月花费"
            if _DETAIL_QUERY_RE.match(previous):
                return "查看小白的花费明细"
    if any(token in stripped for token in ("只看小鸡毛", "只查小鸡毛", "只要小鸡毛", "不要全家，只看小鸡毛", "不要全家只看小鸡毛")):
        for item in reversed(history):
            if item.get("role") != "user":
                continue
            previous = str(item.get("content") or "").strip()
            if _TODAY_TOTAL_RE.match(previous):
                return "查看小鸡毛今天花费"
            if _MONTH_TOTAL_RE.match(previous):
                return "查看小鸡毛本月花费"
            if _DETAIL_QUERY_RE.match(previous):
                return "查看小鸡毛的花费明细"
    return stripped


def _looks_like_exchange_rate_query(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    upper = stripped.upper()
    hint_hits = sum(1 for hint in _EXCHANGE_RATE_HINTS if hint in stripped or hint in upper)
    if "汇率" in stripped and hint_hits >= 1:
        return True
    if any(token in stripped for token in ("兑", "等于", "/")) and hint_hits >= 2:
        return True
    return False


def _build_prompt(
    text: str,
    user_id: int,
    user_name: str,
    session: Session,
    image_path: Optional[str] = None,
    caption: str = "",
) -> str:
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    thread_owner_id = _thread_owner_id(user_id, session)

    if session.interaction_count == 0:
        _reset_session_history(thread_owner_id, session.chat_id, session.assistant_id)

    chat_mode, history, db_snapshot = _build_prompt_context(
        user_id=user_id,
        thread_owner_id=thread_owner_id,
        text=text,
        session=session,
        image_path=image_path,
    )
    family = ", ".join(f"{name}(id:{uid})" for uid, name in FAMILY_MEMBERS.items()) or "not configured"
    chat_kind = "private chat" if session.is_private else "group chat"

    user_block = text.strip() or "The user sent an image. Interpret it with the image context."
    image_hint = ""
    if image_path:
        image_hint = (
            f"\nImage file: {image_path}\n"
            f"Image caption: {caption or 'none'}\n"
            "If this is a receipt or bill image, use it to complete expense handling. "
            "If it is not a recognizable receipt, reply politely and say so."
        )

    return f"""You are `小灰毛`, the local Codex executor behind a Telegram family finance bot.

Handle one Telegram message using the existing repo, SQLite database, and the approved bridge helpers.

Rules:
1. Finance and memory facts must come from approved resident bridge actions / approved database reads and writes in this turn.
2. Do not modify repo source code, do not write ad-hoc SQL, and do not use random execution entrypoints.
3. Output only the final Telegram reply in Simplified Chinese.
4. If details are unclear for safe finance handling, ask one concise follow-up question.
4a. For a simple expense record, if the user does not explicitly name a different owner, default the expense owner to the current sender. Do not ask them to restate "我花的" just to confirm the owner.
5. In group chat, answer from a family perspective and avoid oversharing personal detail.
6. In private chat, you may sound warmer and more personal.
7. If the user is only chatting, reply naturally and briefly. Do not invent finance facts.
8. If the user asks for amounts, history, budgets, memories, or trends, you must ground them in the database in this turn.
9. For stable preferences, goals, habits, or family decisions, ask for confirmation before storing memory.
10. In full-path execution, prefer the resident bridge action loop for grounded actions.
11. The resident action registry is a host-side fast path and the resident bridge actions are the full-path execution surface.
12. Treat `regular` as day-to-day spending and `special` as project/event spending unless the user explicitly asks to include both.
13. If the user wants to delete an expense, prefer checking recent expenses and then deleting by id.
14. In chat mode, `小灰毛` should feel like a real household companion:
   - warm
   - lively
   - gently playful
   - emotionally attentive
   - never stiff or corporate
15. In chat mode, prefer natural human reactions over assistant-like phrasing. Acknowledge mood first, then respond.
16. It is okay for `小灰毛` to sound cute, bright, or lightly cheeky, but avoid sounding exaggerated, flirty, roleplay-heavy, or overly verbose.
17. When it feels natural in family conversation, prefer using `小鸡毛` or `小白` instead of generic `你`.
18. Even in factual replies, keep the tone soft and companion-like rather than tool-like.
19. Emojis are welcome in Telegram replies when they help the message feel warm and lively.
20. The paw-print emoji is a signature touch for `小灰毛`, but it should appear mainly in playful or lively endings rather than in every reply.
21. In finance replies, keep emojis light so the facts stay easy to scan, and do not mechanically add the paw-print emoji to factual or operational replies.

Environment:
- Database path: {DATABASE_PATH}
- Default currency: {CURRENCY}
- Timezone: {TIMEZONE}
- Location: {LOCATION}
- Family members: {family}

Context:
- Current time: {now}
- Active reply mode: {chat_mode}
- User ID: {user_id}
- User name: {user_name}
- Chat type: {chat_kind}
- Recent conversation:
{history}
- Current database snapshot:
{db_snapshot}
{image_hint}

Current user message:
{user_block}
"""


def _extract_action_request(reply: str) -> Optional[dict[str, Any]]:
    match = _ACTION_TAG_RE.search(reply or "")
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _extract_final_reply(reply: str) -> Optional[str]:
    match = _FINAL_TAG_RE.search(reply or "")
    if match:
        return match.group(1).strip()
    cleaned = (reply or "").strip()
    if cleaned and "<ACTION>" not in cleaned:
        return cleaned
    return None


def _format_action_result(result: dict[str, Any]) -> str:
    try:
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception:
        return str(result)


def _build_resident_full_path_prompt(
    *,
    text: str,
    user_id: int,
    user_name: str,
    session: Session,
    image_path: Optional[str] = None,
    caption: str = "",
    last_action_result: Optional[dict[str, Any]] = None,
    step_index: int = 0,
    step_limit: int = _DEFAULT_FULL_PATH_ACTION_STEPS,
) -> str:
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    thread_owner_id = _thread_owner_id(user_id, session)
    chat_mode, history, _ = _build_prompt_context(
        user_id=user_id,
        thread_owner_id=thread_owner_id,
        text=text,
        session=session,
        image_path=image_path if step_index == 0 else None,
    )
    family = ", ".join(f"{name}(id:{uid})" for uid, name in FAMILY_MEMBERS.items()) or "not configured"
    chat_kind = "private chat" if session.is_private else "group chat"
    is_plain_expense_turn = bool(not image_path and _looks_like_record_expense(text))

    image_hint = ""
    if image_path and step_index == 0:
        image_hint = (
            f"\nImage file: {image_path}\n"
            f"Image caption: {caption or 'none'}\n"
            "If the image is a receipt, use it to decide the next resident action."
        )

    action_result_block = ""
    if last_action_result is not None:
        action_result_block = (
            "\nPrevious resident action result (JSON):\n"
            f"{_format_action_result(last_action_result)}\n"
            "Use it to choose the next action or finish with the final reply.\n"
        )

    expense_hint = ""
    if is_plain_expense_turn:
        expense_hint = (
            "\nThis message looks like a plain expense record.\n"
            "For a plain expense record:\n"
            "- do not call `bridge.snapshot` unless the owner/category is truly ambiguous\n"
            "- prefer issuing exactly one `bridge.skill` call with `record_expense`\n"
            "- after the action result comes back, finish immediately with `<FINAL>`\n"
            "- do not loop through extra analysis when one expense record is enough\n"
        )

    return f"""You are `小灰毛`, the resident Codex brain behind a Telegram family finance bot.

Handle exactly one Telegram user request. Telegram is only the terminal. You are the thinking layer.

You have exactly two output modes and must choose one:
1. A resident action request:
<ACTION>{{"kind":"bridge.snapshot"}}</ACTION>
<ACTION>{{"kind":"bridge.skill","name":"record_expense","params":{{"category":"餐饮","amount":20,"currency":"SGD","note":"午饭"}}}}</ACTION>
<ACTION>{{"kind":"bridge.skill","name":"record_expense","params":{{"category":"餐饮","amount":20,"currency":"SGD","note":"午饭","owner_user_id":{{小白的用户id}},"owner_user_name":"小白"}}}}</ACTION>
<ACTION>{{"kind":"bridge.skill","name":"query_summary","params":{{"scope":"me"}}}}</ACTION>
<ACTION>{{"kind":"bridge.skill","name":"query_exchange_rate","params":{{"base_currency":"USD","quote_currency":"SGD"}}}}</ACTION>
<ACTION>{{"kind":"bridge.skill","name":"set_budget","params":{{"categories":["餐饮","交通","超市"],"budget_name":"三项日常预算","amount":2000}}}}</ACTION>
<ACTION>{{"kind":"bridge.skill","name":"set_recurring_rule","params":{{"name":"房租","category":"房租","amount":4500,"due_day":1,"shared":true}}}}</ACTION>
<ACTION>{{"kind":"bridge.skill","name":"query_period_comparison","params":{{"scope":"family"}}}}</ACTION>
<ACTION>{{"kind":"bridge.skill","name":"query_balance_status","params":{{"event_tag":"日本旅行"}}}}</ACTION>
<ACTION>{{"kind":"bridge.skill","name":"query_spending_anomalies","params":{{"scope":"me"}}}}</ACTION>
<ACTION>{{"kind":"bridge.skill","name":"set_spending_goal","params":{{"category":"餐饮","target_amount":500}}}}</ACTION>
<ACTION>{{"kind":"bridge.skill","name":"query_goal_progress","params":{{}}}}</ACTION>
<ACTION>{{"kind":"bridge.store_memory","content":"Reduce takeout spending on weekdays.","category":"goal","importance":7,"shared":false}}</ACTION>
2. A final Telegram reply:
<FINAL>...</FINAL>

Rules:
1. Do not output shell commands, Python commands, SQL, markdown code fences, or process narration.
2. For finance and memory facts, use resident actions instead of guessing.
3. Use one resident action at a time. After receiving an action result, either issue the next action or finish.
4. Use `bridge.snapshot` when you need recent expenses, recent memories, or profile context from the database.
5. Use `bridge.skill` for finance/business actions such as query, budget, event, summary, recurring bills, balances, anomalies, goals, and expense operations.
5a. If the user wants one shared budget across multiple categories, call `set_budget` with a `categories` list and optional `budget_name` instead of recording an expense.
5b. `query_budget` results may contain both `budgets` and `budget_groups`. If `budget_groups` is present, mention the grouped/shared budgets explicitly in the final reply.
6. Use `bridge.store_memory` only after the user has already confirmed the memory should be saved. Memory content must be concise English.
7. If the user is only chatting, you may reply directly with `<FINAL>` and no action.
8. Final replies must be in Simplified Chinese only.
9. Keep the final reply short, warm, and grounded. Prefer `小鸡毛` / `小白` when it sounds natural.
10. If details are ambiguous for a safe write, ask one concise clarification question inside `<FINAL>`.
10a. For a simple expense record, if no different owner is explicitly named, default the owner to the current sender instead of asking them to restate that it was their spending.
10b. If the user explicitly says the expense belongs to `小白` or `小鸡毛`, include `owner_user_id` and `owner_user_name` in the `record_expense` params.
11. This is resident full-path step {step_index + 1} of {step_limit}. Avoid unnecessary loops.
12. Emojis are welcome when they make the reply feel warm and lively. The paw-print emoji is a signature touch for `小灰毛`, but it should mainly appear in playful or lively endings rather than in every reply.
13. In factual finance replies, keep emoji usage light and readable.

Environment:
- Database path: {DATABASE_PATH}
- Default currency: {CURRENCY}
- Timezone: {TIMEZONE}
- Location: {LOCATION}
- Family members: {family}

Context:
- Current time: {now}
- Active reply mode: {chat_mode}
- User ID: {user_id}
- User name: {user_name}
- Chat type: {chat_kind}
- Recent conversation:
{history}
{image_hint}
{expense_hint}
{action_result_block}

Current user message:
{text.strip() or "The user sent an image."}
"""


def _determine_full_path_step_limit(*, text: str, image_path: Optional[str]) -> int:
    stripped = text.strip()
    if image_path:
        return _IMAGE_FULL_PATH_ACTION_STEPS
    if _looks_like_budget_write(stripped) or "预算" in stripped:
        return _BUDGET_FULL_PATH_ACTION_STEPS
    if _looks_like_memory_candidate(stripped) or _ARCHIVE_MEMORY_RE.match(stripped) or _UPDATE_MEMORY_RE.match(stripped):
        return _MEMORY_FULL_PATH_ACTION_STEPS
    if _detect_write_action(stripped) or _looks_like_record_expense(stripped):
        return _WRITE_FULL_PATH_ACTION_STEPS
    return _DEFAULT_FULL_PATH_ACTION_STEPS


async def _execute_resident_action_request(
    *,
    action_request: dict[str, Any],
    user_id: int,
    user_name: str,
) -> dict[str, Any]:
    from app.core.action_registry import (
        run_bridge_skill_async,
        run_bridge_snapshot_async,
        run_bridge_store_memory_async,
    )

    kind = str(action_request.get("kind") or "").strip()
    if kind == "bridge.snapshot":
        return await run_bridge_snapshot_async(user_id)
    if kind == "bridge.skill":
        name = str(action_request.get("name") or "").strip()
        params = action_request.get("params") or {}
        if not name:
            return {"success": False, "message": "Missing skill name"}
        if not isinstance(params, dict):
            return {"success": False, "message": "Skill params must be an object"}
        return await run_bridge_skill_async(user_id, user_name, name, params)
    if kind == "bridge.store_memory":
        content = str(action_request.get("content") or "").strip()
        category = str(action_request.get("category") or "general")
        importance = int(action_request.get("importance") or 5)
        shared = bool(action_request.get("shared"))
        if not content:
            return {"success": False, "message": "Missing memory content"}
        return await run_bridge_store_memory_async(
            user_id=user_id,
            content=content,
            category=category,
            importance=importance,
            shared=shared,
        )
    return {"success": False, "message": f"Unsupported resident action kind: {kind}"}


async def _run_codex_resident_loop(
    *,
    text: str,
    user_id: int,
    user_name: str,
    session: Session,
    assistant_id: str,
    image_path: Optional[str] = None,
    caption: str = "",
) -> str:
    last_action_result: Optional[dict[str, Any]] = None
    looks_like_plain_expense = bool(not image_path and _looks_like_record_expense(text))
    step_limit = _determine_full_path_step_limit(text=text, image_path=image_path)
    for step_index in range(step_limit):
        prompt = _build_resident_full_path_prompt(
            text=text,
            user_id=user_id,
            user_name=user_name,
            session=session,
            image_path=image_path if step_index == 0 else None,
            caption=caption,
            last_action_result=last_action_result,
            step_index=step_index,
            step_limit=step_limit,
        )
        reply = await _run_codex(
            prompt,
            user_id=_thread_owner_id(user_id, session),
            chat_id=session.chat_id,
            assistant_id=assistant_id,
            image_path=image_path if step_index == 0 else None,
        )
        action_request = _extract_action_request(reply)
        if action_request:
            log_event(
                logger,
                "agent.full_path_action_request",
                assistant_id=assistant_id,
                user_id=user_id,
                chat_id=session.chat_id,
                kind=str(action_request.get("kind") or ""),
                step=step_index + 1,
            )
            last_action_result = await _execute_resident_action_request(
                action_request=action_request,
                user_id=user_id,
                user_name=user_name,
            )
            continue
        final_reply = _extract_final_reply(reply)
        if final_reply:
            return final_reply
    log_event(
        logger,
        "agent.full_path_exhausted",
        assistant_id=assistant_id,
        user_id=user_id,
        chat_id=session.chat_id,
        looks_like_plain_expense=looks_like_plain_expense,
        steps=step_limit,
    )
    return "这次链路绕得有点多，小灰毛先停在这里。你再发一次，我会继续接住。"


def _build_plain_expense_prompt(
    *,
    text: str,
    user_id: int,
    user_name: str,
    session: Session,
) -> str:
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    family = ", ".join(f"{name}(id:{uid})" for uid, name in FAMILY_MEMBERS.items()) or "not configured"
    return f"""You are `小灰毛`, the resident Codex brain behind a Telegram family finance bot.

This turn is a plain expense-recording turn. Your job is to understand the user's short message and do exactly one of these:
1. Output one action:
<ACTION>{{"kind":"bridge.skill","name":"record_expense","params":{{"category":"餐饮","amount":20,"currency":"SGD","note":"午饭"}}}}</ACTION>
<ACTION>{{"kind":"bridge.skill","name":"record_expense","params":{{"category":"餐饮","amount":20,"currency":"SGD","note":"午饭","owner_user_id":{{小白的用户id}},"owner_user_name":"小白"}}}}</ACTION>
2. Output one short clarification:
<FINAL>...</FINAL>

Rules:
1. Do not use `bridge.snapshot`.
2. Do not issue more than one action.
3. If no different owner is explicitly named, default the expense owner to the current sender.
3a. If the user explicitly says the expense belongs to `小白` or `小鸡毛`, include `owner_user_id` and `owner_user_name` in the action params.
4. If the message is understandable as one expense, prefer action over clarification.
5. Output only `<ACTION>` or `<FINAL>`.

Environment:
- Current time: {now}
- Default currency: {CURRENCY}
- Timezone: {TIMEZONE}
- Family members: {family}
- Current sender user id: {user_id}
- Current sender name: {user_name}

User message:
{text.strip()}
"""


def _build_plain_expense_finish_prompt(
    *,
    original_text: str,
    action_result: dict[str, Any],
) -> str:
    return f"""You are `小灰毛`.

The expense was already recorded successfully. Write one short final Telegram reply in Simplified Chinese.

Rules:
1. Use the action result as the factual source of truth.
2. Keep all facts unchanged.
3. You may lightly polish the wording so it feels warm, lively, and natural.
4. Emojis are allowed, but keep them light and easy to scan.
5. The paw-print emoji can appear naturally at the end when the tone is playful or lively, but do not force it into factual confirmations.
6. Output only:
<FINAL>...</FINAL>

Original user message:
{original_text.strip()}

Resident action result (JSON):
{_format_action_result(action_result)}
"""


def _build_image_expense_prompt(
    *,
    image_path: str,
    caption: str,
    user_id: int,
    user_name: str,
) -> str:
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    family = ", ".join(f"{name}(id:{uid})" for uid, name in FAMILY_MEMBERS.items()) or "not configured"
    return f"""You are `小灰毛`, the resident Codex brain behind a Telegram family finance bot.

This turn is an image expense turn. You must inspect the image first, then do exactly one of these:
1. Output one action:
<ACTION>{{"kind":"bridge.skill","name":"record_expense","params":{{"category":"餐饮","amount":20,"currency":"SGD","note":"午饭"}}}}</ACTION>
<ACTION>{{"kind":"bridge.skill","name":"record_expense","params":{{"category":"餐饮","amount":20,"currency":"SGD","note":"午饭","owner_user_id":{{小白的用户id}},"owner_user_name":"小白"}}}}</ACTION>
2. Output one short final reply:
<FINAL>...</FINAL>

Rules:
1. Look at the image directly. Do not use `bridge.snapshot`.
2. Do not issue more than one action.
3. If the image is a recognizable receipt/bill and the core fields are visible, prefer recording one expense.
4. If the image is not a recognizable receipt, reply briefly and say you could not stably recognize a bill this time.
5. If no different owner is explicitly named in the caption, default the expense owner to the current sender.
6. If the caption explicitly says the expense belongs to `小白` or `小鸡毛`, include `owner_user_id` and `owner_user_name` in the action params.
7. Use the caption only as a hint. The image content is the primary source.
8. Output only `<ACTION>` or `<FINAL>`.

Environment:
- Current time: {now}
- Default currency: {CURRENCY}
- Timezone: {TIMEZONE}
- Family members: {family}
- Current sender user id: {user_id}
- Current sender name: {user_name}

Image file: {image_path}
Caption: {caption or 'none'}
"""


async def _run_plain_expense_turn(
    *,
    text: str,
    user_id: int,
    user_name: str,
    session: Session,
    assistant_id: str,
) -> str:
    from app.core.action_registry import run_action_async

    prompt = _build_plain_expense_prompt(
        text=text,
        user_id=user_id,
        user_name=user_name,
        session=session,
    )
    reply = await _run_codex(
        prompt,
        user_id=_thread_owner_id(user_id, session),
        chat_id=session.chat_id,
        assistant_id=assistant_id,
    )
    action_request = _extract_action_request(reply)
    if not action_request:
        final_reply = _extract_final_reply(reply)
        if final_reply:
            return final_reply
        log_event(
            logger,
            "agent.plain_expense_no_action",
            assistant_id=assistant_id,
            user_id=user_id,
            chat_id=session.chat_id,
        )
        fallback_result = await run_action_async(
            "finance.record_expense",
            user_id=user_id,
            user_name=user_name,
            text=text,
        )
        if not fallback_result.get("success"):
            return "这笔小灰毛这次没稳稳接住，你再发一次，我继续记。"
        fallback_reply = str(fallback_result.get("reply") or "").strip()
        if fallback_reply:
            return fallback_reply
        return str(fallback_result.get("message") or "好呀，这笔已经记下来了。")

    if str(action_request.get("kind") or "") != "bridge.skill" or str(action_request.get("name") or "") != "record_expense":
        log_event(
            logger,
            "agent.plain_expense_unexpected_action",
            assistant_id=assistant_id,
            user_id=user_id,
            chat_id=session.chat_id,
            kind=str(action_request.get("kind") or ""),
            name=str(action_request.get("name") or ""),
        )
        fallback_result = await run_action_async(
            "finance.record_expense",
            user_id=user_id,
            user_name=user_name,
            text=text,
        )
        if not fallback_result.get("success"):
            return "这笔小灰毛理解偏了点，你再发一次，我重新记。"
        fallback_reply = str(fallback_result.get("reply") or "").strip()
        if fallback_reply:
            return fallback_reply
        return str(fallback_result.get("message") or "好呀，这笔已经记下来了。")

    action_result = await _execute_resident_action_request(
        action_request=action_request,
        user_id=user_id,
        user_name=user_name,
    )
    if not action_result.get("success"):
        return str(action_result.get("message") or "这笔这次没记成功，你再发一次我继续试。")

    finish_prompt = _build_plain_expense_finish_prompt(
        original_text=text,
        action_result=action_result,
    )
    finish_reply = await _run_codex(
        finish_prompt,
        user_id=_thread_owner_id(user_id, session),
        chat_id=session.chat_id,
        assistant_id=assistant_id,
    )
    final_reply = _extract_final_reply(finish_reply)
    if final_reply:
        return final_reply
    return str(action_result.get("confirmation") or action_result.get("message") or "好呀，这笔已经记下来了。")


async def _run_image_expense_turn(
    *,
    image_path: str,
    caption: str,
    user_id: int,
    user_name: str,
    session: Session,
    assistant_id: str,
) -> str:
    prompt = _build_image_expense_prompt(
        image_path=image_path,
        caption=caption,
        user_id=user_id,
        user_name=user_name,
    )
    reply = await _run_codex(
        prompt,
        user_id=_thread_owner_id(user_id, session),
        chat_id=session.chat_id,
        assistant_id=assistant_id,
        image_path=image_path,
    )
    action_request = _extract_action_request(reply)
    if not action_request:
        final_reply = _extract_final_reply(reply)
        if final_reply:
            return final_reply
        log_event(
            logger,
            "agent.image_expense_no_action",
            assistant_id=assistant_id,
            user_id=user_id,
            chat_id=session.chat_id,
        )
        return "这张图小灰毛这次没稳稳看清，你再发一次，或者补一句金额和类别我继续接。"

    if str(action_request.get("kind") or "") != "bridge.skill" or str(action_request.get("name") or "") != "record_expense":
        log_event(
            logger,
            "agent.image_expense_unexpected_action",
            assistant_id=assistant_id,
            user_id=user_id,
            chat_id=session.chat_id,
            kind=str(action_request.get("kind") or ""),
            name=str(action_request.get("name") or ""),
        )
        return "这张图小灰毛理解偏了点，你再发一次，或者补一句金额和类别我重新记。"

    action_result = await _execute_resident_action_request(
        action_request=action_request,
        user_id=user_id,
        user_name=user_name,
    )
    if not action_result.get("success"):
        return str(action_result.get("message") or "这张图这次没记成功，你再发一次我继续试。")

    finish_prompt = _build_plain_expense_finish_prompt(
        original_text=caption or "[图片记账]",
        action_result=action_result,
    )
    finish_reply = await _run_codex(
        finish_prompt,
        user_id=_thread_owner_id(user_id, session),
        chat_id=session.chat_id,
        assistant_id=assistant_id,
    )
    final_reply = _extract_final_reply(finish_reply)
    if final_reply:
        return final_reply
    return str(action_result.get("confirmation") or action_result.get("message") or "好呀，这笔已经记下来了。")


def _build_short_query_prompt(
    *,
    text: str,
    user_id: int,
    user_name: str,
) -> str:
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    family = ", ".join(f"{name}(id:{uid})" for uid, name in FAMILY_MEMBERS.items()) or "not configured"
    return f"""You are `小灰毛`, the resident Codex brain behind a Telegram family finance bot.

This turn is a short finance query turn. First understand the wording and scope, then issue exactly one query action.

You must do exactly one of these:
1. Output one resident action:
<ACTION>{{"kind":"bridge.skill","name":"query_today_total","params":{{"scope":"me"}}}}</ACTION>
<ACTION>{{"kind":"bridge.skill","name":"query_today_total","params":{{"scope":"spouse"}}}}</ACTION>
<ACTION>{{"kind":"bridge.skill","name":"query_today_items","params":{{"scope":"family"}}}}</ACTION>
<ACTION>{{"kind":"bridge.skill","name":"query_monthly_total","params":{{"scope":"me"}}}}</ACTION>
<ACTION>{{"kind":"bridge.skill","name":"query_summary","params":{{"scope":"family"}}}}</ACTION>
<ACTION>{{"kind":"bridge.skill","name":"query_category_total","params":{{"scope":"spouse","category":"餐饮"}}}}</ACTION>
<ACTION>{{"kind":"bridge.skill","name":"query_category_items","params":{{"scope":"spouse","category":"餐饮","limit":20}}}}</ACTION>
<ACTION>{{"kind":"bridge.skill","name":"query_recent_expenses","params":{{"scope":"spouse","limit":10}}}}</ACTION>
<ACTION>{{"kind":"bridge.skill","name":"query_budget","params":{{}}}}</ACTION>
2. Output one short clarification:
<FINAL>...</FINAL>

Rules:
1. Do not use `bridge.snapshot`.
2. Do not issue more than one action.
3. Use scope:
   - `me` for the current sender
   - `spouse` when the sender is asking about the other family member (`小白` or `小鸡毛`)
   - `family` for `全家` / `家庭` / `我们`
4. For `今天/今日` total questions, prefer `query_today_total`.
5. For `今天/今日` expense-list style questions, prefer `query_today_items`.
6. For `本月/这个月` total questions, prefer `query_monthly_total`.
7. For `明细/细则`, prefer `query_today_items`, `query_category_items`, or `query_recent_expenses` depending on the wording.
8. For category total questions such as `餐饮花了多少`, prefer `query_category_total`.
9. For budget questions, prefer `query_budget`.
10. If the wording is understandable enough for one query action, do not ask a clarification question.
11. Output only `<ACTION>` or `<FINAL>`.

Environment:
- Current time: {now}
- Default currency: {CURRENCY}
- Timezone: {TIMEZONE}
- Family members: {family}
- Current sender user id: {user_id}
- Current sender name: {user_name}

User message:
{text.strip()}
"""


def _build_short_query_finish_prompt(
    *,
    original_text: str,
    action_result: dict[str, Any],
) -> str:
    return f"""You are `小灰毛`.

The finance query has already been executed successfully. Write one short final Telegram reply in Simplified Chinese.

Rules:
1. Use the action result as the factual source of truth.
2. Keep all facts unchanged.
3. Be concise, warm, and easy to scan.
4. If the result contains items, it is okay to list a few key lines and then give the total if available.
5. You may lightly polish the wording so it feels natural, but do not invent details or say you cannot query something that is already present in the result.
6. Emojis are optional and should stay light.
7. Output only:
<FINAL>...</FINAL>

Original user message:
{original_text.strip()}

Resident action result (JSON):
{_format_action_result(action_result)}
"""


async def _run_short_query_turn(
    *,
    text: str,
    user_id: int,
    user_name: str,
    session: Session,
    assistant_id: str,
) -> str:
    prompt = _build_short_query_prompt(
        text=text,
        user_id=user_id,
        user_name=user_name,
    )
    reply = await _run_codex(
        prompt,
        user_id=_thread_owner_id(user_id, session),
        chat_id=session.chat_id,
        assistant_id=assistant_id,
    )
    action_request = _extract_action_request(reply)
    if not action_request:
        final_reply = _extract_final_reply(reply)
        if final_reply:
            return final_reply
        log_event(
            logger,
            "agent.short_query_no_action",
            assistant_id=assistant_id,
            user_id=user_id,
            chat_id=session.chat_id,
        )
        return "这次查询小灰毛没稳稳接住，你再发一次，我继续帮你查。"

    if str(action_request.get("kind") or "") != "bridge.skill" or str(action_request.get("name") or "") not in _SHORT_QUERY_SKILL_NAMES:
        log_event(
            logger,
            "agent.short_query_unexpected_action",
            assistant_id=assistant_id,
            user_id=user_id,
            chat_id=session.chat_id,
            kind=str(action_request.get("kind") or ""),
            name=str(action_request.get("name") or ""),
        )
        return "这次查询小灰毛理解偏了点，你再发一次，我重新查。"

    action_result = await _execute_resident_action_request(
        action_request=action_request,
        user_id=user_id,
        user_name=user_name,
    )
    if not action_result.get("success"):
        return str(action_result.get("message") or "这次查询没稳稳跑出来，你再发一次我继续查。")

    finish_prompt = _build_short_query_finish_prompt(
        original_text=text,
        action_result=action_result,
    )
    finish_reply = await _run_codex(
        finish_prompt,
        user_id=_thread_owner_id(user_id, session),
        chat_id=session.chat_id,
        assistant_id=assistant_id,
    )
    final_reply = _extract_final_reply(finish_reply)
    if final_reply:
        return final_reply
    return str(action_result.get("message") or "好呀，小灰毛已经帮你查到了。")


def _build_fast_prompt(
    text: str,
    user_id: int,
    user_name: str,
    session: Session,
    fast_intent: str,
) -> str:
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    action_name, intent_note = {
        "exchange_rate": ("finance.exchange_rate", "simple exchange-rate query"),
        "delete_by_id": ("finance.delete_by_id", "simple delete-by-id"),
        "forward_message": ("family.forward_message", "simple family message forwarding"),
    }[fast_intent]
    return f"""Fast workbench turn for `小灰毛`.

Reply in Simplified Chinese only.
Use exactly one workbench action: `{fast_intent}`.
Only run:
`curl --silent --show-error --unix-socket "{ACTION_REGISTRY_SOCKET_PATH}" -X POST http://localhost/run --data-urlencode "action={action_name}" --data-urlencode "user_id={user_id}" --data-urlencode "user_name={user_name}" --data-urlencode "text=<original user text>"`

The workbench already parses the user text and runs the correct action.
The workbench returns a JSON object with a `reply` field. Use that `reply` as the factual baseline.
You may lightly polish the wording so it feels natural, warm, and chat-friendly, but do not change any facts, numbers, ids, categories, scope, or deletion result.
When it feels natural, prefer `小鸡毛` / `小白` over generic `你`.
Emojis are welcome when they help the reply feel more alive, and the paw-print emoji can be used as a natural signature mainly in playful or lively endings, not in every reply.
Do not inspect repo files. Do not use bridge_ops. Do not use other skills. Keep the reply short.
If the message is genuinely too ambiguous for the workbench, ask one short clarification question.

Time: {now}
Currency: {CURRENCY}
Intent: {intent_note}
User message: {text.strip()}
"""


async def _run_codex(
    prompt: str,
    user_id: int,
    chat_id: int,
    assistant_id: str,
    image_path: Optional[str] = None,
) -> str:
    reply = await DEFAULT_RESIDENT_AGENT_SERVICE.run(
        prompt,
        assistant_id=assistant_id,
        user_id=user_id,
        chat_id=chat_id,
        image_path=image_path,
    )
    cleaned = (reply or "").strip()
    if cleaned:
        return cleaned
    return "小灰毛这次没稳稳接住，麻烦再发一次，我继续接着看。"


async def agent_handle(text: str, user_id: int, user_name: str, session: Session, assistant_id: str) -> str:
    thread_owner_id = _thread_owner_id(user_id, session)
    if session.interaction_count == 0:
        _reset_session_history(thread_owner_id, session.chat_id, assistant_id)
    text = _rewrite_scope_followup(text, _get_recent_history(thread_owner_id, session.chat_id))

    pending_action_key = (thread_owner_id, session.chat_id)
    pending_action = _PENDING_ACTION.get(pending_action_key)
    if pending_action:
        if _is_yes_confirmation(text):
            _PENDING_ACTION.pop(pending_action_key, None)
            original_text = pending_action["original_text"]
            reply = await _run_codex_resident_loop(
                text=original_text,
                user_id=user_id,
                user_name=user_name,
                session=session,
                assistant_id=assistant_id,
            )
            return _remember_and_reply(thread_owner_id, session.chat_id, text, reply)
        if _is_no_confirmation(text):
            _PENDING_ACTION.pop(pending_action_key, None)
            reply = "好呀，那这次小灰毛先不动它。"
            return _remember_and_reply(thread_owner_id, session.chat_id, text, reply)
        _PENDING_ACTION.pop(pending_action_key, None)

    memory_admin_reply = await _maybe_handle_memory_admin(text, user_id)
    if memory_admin_reply:
        return _remember_and_reply(thread_owner_id, session.chat_id, text, memory_admin_reply)

    delete_candidate_reply = _maybe_build_delete_candidate_reply(text, user_id)
    if delete_candidate_reply:
        return _remember_and_reply(thread_owner_id, session.chat_id, text, delete_candidate_reply)

    pending_key = (thread_owner_id, session.chat_id)
    pending = _PENDING_MEMORY.get(pending_key)
    if pending:
        if _is_yes_confirmation(text):
            normalized_content = await _normalize_memory_to_english(
                str(pending["content"]),
                str(pending["category"]),
            )
            if not normalized_content:
                _PENDING_MEMORY.pop(pending_key, None)
                reply = "这条记忆小灰毛这次没稳稳转成英文摘要，所以先没写进去。稍后再试一次会更稳。"
                return _remember_and_reply(thread_owner_id, session.chat_id, text, reply)

            memory_id = store_memory(
                int(pending["target_user_id"]),
                normalized_content,
                category=str(pending["category"]),
                importance=int(pending["importance"]),
            )
            _PENDING_MEMORY.pop(pending_key, None)
            scope = "家庭共享记忆" if pending.get("shared") else "个人记忆"
            reply = (
                f"好呀，小灰毛已经记下来了。\n"
                f"已更新：#{memory_id} [{pending['category']}] {normalized_content}\n"
                f"类型：{scope}"
            )
            return _remember_and_reply(thread_owner_id, session.chat_id, text, reply)
        if _is_no_confirmation(text):
            _PENDING_MEMORY.pop(pending_key, None)
            reply = "好呀，这条小灰毛先不记。之后如果还想记下来，再提一嘴就行。"
            return _remember_and_reply(thread_owner_id, session.chat_id, text, reply)
        _PENDING_MEMORY.pop(pending_key, None)

    memory_candidate = _detect_memory_candidate(user_id, text)
    if memory_candidate:
        if memory_candidate.get("duplicate"):
            reply = f"这条小灰毛已经记着啦：{memory_candidate['content']}"
            return _remember_and_reply(thread_owner_id, session.chat_id, text, reply)

        _PENDING_MEMORY[pending_key] = memory_candidate
        scope = "家庭共享记忆" if memory_candidate.get("shared") else "个人记忆"
        reply = (
            f"小灰毛觉得这句话挺值得记一下：\n"
            f"[{memory_candidate['category']}] {memory_candidate['content']}\n"
            f"准备写入：{scope}\n"
            "要不要让小灰毛记下来？回“是”或者“不要”就行。"
        )
        return _remember_and_reply(thread_owner_id, session.chat_id, text, reply)

    write_action = _detect_write_action(text)
    if write_action:
        _PENDING_ACTION[pending_action_key] = {
            "action_label": write_action,
            "original_text": text,
        }
        reply = _build_action_confirmation(write_action, text)
        return _remember_and_reply(thread_owner_id, session.chat_id, text, reply)

    fast_intent = _detect_fast_finance_intent(text)
    if fast_intent in _FAST_WORKBENCH_INTENTS:
        from app.core.action_registry import run_action_async

        log_event(
            logger,
            "agent.fast_path",
            assistant_id=assistant_id,
            user_id=user_id,
            chat_id=session.chat_id,
            intent=fast_intent,
        )
        result = await run_action_async(
            _FAST_INTENT_ACTIONS[fast_intent],
            user_id=user_id,
            user_name=user_name,
            text=text,
        )
        reply = str(result.get("reply") or "").strip()
        if reply:
            return _remember_and_reply(thread_owner_id, session.chat_id, text, reply)
        reply = "小灰毛这次没稳稳接住这个快捷动作，麻烦再发一次，我继续帮着看。"
        return _remember_and_reply(thread_owner_id, session.chat_id, text, reply)

    if _looks_like_short_query_turn(text):
        log_event(
            logger,
            "agent.short_query_path",
            assistant_id=assistant_id,
            user_id=user_id,
            chat_id=session.chat_id,
        )
        reply = await _run_short_query_turn(
            text=text,
            user_id=user_id,
            user_name=user_name,
            session=session,
            assistant_id=assistant_id,
        )
        return _remember_and_reply(thread_owner_id, session.chat_id, text, reply)

    if _looks_like_record_expense(text):
        log_event(
            logger,
            "agent.plain_expense_path",
            assistant_id=assistant_id,
            user_id=user_id,
            chat_id=session.chat_id,
        )
        reply = await _run_plain_expense_turn(
            text=text,
            user_id=user_id,
            user_name=user_name,
            session=session,
            assistant_id=assistant_id,
        )
        return _remember_and_reply(thread_owner_id, session.chat_id, text, reply)
    else:
        log_event(
            logger,
            "agent.full_path",
            assistant_id=assistant_id,
            user_id=user_id,
            chat_id=session.chat_id,
        )
    reply = await _run_codex_resident_loop(
        text=text,
        user_id=user_id,
        user_name=user_name,
        session=session,
        assistant_id=assistant_id,
    )
    return _remember_and_reply(thread_owner_id, session.chat_id, text, reply)


async def agent_handle_image(
    image_path: str,
    caption: str,
    user_id: int,
    user_name: str,
    session: Session,
    assistant_id: str,
) -> str:
    thread_owner_id = _thread_owner_id(user_id, session)
    if session.interaction_count == 0:
        _reset_session_history(thread_owner_id, session.chat_id, assistant_id)
    log_event(
        logger,
        "agent.image_expense_path",
        assistant_id=assistant_id,
        user_id=user_id,
        chat_id=session.chat_id,
    )
    reply = await _run_image_expense_turn(
        image_path=image_path,
        caption=caption,
        user_id=user_id,
        user_name=user_name,
        session=session,
        assistant_id=assistant_id,
    )
    return _remember_and_reply(thread_owner_id, session.chat_id, caption or "[图片]", reply)


async def agent_handle_export(user_id: int, user_name: str, scope: str = "me") -> Optional[str]:
    result = await execute_tool("export_csv", user_id, user_name, {"scope": scope})
    if result.get("success"):
        return result.get("csv_content", "")
    return None
