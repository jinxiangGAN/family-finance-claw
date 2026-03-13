"""Telegram bot handlers and command definitions."""

import logging
from datetime import datetime

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from zoneinfo import ZoneInfo

from app.config import ALLOWED_USER_IDS, CATEGORIES, CURRENCY, FAMILY_MEMBERS, TELEGRAM_BOT_TOKEN, TIMEZONE
from app.models.expense import Expense, ParsedExpense
from app.parser import parse_expense
from app.services.expense_service import delete_last_expense, save_expense
from app.services.stats_service import (
    get_category_total,
    get_member_name,
    get_month_summary,
    get_month_total,
    get_spouse_id,
    resolve_user_ids,
)

logger = logging.getLogger(__name__)


# ───────────────── Access control ─────────────────

def _is_allowed(user_id: int) -> bool:
    """Check whether the user is in the allow-list (empty list = allow all)."""
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


async def _check_access(update: Update) -> bool:
    """Reply with a rejection and return False if the user is not allowed."""
    if _is_allowed(update.effective_user.id):  # type: ignore[union-attr]
        return True
    await update.message.reply_text("⛔ 你没有使用权限。")  # type: ignore[union-attr]
    return False


# ───────────────── Scope label helper ─────────────────

def _scope_label(scope: str, my_user_id: int) -> str:
    """Return a human-readable label for the query scope."""
    if scope == "me":
        name = FAMILY_MEMBERS.get(my_user_id, "我")
        return f"你（{name}）" if name != "我" else "你"
    elif scope == "spouse":
        spouse_id = get_spouse_id(my_user_id)
        if spouse_id is not None:
            return get_member_name(spouse_id)
        return "配偶"
    else:
        return "家庭"


# ───────────────── Commands ─────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
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
        "🗑 *撤销*：/delete 删除最近一条\n"
        "❓ *帮助*：/help\n\n"
        f"💰 默认货币：{CURRENCY}",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    if not await _check_access(update):
        return
    cats = "、".join(CATEGORIES)
    await update.message.reply_text(  # type: ignore[union-attr]
        "📖 *使用帮助*\n\n"
        "*记账方式*\n"
        "直接发送文字即可记账：\n"
        "  `午饭 35`\n"
        "  `打车 18`\n"
        "  `奶茶 20`\n\n"
        "*查询花销*（三个视角）\n"
        "👤 我的：`本月花了多少` / `餐饮花了多少`\n"
        "👫 配偶：`老婆花了多少` / `老公餐饮花了多少`\n"
        "👨‍👩‍👧 家庭：`总共花了多少` / `家庭餐饮花了多少`\n\n"
        "*汇总*\n"
        "  `本月汇总` — 我的汇总\n"
        "  `老婆汇总` — 配偶汇总\n"
        "  `家庭汇总` — 全家汇总\n\n"
        "*命令*\n"
        "  /start — 开始\n"
        "  /help — 帮助\n"
        "  /delete — 删除最近一条记录\n"
        "  /summary — 家庭汇总（三个视角）\n\n"
        f"*支持分类*：{cats}\n"
        f"*默认货币*：{CURRENCY}",
        parse_mode="Markdown",
    )


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /delete command — remove the user's most recent expense."""
    if not await _check_access(update):
        return
    user_id = update.effective_user.id  # type: ignore[union-attr]
    deleted = delete_last_expense(user_id)
    if deleted:
        await update.message.reply_text(  # type: ignore[union-attr]
            f"🗑 已删除最近一条记录：\n"
            f"{deleted.category} {deleted.amount:.2f} {CURRENCY}（{deleted.note}）"
        )
    else:
        await update.message.reply_text("没有可以删除的记录。")  # type: ignore[union-attr]


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /summary command — show full family summary (all three views)."""
    if not await _check_access(update):
        return
    user_id = update.effective_user.id  # type: ignore[union-attr]
    await _reply_full_summary(update, user_id)


# ───────────────── Message handler ─────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle free-text messages: parse intent and dispatch."""
    if not await _check_access(update):
        return

    text = update.message.text.strip()  # type: ignore[union-attr]
    if not text:
        return

    user = update.effective_user  # type: ignore[union-attr]
    user_id: int = user.id
    user_name: str = user.full_name or user.username or str(user_id)

    parsed_list: list[ParsedExpense] = await parse_expense(text)

    for parsed in parsed_list:
        if parsed.intent == "expense":
            await _handle_expense(update, user_id, user_name, parsed)
        elif parsed.intent == "query":
            await _handle_query(update, user_id, parsed)
        else:
            await update.message.reply_text(  # type: ignore[union-attr]
                "🤔 无法识别您的消息，请输入记账信息或查询指令。\n"
                "输入 /help 查看使用帮助。"
            )


