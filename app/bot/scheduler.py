"""Scheduled tasks: weekly summary + proactive engagement + monthly archive.

Jobs:
1. weekly_summary_job      — Sunday 8PM: comprehensive weekly report
2. proactive_nudge_job     — Fri 6PM: spending check-in & weekend suggestion
3. monthly_archive_job     — 1st of month 1AM: archive previous month's data
"""

import logging
import random
from datetime import datetime

from telegram.ext import ContextTypes

from zoneinfo import ZoneInfo

from app.config import ALLOWED_USER_IDS, CURRENCY, FAMILY_MEMBERS, TIMEZONE
from app.core.memory import get_recent_memories
from app.database import get_connection
from app.services.stats_service import (
    archive_month,
    get_category_total,
    get_last_n_days_summary,
    get_monthly_archive,
    upsert_monthly_report,
    get_month_total,
    get_spouse_id,
)
from app.services.household_service import get_goal_progress, get_recurring_status, get_spending_anomalies

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

    my_summary = get_last_n_days_summary(7, [user_id])
    my_total = sum(item["total"] for item in my_summary)

    family_summary = get_last_n_days_summary(7, None)
    family_total = sum(item["total"] for item in family_summary)

    lines = ["📅 *每周财务报告*\n"]

    # Personal
    lines.append(f"👤 *{name}（最近7天）*")
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
        sp_summary = get_last_n_days_summary(7, [spouse_id])
        sp_total = sum(item["total"] for item in sp_summary)
        lines.append(f"\n👫 *{spouse_name}（最近7天）*")
        if sp_summary:
            for item in sp_summary:
                lines.append(f"  {item['category']}：{item['total']:.2f} {CURRENCY}")
            lines.append(f"  *小计*：{sp_total:.2f} {CURRENCY}")
        else:
            lines.append("  暂无记录")

    # Family total
    lines.append(f"\n👨‍👩‍👧 *家庭最近7天合计*：{family_total:.2f} {CURRENCY}")

    # Budget check (family-shared: user_id=0)
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

    anomalies = get_spending_anomalies(request_user_id=user_id, scope="me")
    if anomalies.get("anomalies"):
        first = anomalies["anomalies"][0]
        lines.append(f"\n⚠️ *异常波动*：{first['label']} 比最近 3 个月均值多了 {float(first['delta']):.0f} {CURRENCY}")

    goals = get_goal_progress(user_id)
    goal_items = goals.get("items") or []
    if goal_items:
        first_goal = goal_items[0]
        goal_label = "总支出" if first_goal["category"] == "_total" else first_goal["category"]
        lines.append(
            f"\n🎯 *目标进度*：{goal_label} {float(first_goal['spent']):.0f}/{float(first_goal['target_amount']):.0f} {CURRENCY}"
        )

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

    my_summary = get_last_n_days_summary(7, [user_id])
    my_total = sum(item["total"] for item in my_summary)

    # Check budget status (family-shared: user_id=0)
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
    lines.append(f"📊 最近7天花了 *{my_total:.2f} {CURRENCY}*")
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

    recurring = get_recurring_status(user_id)
    overdue = [item for item in (recurring.get("items") or []) if item.get("status") == "overdue"]
    if overdue:
        lines.append(f"\n🧾 还有 {len(overdue)} 个固定账单这月还没记，可以顺手补一下。")

    anomalies = get_spending_anomalies(request_user_id=user_id, scope="me")
    if anomalies.get("anomalies"):
        first = anomalies["anomalies"][0]
        lines.append(f"\n⚠️ 最近 {first['label']} 有点冲，高于过去均值。")

    return "\n".join(lines)


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

        family_payload = _build_family_monthly_report_payload(year, month, count)
        upsert_monthly_report(
            year=year,
            month=month,
            user_id=0,
            total=float(family_payload["total"]),
            currency=CURRENCY,
            report_text=str(family_payload["report_text"]),
            report_payload=family_payload,
        )

        # Notify family members
        recipients = ALLOWED_USER_IDS if ALLOWED_USER_IDS else list(FAMILY_MEMBERS.keys())
        if recipients and count > 0:
            for uid in recipients:
                try:
                    payload = _build_monthly_report_payload(uid, year, month, count)
                    upsert_monthly_report(
                        year=year,
                        month=month,
                        user_id=uid,
                        total=float(payload["total"]),
                        currency=CURRENCY,
                        report_text=str(payload["report_text"]),
                        report_payload=payload,
                    )
                    await context.bot.send_message(chat_id=uid, text=str(payload["report_text"]), parse_mode="Markdown")
                except Exception:
                    logger.exception("Failed to send archive notification to %s", uid)

    except Exception:
        logger.exception("Monthly archive job failed for %04d-%02d", year, month)


