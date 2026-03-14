"""Scheduled tasks: weekly summary + proactive engagement + monthly archive.

Jobs:
1. weekly_summary_job      — Sunday 8PM: comprehensive weekly report
2. proactive_nudge_job     — Fri 6PM: spending check-in & weekend suggestion
3. budget_alert_job        — Daily 9PM: alert if any budget is >80%
4. monthly_archive_job     — 1st of month 1AM: archive previous month's data
"""

import logging
import random
from datetime import datetime

from telegram.ext import ContextTypes

from zoneinfo import ZoneInfo

from app.config import ALLOWED_USER_IDS, CURRENCY, FAMILY_MEMBERS, TIMEZONE
from app.core.memory import get_recent_memories, store_memory
from app.services.stats_service import (
    archive_month,
    get_category_total,
    get_month_summary,
    get_month_total,
    get_spouse_id,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════
#  Weekly Summary (Sunday evening)
# ═══════════════════════════════════════════

async def weekly_summary_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a weekly summary to all family members."""
    logger.info("Running weekly summary job")

    recipients = ALLOWED_USER_IDS if ALLOWED_USER_IDS else list(FAMILY_MEMBERS.keys())
    if not recipients:
        logger.warning("No recipients configured for weekly summary")
        return

    for user_id in recipients:
        try:
            msg = _build_weekly_report(user_id)
            await context.bot.send_message(chat_id=user_id, text=msg, parse_mode="Markdown")
            logger.info("Weekly summary sent to user %s", user_id)
        except Exception:
            logger.exception("Failed to send weekly summary to user %s", user_id)


def _build_weekly_report(user_id: int) -> str:
    """Build a weekly report message for a specific user."""
    name = FAMILY_MEMBERS.get(user_id, str(user_id))

    my_summary = get_month_summary([user_id])
    my_total = sum(item["total"] for item in my_summary)

    family_summary = get_month_summary(None)
    family_total = sum(item["total"] for item in family_summary)

    lines = ["📅 *每周财务报告*\n"]

    # Personal
    lines.append(f"👤 *{name}（本月累计）*")
    if my_summary:
        for item in my_summary:
            lines.append(f"  {item['category']}：{item['total']:.2f} {CURRENCY}")
        lines.append(f"  *小计*：{my_total:.2f} {CURRENCY}")
    else:
        lines.append("  暂无记录")

    # Spouse
    spouse_id = get_spouse_id(user_id)
    if spouse_id is not None:
        spouse_name = FAMILY_MEMBERS.get(spouse_id, str(spouse_id))
        sp_summary = get_month_summary([spouse_id])
        sp_total = sum(item["total"] for item in sp_summary)
        lines.append(f"\n👫 *{spouse_name}（本月累计）*")
        if sp_summary:
            for item in sp_summary:
                lines.append(f"  {item['category']}：{item['total']:.2f} {CURRENCY}")
            lines.append(f"  *小计*：{sp_total:.2f} {CURRENCY}")
        else:
            lines.append("  暂无记录")

    # Family total
    lines.append(f"\n👨‍👩‍👧 *家庭合计*：{family_total:.2f} {CURRENCY}")

    # Budget check (family-shared: user_id=0)
    from app.database import get_connection
    with get_connection() as conn:
        budget_rows = conn.execute(
            "SELECT category, monthly_limit FROM budgets WHERE user_id = 0",
        ).fetchall()

    if budget_rows:
        lines.append("\n📋 *家庭预算情况*")
        for row in budget_rows:
            cat = row["category"]
            limit_val = float(row["monthly_limit"])
            if cat == "_total":
                spent = get_month_total(None)  # family total
                cat_label = "家庭总计"
            else:
                spent = get_category_total(cat, None)
                cat_label = f"家庭{cat}"
            pct = spent / limit_val * 100 if limit_val > 0 else 0
            status = "🔴" if pct > 100 else "🟡" if pct > 80 else "🟢"
            lines.append(f"  {status} {cat_label}：{spent:.2f}/{limit_val:.2f} {CURRENCY}（{pct:.0f}%）")

    # Include relevant memories
    memories = get_recent_memories(user_id, limit=3)
    if memories:
        lines.append("\n🧠 *我记得你们说过*")
        for m in memories:
            if m["category"] in ("goal", "decision"):
                lines.append(f"  💡 {m['content']}")

    lines.append("\n💡 回复消息即可继续记账！")
    return "\n".join(lines)


# ═══════════════════════════════════════════
#  Proactive Nudge (Friday evening)
# ═══════════════════════════════════════════

async def proactive_nudge_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Friday evening: check-in with spending context and weekend suggestion."""
    logger.info("Running proactive nudge job")

    recipients = ALLOWED_USER_IDS if ALLOWED_USER_IDS else list(FAMILY_MEMBERS.keys())
    if not recipients:
        return

    for user_id in recipients:
        try:
            msg = _build_proactive_nudge(user_id)
            if msg:
                await context.bot.send_message(chat_id=user_id, text=msg, parse_mode="Markdown")
                logger.info("Proactive nudge sent to user %s", user_id)
        except Exception:
            logger.exception("Failed to send proactive nudge to user %s", user_id)


def _build_proactive_nudge(user_id: int) -> str:
    """Build a context-aware Friday nudge."""
    name = FAMILY_MEMBERS.get(user_id, str(user_id))
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)

    my_summary = get_month_summary([user_id])
    my_total = sum(item["total"] for item in my_summary)

    # Check budget status (family-shared: user_id=0)
    from app.database import get_connection
    with get_connection() as conn:
        budget_rows = conn.execute(
            "SELECT category, monthly_limit FROM budgets WHERE user_id = 0",
        ).fetchall()

    budget_insights = []
    has_surplus = False
    has_pressure = False

    for row in budget_rows:
        cat = row["category"]
        limit_val = float(row["monthly_limit"])
        if cat == "_total":
            spent = get_month_total(None)  # family total
            cat_label = "家庭总预算"
        else:
            spent = get_category_total(cat, None)
            cat_label = f"家庭{cat}"

        pct = spent / limit_val * 100 if limit_val > 0 else 0
        remaining = limit_val - spent

        if pct < 50:
            has_surplus = True
            budget_insights.append(f"{cat_label}还有 {remaining:.0f} {CURRENCY} 的余量")
        elif pct > 80:
            has_pressure = True
            budget_insights.append(f"{cat_label}已用 {pct:.0f}%，注意控制")

    # Check memory for goals/decisions
    memories = get_recent_memories(user_id, limit=5)
    goal_memories = [m for m in memories if m["category"] in ("goal", "decision")]

    lines = [f"👋 {name}，周末快乐！\n"]

    # Week spending overview
    lines.append(f"📊 本月目前花了 *{my_total:.2f} {CURRENCY}*")
    if my_summary:
        top_cat = my_summary[0]
        lines.append(f"  最大开销：{top_cat['category']}（{top_cat['total']:.2f} {CURRENCY}）")

    # Budget-aware suggestion
    if budget_insights:
        lines.append("")
        for insight in budget_insights[:3]:
            lines.append(f"  💡 {insight}")

    # Contextual weekend suggestion
    lines.append("")
    if has_surplus:
        suggestions = [
            "周末预算还充裕，可以一起出去吃顿好的犒劳自己 🍽️",
            "这周控制得不错！周末可以适当放松一下 ☕",
            "本月消费节奏良好，周末想看个电影吗？🎬",
        ]
    elif has_pressure:
        suggestions = [
            "周末在家做顿饭也不错，最近外面吃得有点多了 🍳",
            "这周花得有点猛，周末可以考虑在家休息 🏠",
            "预算压力有点大，周末一起做个菜、看个剧？📺",
        ]
    else:
        suggestions = [
            "周末有什么计划吗？记得记账哦 📝",
            "快乐的周末来了！别忘了有什么花销都发给我 😊",
        ]
    lines.append(random.choice(suggestions))

    # Recall relevant goals
    if goal_memories:
        lines.append(f"\n🧠 提醒：{goal_memories[0]['content']}")

    return "\n".join(lines)


