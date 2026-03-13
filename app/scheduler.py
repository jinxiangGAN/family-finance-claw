"""Scheduled tasks: weekly summary sent to all family members."""

import logging

from telegram.ext import ContextTypes

from app.config import ALLOWED_USER_IDS, CURRENCY, FAMILY_MEMBERS
from app.services.stats_service import get_month_summary, get_month_total, get_spouse_id

logger = logging.getLogger(__name__)


async def weekly_summary_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a weekly summary to all family members."""
    logger.info("Running weekly summary job")

    # Determine who to send to
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

    # Personal summary
    my_summary = get_month_summary([user_id])
    my_total = sum(item["total"] for item in my_summary)

    # Family summary
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

    # Budget check
    from app.database import get_connection
    with get_connection() as conn:
        budget_rows = conn.execute(
            "SELECT category, monthly_limit FROM budgets WHERE user_id = ?",
            (user_id,),
        ).fetchall()

    if budget_rows:
        lines.append("\n📋 *预算情况*")
        from app.services.stats_service import get_category_total
        for row in budget_rows:
            cat = row["category"]
            limit_val = float(row["monthly_limit"])
            if cat == "_total":
                spent = get_month_total([user_id])
                cat_label = "总计"
            else:
                spent = get_category_total(cat, [user_id])
                cat_label = cat
            pct = spent / limit_val * 100 if limit_val > 0 else 0
            status = "🔴" if pct > 100 else "🟡" if pct > 80 else "🟢"
            lines.append(f"  {status} {cat_label}：{spent:.2f}/{limit_val:.2f} {CURRENCY}（{pct:.0f}%）")

    lines.append("\n💡 回复消息即可继续记账！")
    return "\n".join(lines)