def _build_monthly_report_payload(user_id: int, year: int, month: int, archived_count: int) -> dict:
    """Build and persist-friendly monthly report payload for one person view."""
    name = FAMILY_MEMBERS.get(user_id, str(user_id))
    my_rows = get_monthly_archive(year, month, user_id=user_id)
    my_total = sum(r["total"] for r in my_rows)

    spouse_id = get_spouse_id(user_id)
    spouse_rows = get_monthly_archive(year, month, user_id=spouse_id) if spouse_id is not None else []
    spouse_total = sum(r["total"] for r in spouse_rows)

    family_rows = get_monthly_archive(year, month, user_id=None)
    family_total = sum(r["total"] for r in family_rows)

    lines = [f"📦 *{year}年{month}月总结*\n"]
    lines.append(f"👤 *{name}*：{my_total:.2f} {CURRENCY}")
    for row in my_rows[:5]:
        lines.append(f"  {row['category']}：{row['total']:.2f} {CURRENCY}")

    if spouse_id is not None:
        spouse_name = FAMILY_MEMBERS.get(spouse_id, str(spouse_id))
        lines.append(f"\n👫 *{spouse_name}*：{spouse_total:.2f} {CURRENCY}")
        for row in spouse_rows[:5]:
            lines.append(f"  {row['category']}：{row['total']:.2f} {CURRENCY}")

    lines.append(f"\n👨‍👩‍👧 *家庭合计*：{family_total:.2f} {CURRENCY}")
    for row in family_rows[:5]:
        lines.append(f"  {row['category']}：{row['total']:.2f} {CURRENCY}")

    monthly_memories = _get_monthly_memories(user_id, year, month, limit=5)
    goals = get_goal_progress(user_id, year=year, month=month)
    goal_items = goals.get("items") or []
    if monthly_memories:
        lines.append("\n🧠 *相关提醒*")
        for memory in monthly_memories[:2]:
            lines.append(f"  💡 {memory['content']}")

    if goal_items:
        lines.append("\n🎯 *本月目标状态*")
        for item in goal_items[:3]:
            goal_label = "总支出" if item["category"] == "_total" else item["category"]
            status = "🟢" if item["on_track"] else "🟡"
            lines.append(
                f"  {status} {goal_label}：{float(item['spent']):.2f}/{float(item['target_amount']):.2f} {CURRENCY}"
            )

    lines.append(f"\n已归档 {archived_count} 条月度汇总记录。")
    lines.append("现在你可以直接问我：上个月花了多少、上个月餐饮花了多少。")
    return {
        "scope": "personal_view",
        "user_id": user_id,
        "year": year,
        "month": month,
        "total": round(my_total, 2),
        "currency": CURRENCY,
        "my_summary": my_rows,
        "spouse_summary": spouse_rows,
        "family_summary": family_rows,
        "memories": monthly_memories,
        "goals": goal_items,
        "report_text": "\n".join(lines),
    }


def _build_family_monthly_report_payload(year: int, month: int, archived_count: int) -> dict:
    family_rows = get_monthly_archive(year, month, user_id=None)
    family_total = sum(r["total"] for r in family_rows)
    monthly_memories = _get_monthly_memories(0, year, month, limit=5)
    goals = get_goal_progress(0, year=year, month=month)
    family_goals = [item for item in (goals.get("items") or []) if item.get("scope") == "family"]

    lines = [f"📦 *{year}年{month}月家庭总结*\n"]
    lines.append(f"👨‍👩‍👧 *家庭合计*：{family_total:.2f} {CURRENCY}")
    for row in family_rows[:8]:
        lines.append(f"  {row['category']}：{row['total']:.2f} {CURRENCY}")

    if monthly_memories:
        lines.append("\n🧠 *家庭相关提醒*")
        for memory in monthly_memories[:3]:
            lines.append(f"  💡 {memory['content']}")

    if family_goals:
        lines.append("\n🎯 *家庭目标状态*")
        for item in family_goals[:3]:
            goal_label = "总支出" if item["category"] == "_total" else item["category"]
            status = "🟢" if item["on_track"] else "🟡"
            lines.append(
                f"  {status} {goal_label}：{float(item['spent']):.2f}/{float(item['target_amount']):.2f} {CURRENCY}"
            )

    lines.append(f"\n已归档 {archived_count} 条月度汇总记录。")
    return {
        "scope": "family",
        "user_id": 0,
        "year": year,
        "month": month,
        "total": round(family_total, 2),
        "currency": CURRENCY,
        "family_summary": family_rows,
        "memories": monthly_memories,
        "goals": family_goals,
        "report_text": "\n".join(lines),
    }


def _get_monthly_memories(user_id: int, year: int, month: int, limit: int = 5) -> list[dict]:
    """Return only memories created within the target month."""
    tz = ZoneInfo(TIMEZONE)
    start = datetime(year, month, 1, tzinfo=tz)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=tz)
    else:
        end = datetime(year, month + 1, 1, tzinfo=tz)

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, user_id, content, category, importance, created_at "
            "FROM episodic_memories "
            "WHERE user_id IN (?, 0) "
            "AND category IN ('goal', 'decision') "
            "AND datetime(created_at) >= datetime(?) "
            "AND datetime(created_at) < datetime(?) "
            "ORDER BY importance DESC, datetime(created_at) DESC "
            "LIMIT ?",
            (user_id, start.isoformat(), end.isoformat(), limit),
        ).fetchall()

    return [
        {
            "id": row["id"],
            "content": row["content"],
            "category": row["category"],
            "importance": row["importance"],
            "created_at": row["created_at"],
            "scope": "family" if row["user_id"] == 0 else "personal",
        }
        for row in rows
    ]
