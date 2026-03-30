"""Local Codex bridge for Telegram messages.

This replaces the old remote LLM provider flow. Every Telegram message is
forwarded to the local Codex CLI, which can inspect this repository and use the
existing Python skills/database helpers to manage expenses.
"""

import asyncio
import logging
import os
import re
import tempfile
from datetime import datetime
from typing import Optional

from zoneinfo import ZoneInfo

from app.core.memory import get_memory_manager, get_recent_memories, store_memory
from app.config import (
    CODEX_BIN,
    CODEX_HOME,
    CODEX_MODEL,
    CODEX_PROFILE,
    CODEX_TIMEOUT_SECONDS,
    CODEX_WORKDIR,
    CURRENCY,
    DATABASE_PATH,
    FAMILY_MEMBERS,
    LOCATION,
    TIMEZONE,
)
from app.core.session import Session
from app.mcp_tools.registry import execute_tool
from app.services.expense_service import get_recent_expenses

logger = logging.getLogger(__name__)

_SESSION_HISTORY: dict[tuple[int, int], list[dict[str, str]]] = {}
_PENDING_MEMORY: dict[tuple[int, int], dict[str, object]] = {}
_MAX_HISTORY_TURNS = 6
_MAX_DB_RECENT_EXPENSES = 5
_MAX_DB_RECENT_MEMORIES = 5

_RECORD_LIKE_RE = re.compile(r"^\s*\S.{0,40}\s+\d+(?:\.\d+)?(?:\s*[A-Za-z]{3}|元|块|人民币)?\s*$")
_MEMORY_PATTERNS: list[tuple[re.Pattern[str], str, int]] = [
    (re.compile(r"(目标|计划|打算|争取|要存|想存|少花|省钱|控制预算|减少开销)"), "goal", 7),
    (re.compile(r"(喜欢|不喜欢|偏好|习惯|通常|尽量|以后|不再|少坐|少点外卖|多做饭)"), "preference", 6),
    (re.compile(r"(决定|商量好了|定了|以后我们|这个月我们|接下来我们)"), "decision", 7),
]


def _remember_turn(user_id: int, chat_id: int, role: str, content: str) -> None:
    key = (user_id, chat_id)
    history = _SESSION_HISTORY.setdefault(key, [])
    history.append({"role": role, "content": content})
    if len(history) > _MAX_HISTORY_TURNS * 2:
        _SESSION_HISTORY[key] = history[-(_MAX_HISTORY_TURNS * 2):]


def _get_recent_history(user_id: int, chat_id: int) -> list[dict[str, str]]:
    return list(_SESSION_HISTORY.get((user_id, chat_id), []))


def _reset_session_history(user_id: int, chat_id: int) -> None:
    _SESSION_HISTORY.pop((user_id, chat_id), None)
    _PENDING_MEMORY.pop((user_id, chat_id), None)


def _format_history(history: list[dict[str, str]]) -> str:
    if not history:
        return "无"
    lines: list[str] = []
    for item in history:
        role = "用户" if item["role"] == "user" else "助手"
        lines.append(f"- {role}: {item['content']}")
    return "\n".join(lines)


