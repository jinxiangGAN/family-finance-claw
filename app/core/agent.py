"""Local Codex bridge for Telegram messages.

This replaces the old remote LLM provider flow. Every Telegram message is
forwarded to the local Codex CLI, which can inspect this repository and use the
existing Python skills/database helpers to manage expenses.
"""

import logging
import re
from datetime import datetime
from typing import Optional

from zoneinfo import ZoneInfo

from app.core.memory import delete_memory, get_memory_manager, get_recent_memories, store_memory, update_memory
from app.config import (
    CURRENCY,
    DATABASE_PATH,
    FAMILY_MEMBERS,
    LOCATION,
    PYTHON_BIN,
    TIMEZONE,
)
from app.core.resident_agent import DEFAULT_RESIDENT_AGENT_SERVICE
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

_RECORD_LIKE_RE = re.compile(r"^\s*\S.{0,40}\s+\d+(?:\.\d+)?(?:\s*[A-Za-z]{3}|元|块|人民币)?\s*$")
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
    "房租",
    "机票",
    "签证",
    "明细",
    "汇总",
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
_WRITE_ACTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?:^|\s)(?:删除|删掉|撤销)(?:最近一笔|上一笔|#?\d+|这笔|那笔)?"), "delete an expense record"),
    (re.compile(r"(?:预算.*(?:设为|改成|改为|调整为))"), "update a budget"),
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


def _looks_like_query(text: str) -> bool:
    return any(
        token in text
        for token in (
            "多少",
            "明细",
            "汇总",
            "分析",
            "预算",
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
    if _RECORD_LIKE_RE.match(stripped):
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
    if _RECORD_LIKE_RE.match(stripped):
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
        f"我理解你是要执行这个操作：{action_label}。\n"
        f"预计调用：`{function_name}`\n"
        f"作用：{function_purpose}\n"
        f"原始内容：{original_text}\n"
        "要我继续执行吗？回复“是”继续，回复“不要”取消。"
    )


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

    history = _format_history(_get_recent_history(thread_owner_id, session.chat_id))
    db_snapshot = _format_db_snapshot(user_id)
    family = ", ".join(f"{name}(id:{uid})" for uid, name in FAMILY_MEMBERS.items()) or "not configured"
    chat_kind = "private chat" if session.is_private else "group chat"
    chat_mode = _detect_chat_mode(text, image_path=image_path)

    user_block = text.strip() or "The user sent an image. Interpret it with the image context."
    image_hint = ""
    if image_path:
        image_hint = (
            f"\nImage file: {image_path}\n"
            f"Image caption: {caption or 'none'}\n"
            "If this is a receipt or bill image, use it to complete expense handling. "
            "If it is not a recognizable receipt, reply politely and say so."
        )

    return f"""You are the local Codex executor behind a Telegram family finance bot.

Your job is to handle one Telegram message and, when needed, use the existing repository code to read or update the finance database.

Hard rules:
0. The bot's name is `小灰毛`. The male owner is `小鸡毛`. The female owner is `小白`.
1. You are running in strict bridge mode. By default, finance and memory facts must be read or written through `app/bridge_ops.py`.
2. Do not modify repository source code. Do not write ad-hoc SQL. Do not bypass `app/bridge_ops.py` to obtain numbers or history.
3. Short-lived read/write commands are allowed only for database-backed finance tasks, and you should strongly prefer `app/bridge_ops.py`.
4. Do not run git commands, install dependencies, make network requests, or start long-lived background processes.
5. Output only the final Telegram reply body in Simplified Chinese. Do not include analysis, logs, code fences, or prefixes.
6. If the task is finance-related, use the existing skills and database helpers. Do not pretend an operation was executed if it was not.
7. Database path: {DATABASE_PATH}
8. Default currency: {CURRENCY}; timezone: {TIMEZONE}; location: {LOCATION}
9. Family members: {family}
10. If the message is not specific enough for safe expense handling, ask a concise follow-up question instead of guessing.
11. In group chat, protect privacy and answer from a family perspective. In private chat, you may sound a bit more personal and warm.
12. Any reply involving amounts, counts, history, trends, budgets, memory, preferences, goals, previous events, or recent activity must be grounded in a database read or write in the current turn.
13. If you did not personally query or update the database in this turn, do not state specific numbers, historical facts, preferences, or claims like "you said before".
14. When the user expresses a stable preference, goal, habit, or family decision, do not store it immediately. Ask for confirmation first, and only store it after explicit consent.
15. Inside this Telegram bridge, use only these whitelisted CLI patterns for finance or memory facts:
    - PYTHONPYCACHEPREFIX=/tmp/pycache {PYTHON_BIN} -m app.bridge_ops snapshot --user-id {user_id}
    - PYTHONPYCACHEPREFIX=/tmp/pycache {PYTHON_BIN} -m app.bridge_ops skill --user-id {user_id} --user-name "{user_name}" --name <skill_name> --params-json '<json>'
    - PYTHONPYCACHEPREFIX=/tmp/pycache {PYTHON_BIN} -m app.bridge_ops store-memory --user-id {user_id} ...
16. Do not write temporary scripts just to fetch numbers or memory. Do not use other repository entrypoints instead of `bridge_ops`.
17. If the user is only chatting, thanking you, venting, or confirming something, do not fabricate finance facts. Reply naturally and briefly.
18. If the user wants to delete an expense, prefer checking records with `query_recent_expenses` first and then use `delete_expense_by_id`. Use `delete_last_expense` only for explicit "undo last expense" requests.
19. Treat `regular` as day-to-day spending and `special` as project/event spending. Unless the user explicitly asks to include special spending, monthly, weekly, and budget-related answers should default to the regular ledger.
20. If the user wants to create a trip, renovation, wedding, or other special plan, prefer using `start_event` with `planning` status first. Only switch it to `active` when the user clearly says it has started.
21. `小灰毛` has two reply modes:
    - `finance mode`: strict, database-grounded handling for expenses, budgets, history, memory, and special plans.
    - `chat mode`: warm, light, natural conversation for casual family chatting, venting, greetings, or comfort.
22. In `chat mode`, `小灰毛` should feel gentle, familiar, and supportive, like a thoughtful household assistant. Stay natural, brief, and not overly theatrical.
23. In `chat mode`, if the user casually mentions feelings about spending or daily life, you may respond softly. Only switch back to `finance mode` when the user clearly asks for amounts, history, budgets, records, or other factual finance operations.
24. If a message mixes casual chat with a finance request, give one short warm response first, then handle the finance part.

Recommended command patterns:
- Prefer `PYTHONPYCACHEPREFIX=/tmp/pycache {PYTHON_BIN} ...` for short commands to avoid pycache permission issues.
- Preferred: `PYTHONPYCACHEPREFIX=/tmp/pycache {PYTHON_BIN} -m app.bridge_ops snapshot --user-id {user_id}`
- Preferred: `PYTHONPYCACHEPREFIX=/tmp/pycache {PYTHON_BIN} -m app.bridge_ops skill --user-id {user_id} --user-name "{user_name}" --name query_monthly_total --params-json '{{"scope":"me"}}'`
- To inspect recent expenses: `PYTHONPYCACHEPREFIX=/tmp/pycache {PYTHON_BIN} -m app.bridge_ops skill --user-id {user_id} --user-name "{user_name}" --name query_recent_expenses --params-json '{{"scope":"me","limit":10}}'`
- To inspect budget changes: `PYTHONPYCACHEPREFIX=/tmp/pycache {PYTHON_BIN} -m app.bridge_ops skill --user-id {user_id} --user-name "{user_name}" --name query_budget_changes --params-json '{{"limit":10}}'`

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


async def _run_codex(
    prompt: str,
    user_id: int,
    chat_id: int,
    assistant_id: str,
    image_path: Optional[str] = None,
) -> str:
    return await DEFAULT_RESIDENT_AGENT_SERVICE.run(
        prompt,
        assistant_id=assistant_id,
        user_id=user_id,
        chat_id=chat_id,
        image_path=image_path,
    )


async def agent_handle(text: str, user_id: int, user_name: str, session: Session, assistant_id: str) -> str:
    thread_owner_id = _thread_owner_id(user_id, session)
    if session.interaction_count == 0:
        _reset_session_history(thread_owner_id, session.chat_id, assistant_id)

    pending_action_key = (thread_owner_id, session.chat_id)
    pending_action = _PENDING_ACTION.get(pending_action_key)
    if pending_action:
        if _is_yes_confirmation(text):
            _PENDING_ACTION.pop(pending_action_key, None)
            original_text = pending_action["original_text"]
            prompt = _build_prompt(text=original_text, user_id=user_id, user_name=user_name, session=session)
            reply = await _run_codex(prompt, user_id=thread_owner_id, chat_id=session.chat_id, assistant_id=assistant_id)
            return _remember_and_reply(thread_owner_id, session.chat_id, text, reply)
        if _is_no_confirmation(text):
            _PENDING_ACTION.pop(pending_action_key, None)
            reply = "好，这次操作我先不执行。"
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
                reply = "这条记忆我这次没能稳定转换成英文摘要，所以先没有入库。你可以稍后再试一次。"
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
                f"好，我已经记下来了。\n"
                f"已更新：#{memory_id} [{pending['category']}] {normalized_content}\n"
                f"类型：{scope}"
            )
            return _remember_and_reply(thread_owner_id, session.chat_id, text, reply)
        if _is_no_confirmation(text):
            _PENDING_MEMORY.pop(pending_key, None)
            reply = "好，这条我先不记。之后如果你想记下来，直接再告诉我一遍就行。"
            return _remember_and_reply(thread_owner_id, session.chat_id, text, reply)
        _PENDING_MEMORY.pop(pending_key, None)

    memory_candidate = _detect_memory_candidate(user_id, text)
    if memory_candidate:
        if memory_candidate.get("duplicate"):
            reply = f"这条信息我已经记着了：{memory_candidate['content']}"
            return _remember_and_reply(thread_owner_id, session.chat_id, text, reply)

        _PENDING_MEMORY[pending_key] = memory_candidate
        scope = "家庭共享记忆" if memory_candidate.get("shared") else "个人记忆"
        reply = (
            f"我发现这句话可能值得记忆：\n"
            f"[{memory_candidate['category']}] {memory_candidate['content']}\n"
            f"准备写入：{scope}\n"
            "要我记下来吗？回复“是”或“不要”即可。"
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

    prompt = _build_prompt(text=text, user_id=user_id, user_name=user_name, session=session)
    reply = await _run_codex(prompt, user_id=thread_owner_id, chat_id=session.chat_id, assistant_id=assistant_id)
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
    prompt = _build_prompt(
        text=caption,
        user_id=user_id,
        user_name=user_name,
        session=session,
        image_path=image_path,
        caption=caption,
    )
    reply = await _run_codex(
        prompt,
        user_id=thread_owner_id,
        chat_id=session.chat_id,
        assistant_id=assistant_id,
        image_path=image_path,
    )
    return _remember_and_reply(thread_owner_id, session.chat_id, caption or "[图片]", reply)


async def agent_handle_export(user_id: int, user_name: str, scope: str = "me") -> Optional[str]:
    result = await execute_tool("export_csv", user_id, user_name, {"scope": scope})
    if result.get("success"):
        return result.get("csv_content", "")
    return None