# ═══════════════════════════════════════════
#  Daily Budget Alert (every evening)
# ═══════════════════════════════════════════

async def budget_alert_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily check: alert users if any budget is over 80%."""
    logger.info("Running daily budget alert check")

    recipients = ALLOWED_USER_IDS if ALLOWED_USER_IDS else list(FAMILY_MEMBERS.keys())
    if not recipients:
        return

    from app.database import get_connection

    for user_id in recipients:
        try:
            alerts = []
            with get_connection() as conn:
                budget_rows = conn.execute(
                    "SELECT category, monthly_limit FROM budgets WHERE user_id = 0",
                ).fetchall()

            for row in budget_rows:
                cat = row["category"]
                limit_val = float(row["monthly_limit"])
                if cat == "_total":
                    spent = get_month_total(None)  # family total
                    cat_label = "家庭总预算"
                else:
                    spent = get_category_total(cat, None)
                    cat_label = f"家庭{cat}"

                pct = spent / limit_val * 100 if limit_val > 0 else 0

                if pct >= 100:
                    alerts.append(f"🔴 {cat_label}已超支！（{spent:.2f}/{limit_val:.2f} {CURRENCY}，{pct:.0f}%）")
                elif pct >= 90:
                    alerts.append(f"🟡 {cat_label}即将用完（{spent:.2f}/{limit_val:.2f} {CURRENCY}，{pct:.0f}%）")

            if alerts:
                name = FAMILY_MEMBERS.get(user_id, str(user_id))
                msg = f"⚠️ *{name}，家庭预算预警*\n\n" + "\n".join(alerts) + "\n\n注意控制接下来的支出哦！"
                await context.bot.send_message(chat_id=user_id, text=msg, parse_mode="Markdown")
                logger.info("Budget alert sent to user %s: %d alerts", user_id, len(alerts))

        except Exception:
            logger.exception("Failed to check budget for user %s", user_id)


# ═══════════════════════════════════════════
#  Monthly Archive (1st of each month)
# ═══════════════════════════════════════════

async def monthly_archive_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Archive last month's expense summary into monthly_summaries table.

    Runs on the 1st of each month at 1AM. Safe to run multiple times
    (UPSERT — won't duplicate data).
    """
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)

    # Archive the PREVIOUS month
    if now.month == 1:
        year, month = now.year - 1, 12
    else:
        year, month = now.year, now.month - 1

    logger.info("Running monthly archive job for %04d-%02d", year, month)
    try:
        count = archive_month(year, month)
        logger.info("Monthly archive complete: %d rows for %04d-%02d", count, year, month)

        # Notify family members
        recipients = ALLOWED_USER_IDS if ALLOWED_USER_IDS else list(FAMILY_MEMBERS.keys())
        if recipients and count > 0:
            msg = (
                f"📦 *{year}年{month}月账单已归档*\n\n"
                f"共 {count} 条汇总记录已保存。\n"
                "随时可以问我「上个月花了多少」来查看！"
            )
            for uid in recipients:
                try:
                    await context.bot.send_message(chat_id=uid, text=msg, parse_mode="Markdown")
                except Exception:
                    logger.exception("Failed to send archive notification to %s", uid)

    except Exception:
        logger.exception("Monthly archive job failed for %04d-%02d", year, month)