def _format_db_snapshot(user_id: int) -> str:
    mm = get_memory_manager()
    profile_entries = mm.get_all_profile_keys(user_id)
    recent_memories = get_recent_memories(user_id, limit=_MAX_DB_RECENT_MEMORIES)
    recent_expenses = get_recent_expenses(user_id, limit=_MAX_DB_RECENT_EXPENSES)

    lines = ["[Database Snapshot]"]

    if recent_expenses:
        lines.append("最近账目记录:")
        for exp in recent_expenses:
            lines.append(
                f"- #{exp.id} {exp.category} {exp.amount:.2f} {exp.currency} "
                f"[{exp.ledger_type}] 备注:{exp.note or '无'} "
                f"事件:{exp.event_tag or '无'} 时间:{exp.created_at}"
            )
    else:
        lines.append("最近账目记录: 无")

    if recent_memories:
        lines.append("最近记忆:")
        for memory in recent_memories:
            scope = "家庭" if memory.get("scope") == "family" else "个人"
            lines.append(
                f"- #{memory['id']} [{scope}/{memory['category']}] {memory['content']} "
                f"(importance={memory['importance']})"
            )
    else:
        lines.append("最近记忆: 无")

    if profile_entries:
        lines.append("当前画像/Profile:")
        for entry in profile_entries:
            scope = "家庭" if entry.get("scope") == "family" else "个人"
            lines.append(f"- [{scope}] {entry['key']}: {entry['value']}")
    else:
        lines.append("当前画像/Profile: 无")

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

    if session.interaction_count == 0:
        _reset_session_history(user_id, session.chat_id)

    history = _format_history(_get_recent_history(user_id, session.chat_id))
    db_snapshot = _format_db_snapshot(user_id)
    family = ", ".join(f"{name}(id:{uid})" for uid, name in FAMILY_MEMBERS.items()) or "未配置"
    chat_kind = "私聊" if session.is_private else "群聊"

    user_block = text.strip() or "用户发送了一张图片，请结合图片判断。"
    image_hint = ""
    if image_path:
        image_hint = (
            f"\n图片文件: {image_path}\n"
            f"图片说明: {caption or '无'}\n"
            "如果是收据或账单图片，请结合图片内容完成记账；如果不是可识别票据，请友好说明。"
        )

    return f"""你现在是家庭记账 Telegram bot 背后的本地 Codex 执行器。

你的任务是处理一条 Telegram 用户消息，并在必要时通过现有仓库代码管理账本数据库。

必须遵守：
0. 机器人名字是“小灰毛”；男主人叫“小鸡毛”；女主人叫“小白”。
1. 你运行在严格 bridge 模式下。默认只允许通过 `app/bridge_ops.py` 读取或写入财务/记忆事实。
2. 不要直接修改仓库源码，不要自己拼 SQL，不要绕过 `bridge_ops.py` 去拿数字或历史。
3. 可以运行只针对数据库或查询的短命令，但优先且尽量只使用 `bridge_ops.py`。
4. 不要执行 git、安装依赖、联网请求、长时间后台进程。
5. 最终只输出“要发回 Telegram 的中文回复正文”，不要输出分析过程、命令日志、代码块或额外前缀。
6. 如果需要记账/查询，请直接用仓库里的现有 skills 和数据库；不要假装已经执行。
7. 当前数据库路径: {DATABASE_PATH}
8. 默认货币: {CURRENCY}；时区: {TIMEZONE}；地点: {LOCATION}
9. 家庭成员: {family}
10. 如果信息不足以安全记账，直接向用户追问，不要猜。
11. 群聊里注意隐私，用家庭视角回复；私聊可以更自然一些。
12. 任何包含“金额、次数、历史、趋势、预算、记忆、偏好、目标、上次、最近”的回答，必须以数据库查询或数据库写入为依据。
13. 如果这一轮你没有亲自查库/落库，就不要给出具体数字、历史事实、偏好判断或“你之前说过”的表述。
14. 当用户表达稳定偏好、目标、习惯、家庭决定时，不要直接写入记忆。先询问用户是否要记忆，只有在用户明确确认后才写入数据库。
15. 在这条 Telegram bridge 里，读取或写入财务/记忆事实时，只应使用下面这些白名单 CLI：
    - PYTHONPYCACHEPREFIX=/tmp/pycache python3 -m app.bridge_ops snapshot --user-id {user_id}
    - PYTHONPYCACHEPREFIX=/tmp/pycache python3 -m app.bridge_ops skill --user-id {user_id} --user-name "{user_name}" --name <skill_name> --params-json '<json>'
    - PYTHONPYCACHEPREFIX=/tmp/pycache python3 -m app.bridge_ops store-memory --user-id {user_id} ...
16. 不要为了拿数字或记忆去写临时脚本；不要调用其他 repo 内部入口替代 bridge_ops。
17. 如果用户只是闲聊、感谢、确认，不要编造账务信息，简短自然回复即可。
18. 如果用户要删账，优先先用 `query_recent_expenses` 或相关查询确认记录，再用 `delete_expense_by_id` 精准删除；只有明确说“撤销上一笔”时才用 `delete_last_expense`。
19. 默认把 `regular` 视为日常开销、`special` 视为专项开销。除非用户明确要求“包含专项”，否则月度/周度/预算类回答优先使用默认日常口径。
20. 如果用户要创建旅行/装修/婚礼等专项计划，优先用 `start_event` 创建 `planning` 状态的计划；只有用户明确说“开始了/进入进行中”时，再把它设为 `active`。

推荐执行方式：
- 运行只读或短命令时，优先带上 `PYTHONPYCACHEPREFIX=/tmp/pycache python3 ...`，避免 pycache 权限问题。
- 首选：`PYTHONPYCACHEPREFIX=/tmp/pycache python3 -m app.bridge_ops snapshot --user-id {user_id}`
- 首选：`PYTHONPYCACHEPREFIX=/tmp/pycache python3 -m app.bridge_ops skill --user-id {user_id} --user-name "{user_name}" --name query_monthly_total --params-json '{{"scope":"me"}}'`
- 看最近账目：`PYTHONPYCACHEPREFIX=/tmp/pycache python3 -m app.bridge_ops skill --user-id {user_id} --user-name "{user_name}" --name query_recent_expenses --params-json '{{"scope":"me","limit":10}}'`
- 查预算变更：`PYTHONPYCACHEPREFIX=/tmp/pycache python3 -m app.bridge_ops skill --user-id {user_id} --user-name "{user_name}" --name query_budget_changes --params-json '{{"limit":10}}'`

上下文：
- 当前时间: {now}
- 用户ID: {user_id}
- 用户名: {user_name}
- 会话类型: {chat_kind}
- 最近对话:
{history}
- 当前数据库快照:
{db_snapshot}
{image_hint}

本次用户消息：
{user_block}
"""


