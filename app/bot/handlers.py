"""Telegram bot handlers (Agent v3: memory + session + proactive)."""

import io
import logging
import os
import tempfile
import time as _time
from datetime import datetime, time

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from zoneinfo import ZoneInfo

from app.core.agent import agent_handle, agent_handle_export, agent_handle_image, reset_agent_context
from app.config import (
    ALLOWED_USER_IDS,
    BOT_BACKEND,
    CATEGORIES,
    CURRENCY,
    FAMILY_MEMBERS,
    TELEGRAM_BOT_TOKEN,
    TIMEZONE,
    WEEKLY_SUMMARY_DAY,
    WEEKLY_SUMMARY_HOUR,
)
from app.bot.scheduler import monthly_archive_job, proactive_nudge_job, weekly_summary_job
from app.core.assistant_router import DEFAULT_ASSISTANT_ROUTER
from app.core.observability import log_event
from app.core.session import get_or_create_session, reset_session

logger = logging.getLogger(__name__)

_HELP_LIKE_TEXTS = {
    "help",
    "帮助",
    "你会什么",
    "你能做什么",
    "怎么用你",
    "怎么使用你",
    "有哪些命令",
    "有什么命令",
    "功能介绍",
    "使用说明",
}


# ───────────────── Access control ─────────────────

def _is_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


async def _check_access(update: Update) -> bool:
    if _is_allowed(update.effective_user.id):  # type: ignore[union-attr]
        return True
    await update.message.reply_text("⛔ 小灰毛这边还没给这个账号开权限。")  # type: ignore[union-attr]
    return False


def _get_session(update: Update):
    """Extract session from update."""
    user = update.effective_user  # type: ignore[union-attr]
    chat = update.effective_chat  # type: ignore[union-attr]
    return get_or_create_session(
        user_id=user.id,
        user_name=user.full_name or user.username or str(user.id),
        chat_id=chat.id,
        chat_type=chat.type,
    )


def _build_help_text(session, categories_text: str) -> str:
    help_text = (
        "🗺 *小灰毛功能地图*\n\n"
        "*1. 记账*\n"
        "  `午饭 35`\n"
        "  `打车 18`\n"
        "  `午饭 50 人民币`\n"
        "  也可以直接发送收据照片\n\n"
        "*2. 查账*\n"
        "  `本月花了多少`\n"
        "  `餐饮花了多少`\n"
        "  `餐饮明细`\n"
        "  `看看最近5笔`\n"
        "  `老婆花了多少`\n"
        "  `家庭汇总`\n\n"
        "*3. 预算*\n"
        "  `餐饮预算设为1000`\n"
        "  `预算还剩多少`\n"
        "  `最近预算改过什么`\n\n"
        "*4. 专项计划*\n"
        "  `创建日本旅行计划`\n"
        "  `开始日本旅行`\n"
        "  `日本签证 300`\n"
        "  `日本旅行汇总`\n\n"
        "*5. 记忆*\n"
        "  我会先问你要不要记，再入库\n"
        "  `你还记得什么`\n"
        "  `归档记忆 #12`\n"
        "  `把记忆 #12 改成 ...`\n\n"
        "*6. 代发消息*\n"
        "  `给小白发消息：今晚我晚点回家`\n"
        "  `转告小鸡毛：记得买牛奶`\n\n"
        "*7. 常用命令*\n"
        "  `/help` — 查看这份功能地图\n"
        "  `/memory` — 查看记忆\n"
        "  `/reset` — 清空当前聊天上下文\n"
        "  `/delete` — 进入删除最近一条账目的流程\n"
        "  `/export` — 导出 CSV\n"
        "  `/usage` — 查看当前运行模式\n\n"
        f"*分类*：{categories_text}\n"
        f"*货币*：{CURRENCY}（支持 CNY/USD/AUD/JPY/MYR/EUR 等）"
    )

    if session.is_group:
        help_text += "\n\n💡 在群聊里，我会尽量从家庭视角回答，避免暴露过多个人细节。"
    else:
        help_text += "\n\n💡 你也可以直接问我：`你会什么`、`怎么改记忆`、`怎么开始旅行计划`。"

    return help_text