async def _handle_expense(
    update: Update, user_id: int, user_name: str, parsed: ParsedExpense
) -> None:
    """Save a single expense and reply confirmation."""
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    expense = Expense(
        user_id=user_id,
        user_name=user_name,
        category=parsed.category or "其他",
        amount=parsed.amount or 0.0,
        note=parsed.note or "",
        created_at=now.isoformat(),
    )
    save_expense(expense)
    await update.message.reply_text(  # type: ignore[union-attr]
        f"✅ 已记录\n"
        f"{expense.category}  {expense.amount:.2f} {CURRENCY}"
        f"{'  (' + expense.note + ')' if expense.note else ''}"
    )


async def _handle_query(update: Update, user_id: int, parsed: ParsedExpense) -> None:
    """Dispatch query by query_type and scope."""
    qtype = parsed.query_type
    scope = parsed.scope
    user_ids = resolve_user_ids(scope, user_id)
    label = _scope_label(scope, user_id)

    if qtype == "monthly_total":
        total = get_month_total(user_ids)
        await update.message.reply_text(  # type: ignore[union-attr]
            f"📊 {label}本月总支出：{total:.2f} {CURRENCY}"
        )
    elif qtype == "category_total":
        cat = parsed.category or "其他"
        total = get_category_total(cat, user_ids)
        await update.message.reply_text(  # type: ignore[union-attr]
            f"📊 {label}本月{cat}支出：{total:.2f} {CURRENCY}"
        )
    elif qtype == "summary":
        await _reply_summary(update, label, user_ids)
    else:
        await update.message.reply_text("🤔 暂不支持该查询类型。")  # type: ignore[union-attr]


async def _reply_summary(
    update: Update, label: str, user_ids: list[int] | None
) -> None:
    """Build and send a summary message for given user_ids."""
    summary = get_month_summary(user_ids)
    if not summary:
        await update.message.reply_text(  # type: ignore[union-attr]
            f"📊 {label}本月暂无支出记录。"
        )
        return

    grand_total = sum(item["total"] for item in summary)
    lines = [f"📊 *{label} · 本月支出汇总*\n"]
    for item in summary:
        lines.append(f"  {item['category']}：{item['total']:.2f} {CURRENCY}")
    lines.append(f"\n💰 *合计*：{grand_total:.2f} {CURRENCY}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")  # type: ignore[union-attr]


async def _reply_full_summary(update: Update, user_id: int) -> None:
    """Send a complete summary with all three views: me, spouse, family."""
    sections: list[str] = []

    # My summary
    my_label = _scope_label("me", user_id)
    my_ids = resolve_user_ids("me", user_id)
    my_summary = get_month_summary(my_ids)
    my_total = sum(item["total"] for item in my_summary)
    section = f"👤 *{my_label}*\n"
    if my_summary:
        for item in my_summary:
            section += f"  {item['category']}：{item['total']:.2f} {CURRENCY}\n"
        section += f"  小计：{my_total:.2f} {CURRENCY}"
    else:
        section += "  暂无记录"
    sections.append(section)

    # Spouse summary
    spouse_id = get_spouse_id(user_id)
    if spouse_id is not None:
        sp_label = _scope_label("spouse", user_id)
        sp_ids = resolve_user_ids("spouse", user_id)
        sp_summary = get_month_summary(sp_ids)
        sp_total = sum(item["total"] for item in sp_summary)
        section = f"👫 *{sp_label}*\n"
        if sp_summary:
            for item in sp_summary:
                section += f"  {item['category']}：{item['total']:.2f} {CURRENCY}\n"
            section += f"  小计：{sp_total:.2f} {CURRENCY}"
        else:
            section += "  暂无记录"
        sections.append(section)

    # Family summary
    family_summary = get_month_summary(None)
    family_total = sum(item["total"] for item in family_summary)
    section = f"👨‍👩‍👧 *家庭*\n"
    if family_summary:
        for item in family_summary:
            section += f"  {item['category']}：{item['total']:.2f} {CURRENCY}\n"
        section += f"  小计：{family_total:.2f} {CURRENCY}"
    else:
        section += "  暂无记录"
    sections.append(section)

    header = "📊 *本月支出汇总*\n"
    msg = header + "\n\n".join(sections) + f"\n\n💰 *家庭合计*：{family_total:.2f} {CURRENCY}"
    await update.message.reply_text(msg, parse_mode="Markdown")  # type: ignore[union-attr]


# ───────────────── Bot builder ─────────────────

def build_application() -> Application:
    """Create and configure the Telegram bot Application."""
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    return app
