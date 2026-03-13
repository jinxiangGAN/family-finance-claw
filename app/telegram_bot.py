"""Telegram bot handlers and command definitions (Agent architecture)."""

import logging
from datetime import time

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from zoneinfo import ZoneInfo

from app.agent import agent_handle
from app.api_tracker import get_usage_stats
from app.config import (
    ALLOWED_USER_IDS,
    CATEGORIES,
    CURRENCY,
    TELEGRAM_BOT_TOKEN,
    TIMEZONE,
    WEEKLY_SUMMARY_DAY,
    WEEKLY_SUMMARY_HOUR,
)
from app.scheduler import weekly_summary_job
from app.services.expense_service import delete_last_expense

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


# ───────────────── Commands ─────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_access(update):
        return
    await update.message.reply_text(  # type: ignore[union-attr]
        "👋 欢迎使用家庭记账机器人！\n\n"
        "📝 *记账*：直接发送消息，例如 `午饭 35`\n"
        "🔍 *查询*：\n"
        "  `本月花了多少` — 我的花销\n"
        "  `老婆花了多少` — 配偶花销\n"
        "  `总共花了多少` — 家庭总花销\n"
        "📊 *汇总*：`本月汇总` / `家庭汇总`\n"
        "💰 *预算*：`餐饮预算设为1000` / `预算还剩多少`\n"
        "📈 *分析*：`分析一下消费` / `怎么省钱`\n"
        "🗑 *撤销*：/delete 删除最近一条\n"
        "📉 *API用量*：/usage\n"
        "❓ *帮助*：/help\n\n"
        f"💰 默认货币：{CURRENCY}",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_access(update):
        return
    cats = "、".join(CATEGORIES)
    await update.message.reply_text(  # type: ignore[union-attr]
        "📖 *使用帮助*\n\n"
        "*记账*：直接发文字\n"
        "  `午饭 35`  `打车 18`  `奶茶 20`\n\n"
        "*查询*（三个视角）\n"
        "  👤 `本月花了多少` / `餐饮花了多少`\n"
        "  👫 `老婆花了多少` / `老公餐饮花了多少`\n"
        "  👨‍👩‍👧 `总共花了多少` / `家庭餐饮花了多少`\n\n"
        "*汇总*\n"
        "  `本月汇总` / `家庭汇总`\n\n"
        "*预算管理*\n"
        "  `餐饮预算设为1000` — 设置分类预算\n"
        "  `总预算设为5000` — 设置总预算\n"
        "  `预算还剩多少` — 查看预算\n\n"
        "*智能功能*\n"
        "  `分析一下我的消费` — 消费分析\n"
        "  `怎么省钱` — 财务建议\n"
        "  `帮我做个财务规划` — 财务规划\n\n"
        "*命令*\n"
        "  /start — 开始\n"
        "  /help — 帮助\n"
        "  /delete — 删除最近一条\n"
        "  /usage — API 用量\n\n"
        f"*分类*：{cats}\n"
        f"*货币*：{CURRENCY}",
        parse_mode="Markdown",
    )


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_access(update):
        return
    user_id = update.effective_user.id  # type: ignore[union-attr]
    deleted = delete_last_expense(user_id)
    if deleted:
        await update.message.reply_text(  # type: ignore[union-attr]
            f"🗑 已删除：{deleted.category} {deleted.amount:.2f} {CURRENCY}（{deleted.note}）"
        )
    else:
        await update.message.reply_text("没有可以删除的记录。")  # type: ignore[union-attr]


async def cmd_usage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show API usage statistics."""
    if not await _check_access(update):
        return
    stats = get_usage_stats()
    if stats["monthly_limit"] > 0:
        await update.message.reply_text(  # type: ignore[union-attr]
            f"📉 *MiniMax API 本月用量*\n\n"
            f"  已用 tokens：{stats['monthly_used']:,}\n"
            f"  月度上限：{stats['monthly_limit']:,}\n"
            f"  剩余：{stats['remaining']:,}\n"
            f"  使用率：{stats['usage_pct']:.1f}%",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(  # type: ignore[union-attr]
            f"📉 *MiniMax API 本月用量*\n\n"
            f"  已用 tokens：{stats['monthly_used']:,}\n"
            f"  月度上限：无限制",
            parse_mode="Markdown",
        )


# ───────────────── Message handler ─────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route all text messages through the LLM agent."""
    if not await _check_access(update):
        return

    text = update.message.text.strip()  # type: ignore[union-attr]
    if not text:
        return

    user = update.effective_user  # type: ignore[union-attr]
    user_id: int = user.id
    user_name: str = user.full_name or user.username or str(user_id)

    # Send "typing" indicator
    await update.message.chat.send_action("typing")  # type: ignore[union-attr]

    reply = await agent_handle(text, user_id, user_name)
    await update.message.reply_text(reply)  # type: ignore[union-attr]


# ───────────────── Bot builder ─────────────────

def build_application() -> Application:
    """Create and configure the Telegram bot Application with scheduled jobs."""
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("usage", cmd_usage))

    # Free text → agent
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Weekly summary job
    tz = ZoneInfo(TIMEZONE)
    app.job_queue.run_daily(  # type: ignore[union-attr]
        weekly_summary_job,
        time=time(hour=WEEKLY_SUMMARY_HOUR, minute=0, tzinfo=tz),
        days=(WEEKLY_SUMMARY_DAY,),
        name="weekly_summary",
    )
    logger.info(
        "Weekly summary scheduled: day=%s hour=%s tz=%s",
        WEEKLY_SUMMARY_DAY, WEEKLY_SUMMARY_HOUR, TIMEZONE,
    )

    return app