# ───────────────── Commands ─────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_access(update):
        return
    session = _get_session(update)
    greeting = f"👋 {session.display_name}，" if session.is_private else "👋 "
    await update.message.reply_text(  # type: ignore[union-attr]
        f"{greeting}欢迎使用家庭记账机器人！\n\n"
        "📝 *记账*：`午饭 35` 或发送收据照片\n"
        "🔍 *查询*：`本月花了多少` / `老婆花了多少` / `餐饮明细`\n"
        "📊 *汇总*：`本月汇总` / `家庭汇总`\n"
        "💰 *预算*：`餐饮预算设为1000` / `预算还剩多少`\n"
        "🏷 *事件*：`开始日本旅行` / `结束旅行`\n"
        "🧠 *记忆*：我会记住你的偏好和目标\n"
        "📷 *收据*：发送照片自动识别\n"
        "📤 *导出*：/export\n\n"
        "📌 *命令*：/help /delete /export /usage /memory /reset\n"
        "   其中 `/delete` 会走统一删除流程，不会直接绕过小灰毛删库\n\n"
        f"💰 {CURRENCY} | 🤖 {BOT_BACKEND}",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_access(update):
        return
    cats = "、".join(CATEGORIES)
    session = _get_session(update)
    help_text = _build_help_text(session, cats)
    await update.message.reply_text(help_text, parse_mode="Markdown")  # type: ignore[union-attr]


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_access(update):
        return
    user = update.effective_user  # type: ignore[union-attr]
    session = _get_session(update)
    user_name = session.display_name
    reply = await agent_handle("删除最近一笔", user.id, user_name, session, session.assistant_id)
    await update.message.reply_text(_safe_reply_text(reply))  # type: ignore[union-attr]


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_access(update):
        return
    user_id = update.effective_user.id  # type: ignore[union-attr]
    session = _get_session(update)
    user_name = session.display_name

    scope = "family" if context.args and context.args[0] == "family" else "me"

    csv_content = await agent_handle_export(user_id, user_name, scope)
    if csv_content:
        tz = ZoneInfo(TIMEZONE)
        now = datetime.now(tz)
        filename = f"expenses_{scope}_{now.strftime('%Y%m%d')}.csv"
        buf = io.BytesIO(csv_content.encode("utf-8-sig"))
        buf.name = filename
        await update.message.reply_document(  # type: ignore[union-attr]
            document=buf,
            filename=filename,
            caption=f"📤 {scope} 账单导出完成（{csv_content.count(chr(10))} 条记录）",
        )
    else:
        await update.message.reply_text("没有可导出的数据。")  # type: ignore[union-attr]


async def cmd_usage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_access(update):
        return
    await update.message.reply_text(  # type: ignore[union-attr]
        "🤖 当前运行模式：Telegram -> 本地 Codex CLI\n\n"
        "这个版本不再直接调用外部 LLM API；收到消息后会交给本机 Codex 处理，"
        "再复用仓库里的 skills 和 SQLite 账本。",
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_access(update):
        return
    user = update.effective_user  # type: ignore[union-attr]
    chat = update.effective_chat  # type: ignore[union-attr]
    session = _get_session(update)
    reset_agent_context(user.id, chat.id, assistant_id=session.assistant_id, is_group=session.is_group)
    reset_session(user.id, chat.id)
    await update.message.reply_text(  # type: ignore[union-attr]
        "好啦，这段聊天上下文已经清空了。账目、记忆和画像都还在，小灰毛只是把这段临时状态放下了。"
    )


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current memories for the user."""
    if not await _check_access(update):
        return
    user_id = update.effective_user.id  # type: ignore[union-attr]

    from app.core.memory import get_recent_memories
    memories = get_recent_memories(user_id, limit=15, include_archived=True)

    if not memories:
        await update.message.reply_text("🧠 小灰毛这边暂时还没有记住什么，之后多聊几句就会慢慢有啦。")  # type: ignore[union-attr]
        return

    lines = ["🧠 *我记住的信息*\n"]
    for m in memories:
        prefix = "🔴" if m["importance"] >= 8 else "🟡" if m["importance"] >= 5 else "🟢"
        scope = "家庭" if m.get("scope") == "family" else "个人"
        status = "active" if m.get("is_active", True) else "archived"
        lines.append(f"  {prefix} #{m['id']} [{scope}/{m['category']}/{status}] {m['content']}")

    lines.append("\n💡 说「归档记忆 #ID」会归档某条记忆；说「把记忆 #ID 改成 ...」可以迭代更新")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")  # type: ignore[union-attr]


# ───────────────── Message handlers ─────────────────

def _safe_reply_text(reply: str) -> str:
    cleaned = (reply or "").strip()
    if cleaned:
        return cleaned
    return "小灰毛这次没稳稳接住，麻烦再发一次，我继续帮着看。"


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global Telegram error handler so failures stay visible and user-friendly."""
    err = context.error
    logger.exception("Unhandled Telegram update failure", exc_info=err)
    log_event(
        logger,
        "telegram.unhandled_error",
        error_type=type(err).__name__ if err else "UnknownError",
        error_message=str(err)[:300] if err else "",
    )

    if not isinstance(update, Update):
        return
    chat = update.effective_chat
    if not chat:
        return

    try:
        await context.bot.send_message(
            chat_id=chat.id,
            text="小灰毛刚刚卡了一下，这次没有顺利接住。你再发一次，我接着处理。",
        )
    except TelegramError:
        logger.exception("Failed to send fallback error message to chat %s", chat.id)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route text messages through the session-aware LLM agent."""
    if not await _check_access(update):
        return

    text = update.message.text.strip()  # type: ignore[union-attr]
    if not text:
        return

    user = update.effective_user  # type: ignore[union-attr]
    user_id: int = user.id
    session = _get_session(update)
    user_name: str = session.display_name
    started_at = _time.perf_counter()
    if text.strip().lower() in _HELP_LIKE_TEXTS:
        cats = "、".join(CATEGORIES)
        await update.message.reply_text(_build_help_text(session, cats), parse_mode="Markdown")  # type: ignore[union-attr]
        return

    await update.message.chat.send_action("typing")  # type: ignore[union-attr]

    route = DEFAULT_ASSISTANT_ROUTER.resolve(text, session)
    log_event(
        logger,
        "telegram.text_received",
        user_id=user_id,
        chat_id=session.chat_id,
        assistant_id=route.assistant_id,
        is_group=session.is_group,
        help_like=text.strip().lower() in _HELP_LIKE_TEXTS,
        text_preview=text[:80],
    )
    if route.unknown_identifier:
        await update.message.reply_text(  # type: ignore[union-attr]
            f"小灰毛这边暂时还没有接入名为「{route.unknown_identifier}」的助手。现在能用的是：小灰毛。"
        )
        return

    reply = await agent_handle(route.message_text, user_id, user_name, session, route.assistant_id)
    await update.message.reply_text(_safe_reply_text(reply))  # type: ignore[union-attr]
    log_event(
        logger,
        "telegram.text_replied",
        user_id=user_id,
        chat_id=session.chat_id,
        assistant_id=route.assistant_id,
        elapsed_ms=round((_time.perf_counter() - started_at) * 1000, 1),
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo messages: Receipt OCR."""
    if not await _check_access(update):
        return

    user = update.effective_user  # type: ignore[union-attr]
    user_id: int = user.id
    session = _get_session(update)
    user_name: str = session.display_name
    started_at = _time.perf_counter()
    route = DEFAULT_ASSISTANT_ROUTER.resolve(update.message.caption or "", session)  # type: ignore[union-attr]
    if route.unknown_identifier:
        await update.message.reply_text(  # type: ignore[union-attr]
            f"小灰毛这边暂时还没有接入名为「{route.unknown_identifier}」的助手。现在能用的是：小灰毛。"
        )
        return

    photo = update.message.photo[-1]  # type: ignore[union-attr]
    file = await photo.get_file()
    caption = route.message_text
    suffix = ".jpg"
    if file.file_path:
        _, ext = os.path.splitext(file.file_path)
        if ext:
            suffix = ext
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp_path = tmp.name
    tmp.close()

    await update.message.chat.send_action("typing")  # type: ignore[union-attr]
    log_event(
        logger,
        "telegram.photo_received",
        user_id=user_id,
        chat_id=session.chat_id,
        assistant_id=route.assistant_id,
        caption_preview=(caption or "")[:80],
    )

    try:
        await file.download_to_drive(custom_path=tmp_path)
        reply = await agent_handle_image(tmp_path, caption, user_id, user_name, session, route.assistant_id)
        await update.message.reply_text(_safe_reply_text(reply))  # type: ignore[union-attr]
        log_event(
            logger,
            "telegram.photo_replied",
            user_id=user_id,
            chat_id=session.chat_id,
            assistant_id=route.assistant_id,
            elapsed_ms=round((_time.perf_counter() - started_at) * 1000, 1),
        )
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass


# ───────────────── Bot builder ─────────────────

def build_application() -> Application:
    """Create and configure the Telegram bot Application with all scheduled jobs."""
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("reset", cmd_reset))

    # Text messages → agent
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Photo messages → Receipt OCR
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_error_handler(handle_error)

    # ── Scheduled Jobs ──
    tz = ZoneInfo(TIMEZONE)

    # 1. Weekly summary (Sunday evening)
    app.job_queue.run_daily(  # type: ignore[union-attr]
        weekly_summary_job,
        time=time(hour=WEEKLY_SUMMARY_HOUR, minute=0, tzinfo=tz),
        days=(WEEKLY_SUMMARY_DAY,),
        name="weekly_summary",
    )
    logger.info("Scheduled: weekly summary — day=%s hour=%s", WEEKLY_SUMMARY_DAY, WEEKLY_SUMMARY_HOUR)

    # 2. Proactive nudge (Friday 6PM)
    app.job_queue.run_daily(  # type: ignore[union-attr]
        proactive_nudge_job,
        time=time(hour=18, minute=0, tzinfo=tz),
        days=(4,),  # Friday = 4 (Monday=0)
        name="proactive_nudge",
    )
    logger.info("Scheduled: proactive nudge — Friday 18:00")

    # 3. Monthly archive (1st of month, 1AM)
    app.job_queue.run_monthly(  # type: ignore[union-attr]
        monthly_archive_job,
        when=time(hour=1, minute=0, tzinfo=tz),
        day=1,
        name="monthly_archive",
    )
    logger.info("Scheduled: monthly archive — 1st of month 01:00")

    return app