async def _run_codex(prompt: str, image_path: Optional[str] = None) -> str:
    db_dir = os.path.dirname(os.path.abspath(DATABASE_PATH)) or "."
    args = [
        CODEX_BIN,
        "exec",
        "--full-auto",
        "--sandbox",
        "workspace-write",
        "--cd",
        CODEX_WORKDIR,
        "--add-dir",
        db_dir,
        "--output-last-message",
    ]

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
    output_path = tmp.name
    tmp.close()
    args.append(output_path)
    args.extend(["--color", "never", "--ephemeral"])

    if CODEX_PROFILE:
        args.extend(["--profile", CODEX_PROFILE])
    if CODEX_MODEL:
        args.extend(["--model", CODEX_MODEL])
    if image_path:
        args.extend(["--image", image_path])

    args.append(prompt)
    logger.info("[CODEX] Executing local Codex CLI")

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=CODEX_WORKDIR,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "CODEX_HOME": CODEX_HOME},
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=CODEX_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            logger.error("[CODEX] Timed out after %ss", CODEX_TIMEOUT_SECONDS)
            return "这次处理超时了，请稍后再试一次。"

        if proc.returncode != 0:
            logger.error(
                "[CODEX] Exit=%s stdout=%s stderr=%s",
                proc.returncode,
                stdout.decode("utf-8", errors="ignore")[:500],
                stderr.decode("utf-8", errors="ignore")[:500],
            )
            return "本地 Codex 处理失败了，请稍后再试。"

        try:
            with open(output_path, "r", encoding="utf-8") as fh:
                message = fh.read().strip()
        except FileNotFoundError:
            logger.error("[CODEX] Output file missing")
            return "本地 Codex 没有返回结果，请稍后再试。"

        return message or "操作完成。"
    finally:
        try:
            os.unlink(output_path)
        except FileNotFoundError:
            pass


async def agent_handle(text: str, user_id: int, user_name: str, session: Session) -> str:
    if session.interaction_count == 0:
        _reset_session_history(user_id, session.chat_id)

    pending_key = (user_id, session.chat_id)
    pending = _PENDING_MEMORY.get(pending_key)
    if pending:
        if _is_yes_confirmation(text):
            memory_id = store_memory(
                int(pending["target_user_id"]),
                str(pending["content"]),
                category=str(pending["category"]),
                importance=int(pending["importance"]),
            )
            _PENDING_MEMORY.pop(pending_key, None)
            scope = "家庭共享记忆" if pending.get("shared") else "个人记忆"
            reply = (
                f"好，我已经记下来了。\n"
                f"已更新：#{memory_id} [{pending['category']}] {pending['content']}\n"
                f"类型：{scope}"
            )
            return _remember_and_reply(user_id, session.chat_id, text, reply)
        if _is_no_confirmation(text):
            _PENDING_MEMORY.pop(pending_key, None)
            reply = "好，这条我先不记。之后如果你想记下来，直接再告诉我一遍就行。"
            return _remember_and_reply(user_id, session.chat_id, text, reply)
        _PENDING_MEMORY.pop(pending_key, None)

    memory_candidate = _detect_memory_candidate(user_id, text)
    if memory_candidate:
        if memory_candidate.get("duplicate"):
            reply = f"这条信息我已经记着了：{memory_candidate['content']}"
            return _remember_and_reply(user_id, session.chat_id, text, reply)

        _PENDING_MEMORY[pending_key] = memory_candidate
        scope = "家庭共享记忆" if memory_candidate.get("shared") else "个人记忆"
        reply = (
            f"我发现这句话可能值得记忆：\n"
            f"[{memory_candidate['category']}] {memory_candidate['content']}\n"
            f"准备写入：{scope}\n"
            "要我记下来吗？回复“是”或“不要”即可。"
        )
        return _remember_and_reply(user_id, session.chat_id, text, reply)

    prompt = _build_prompt(text=text, user_id=user_id, user_name=user_name, session=session)
    reply = await _run_codex(prompt)
    return _remember_and_reply(user_id, session.chat_id, text, reply)


async def agent_handle_image(
    image_path: str,
    caption: str,
    user_id: int,
    user_name: str,
    session: Session,
) -> str:
    if session.interaction_count == 0:
        _reset_session_history(user_id, session.chat_id)
    prompt = _build_prompt(
        text=caption,
        user_id=user_id,
        user_name=user_name,
        session=session,
        image_path=image_path,
        caption=caption,
    )
    reply = await _run_codex(prompt, image_path=image_path)
    return _remember_and_reply(user_id, session.chat_id, caption or "[图片]", reply)


async def agent_handle_export(user_id: int, user_name: str, scope: str = "me") -> Optional[str]:
    result = await execute_tool("export_csv", user_id, user_name, {"scope": scope})
    if result.get("success"):
        return result.get("csv_content", "")
    return None
