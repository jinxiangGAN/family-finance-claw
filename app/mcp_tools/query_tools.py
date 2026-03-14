"""MCP Tool: Query & analytics — monthly totals, summaries, analysis."""

from app.config import CATEGORIES
from app.services.skills import (
    skill_get_spending_analysis,
    skill_query_budget,
    skill_query_category_total,
    skill_query_monthly_total,
    skill_query_summary,
    skill_set_budget,
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_monthly_total",
            "description": "Query the total spending for the current month.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "description": "Query scope: personal, spouse, or whole family", "enum": ["me", "spouse", "family"]},
                },
                "required": ["scope"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_category_total",
            "description": "Query spending for a specific category in the current month.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "Expense category to query", "enum": CATEGORIES},
                    "scope": {"type": "string", "description": "Query scope", "enum": ["me", "spouse", "family"]},
                },
                "required": ["category", "scope"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_summary",
            "description": "Query a category-wise spending breakdown for the current month.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "description": "Query scope", "enum": ["me", "spouse", "family"]},
                },
                "required": ["scope"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_budget",
            "description": "设置家庭共享月度预算上限。预算对全家生效，支出按全家合计计算。category 为 '_total' 表示家庭总预算。",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "预算分类。'_total' 表示家庭总预算，其他如 '餐饮'、'交通' 等"},
                    "amount": {"type": "number", "description": "每月预算金额"},
                },
                "required": ["category", "amount"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_budget",
            "description": "查询家庭预算使用情况。预算是全家共享的，支出按全家合计。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_spending_analysis",
            "description": "Retrieve spending data and patterns for financial analysis and advice.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "description": "Analysis scope", "enum": ["me", "spouse", "family"]},
                },
                "required": ["scope"],
            },
        },
    },
]

HANDLERS = {
    "query_monthly_total": skill_query_monthly_total,
    "query_category_total": skill_query_category_total,
    "query_summary": skill_query_summary,
    "set_budget": skill_set_budget,
    "query_budget": skill_query_budget,
    "get_spending_analysis": skill_get_spending_analysis,
}
