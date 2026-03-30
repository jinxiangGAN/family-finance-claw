"""Telegram bot handlers (Agent v3: memory + session + proactive)."""

import io
import logging
import os
import tempfile
from datetime import datetime, time

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from zoneinfo import ZoneInfo

from app.core.agent import agent_handle, agent_handle_export, agent_handle_image
from app.config import (
    ALLOWED_USER_IDS,
    BOT_BACKEND,
    CATEGORIES,
    CURRENCY,
    TELEGRAM_BOT_TOKEN,
    TIMEZONE,
    WEEKLY_SUMMARY_DAY,
    WEEKLY_SUMMARY_HOUR,
)
from app.bot.scheduler import budget_alert_job, monthly_archive_job, proactive_nudge_job, weekly_summary_job
from app.services.expense_service import delete_last_expense
from app.core.session import get_or_create_session

logger = logging.getLogger(__name__)


# ───────────────── Access control ─────────────────

def _is_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


async def _check_access(update: Update) -> bool:
    if _is_allowed(update.effective_user.id):  # type: ignore[union-attr]
        return True
    await update.message.reply_text("⛔ 你没有使用权限。")  # type: ignore[union-attr]
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
        "📌 *命令*：/help /delete /export /usage /memory\n\n"
        f"💰 {CURRENCY} | 🤖 {BOT_BACKEND}",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_access(update):
        return
    cats = "、".join(CATEGORIES)
    session = _get_session(update)

    help_text = (
        "📖 *使用帮助*\n\n"
        "*记账*\n"
        "  `午饭 35`  `打车 18`  `午饭 50 人民币`\n"
        "  发送收据照片自动识别\n\n"
        "*查询*\n"
        "  👤 `本月花了多少` / `餐饮花了多少` / `餐饮明细`\n"
        "  👫 `老婆花了多少`\n"
        "  👨‍👩‍👧 `总共花了多少` / `家庭汇总` / `家庭餐饮明细`\n\n"
        "*预算*\n"
        "  `餐饮预算设为1000` / `预算还剩多少`\n\n"
        "*事件标签*\n"
        "  `开始日本旅行` → `结束旅行` → `日本旅行汇总`\n\n"
        "*智能功能*\n"
        "  `分析消费` / `怎么省钱` / `财务规划`\n\n"
        "*记忆*\n"
        "  我会自动记住你的偏好和目标\n"
        "  `你还记得什么` — 查看记忆\n"
        "  `归档记忆 #12` — 归档某条记忆\n"
        "  `把记忆 #12 改成 ...` — 更新某条记忆\n\n"
        "*命令*\n"
        "  /start — 开始\n"
        "  /help — 帮助\n"
        "  /delete — 删除最近一条\n"
        "  /export — 导出 CSV\n"
        "  /export family — 导出家庭 CSV\n"
        "  /usage — 查看当前桥接模式\n"
        "  /memory — 查看我的记忆\n\n"
        f"*分类*：{cats}\n"
        f"*货币*：{CURRENCY}（支持 CNY/USD/AUD/JPY/MYR/EUR 等）"
    )

    if session.is_group:
        help_text += "\n\n💡 在群聊中，我会以家庭视角回复，保护个人隐私。"

    await update.message.reply_text(help_text, parse_mode="Markdown")  # type: ignore[union-attr]


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_access(update):
        return
    user_id = update.effective_user.id  # type: ignore[union-attr]
    deleted = delete_last_expense(user_id)
    if deleted:
        await update.message.reply_text(  # type: ignore[union-attr]
            f"🗑 已删除：{deleted.category} {deleted.amount:.2f} {deleted.currency}（{deleted.note}）"
        )
    else:
        await update.message.reply_text("没有可以删除的记录。")  # type: ignore[union-attr]


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_access(update):
        return
    user_id = update.effective_user.id  # type: ignore[union-attr]
    user_name = update.effective_user.full_name or str(user_id)  # type: ignore[union-attr]

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


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current memories for the user."""
    if not await _check_access(update):
        return
    user_id = update.effective_user.id  # type: ignore[union-attr]

    from app.core.memory import get_recent_memories
    memories = get_recent_memories(user_id, limit=15, include_archived=True)

    if not memories:
        await update.message.reply_text("🧠 我还没有记住任何信息。和我多聊聊吧！")  # type: ignore[union-attr]
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

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route text messages through the session-aware LLM agent."""
    if not await _check_access(update):
        return

    text = update.message.text.strip()  # type: ignore[union-attr]
    if not text:
        return

    user = update.effective_user  # type: ignore[union-attr]
    user_id: int = user.id
    user_name: str = user.full_name or user.username or str(user_id)
    session = _get_session(update)

    await update.message.chat.send_action("typing")  # type: ignore[union-attr]

    reply = await agent_handle(text, user_id, user_name, session)
    await update.message.reply_text(reply)  # type: ignore[union-attr]


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo messages: Receipt OCR."""
    if not await _check_access(update):
        return

    user = update.effective_user  # type: ignore[union-attr]
    user_id: int = user.id
    user_name: str = user.full_name or user.username or str(user_id)
    session = _get_session(update)

    photo = update.message.photo[-1]  # type: ignore[union-attr]
    file = await photo.get_file()
    caption = update.message.caption or ""  # type: ignore[union-attr]
    suffix = ".jpg"
    if file.file_path:
        _, ext = os.path.splitext(file.file_path)
        if ext:
            suffix = ext
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp_path = tmp.name
    tmp.close()

    await update.message.chat.send_action("typing")  # type: ignore[union-attr]

    try:
        await file.download_to_drive(custom_path=tmp_path)
        reply = await agent_handle_image(tmp_path, caption, user_id, user_name, session)
        await update.message.reply_text(reply)  # type: ignore[union-attr]
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

    # Text messages → agent
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Photo messages → Receipt OCR
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

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

    # 3. Daily budget alert (9PM)
    app.job_queue.run_daily(  # type: ignore[union-attr]
        budget_alert_job,
        time=time(hour=21, minute=0, tzinfo=tz),
        name="budget_alert",
    )
    logger.info("Scheduled: daily budget alert — 21:00")

    # 4. Monthly archive (1st of month, 1AM)
    app.job_queue.run_monthly(  # type: ignore[union-attr]
        monthly_archive_job,
        when=time(hour=1, minute=0, tzinfo=tz),
        day=1,
        name="monthly_archive",
    )
    logger.info("Scheduled: monthly archive — 1st of month 01:00")

    return app
