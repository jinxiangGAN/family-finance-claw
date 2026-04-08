"""MCP query tool exposure built from the central skill registry."""

from __future__ import annotations

from app.services.skills import SKILL_MAP, TOOL_DEFINITIONS

_QUERY_TOOL_NAMES = (
    "query_recurring_status",
    "query_period_comparison",
    "query_monthly_total",
    "query_category_total",
    "query_category_items",
    "query_recent_expenses",
    "query_summary",
    "query_period_spending",
    "set_budget",
    "query_balance_status",
    "query_spending_anomalies",
    "query_goal_progress",
    "query_budget",
    "query_budget_changes",
    "query_exchange_rate",
    "get_spending_analysis",
    "query_monthly_archive",
)

TOOLS = [
    definition
    for definition in TOOL_DEFINITIONS
    if str(definition.get("function", {}).get("name") or "") in _QUERY_TOOL_NAMES
]

HANDLERS = {name: SKILL_MAP[name] for name in _QUERY_TOOL_NAMES}
