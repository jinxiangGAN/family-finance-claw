"""MCP Tool: Query & analytics — monthly totals, summaries, analysis."""

from app.config import CATEGORIES
from app.services.skills import (
    skill_query_category_items,
    skill_query_recent_expenses,
    skill_get_spending_analysis,
    skill_query_balance_status,
    skill_query_budget,
    skill_query_budget_changes,
    skill_query_category_total,
    skill_query_exchange_rate,
    skill_query_goal_progress,
    skill_query_monthly_archive,
    skill_query_monthly_total,
    skill_query_period_comparison,
    skill_query_recurring_status,
    skill_query_spending_anomalies,
    skill_query_summary,
    skill_set_budget,
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_recurring_status",
            "description": "Query this month's recurring bill status to see what has already been logged and what is still missing.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_period_comparison",
            "description": "Compare this month against last month for total spending or a specific category.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "description": "Query scope", "enum": ["me", "spouse", "family"]},
                    "category": {"type": "string", "description": "Optional category filter", "enum": ["", *CATEGORIES]},
                    "include_special": {"type": "boolean", "description": "Whether to include special/event expenses. Default false."},
                },
                "required": ["scope"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_monthly_total",
            "description": "Query the total spending for the current month.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "description": "Query scope: personal, spouse, or whole family", "enum": ["me", "spouse", "family"]},
                    "include_special": {"type": "boolean", "description": "Whether to include special/event expenses. Default false."},
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
                    "include_special": {"type": "boolean", "description": "Whether to include special/event expenses. Default false."},
                },
                "required": ["category", "scope"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_category_items",
            "description": "Query itemized expenses for a specific category in the current month.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "Expense category to query", "enum": CATEGORIES},
                    "scope": {"type": "string", "description": "Query scope", "enum": ["me", "spouse", "family"]},
                    "limit": {"type": "integer", "description": "Max number of records to return, default 20"},
                    "include_special": {"type": "boolean", "description": "Whether to include special/event expenses. Default false."},
                },
                "required": ["category", "scope"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_recent_expenses",
            "description": "Query the most recent expenses. Use this before deleting by ID or when the user asks for recent records.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "description": "Query scope", "enum": ["me", "spouse", "family"]},
                    "category": {"type": "string", "description": "Optional category filter", "enum": ["", *CATEGORIES]},
                    "limit": {"type": "integer", "description": "Max number of records to return, default 10, max 30"},
                    "ledger_type": {"type": "string", "description": "Optional ledger filter", "enum": ["", "regular", "special"]},
                },
                "required": ["scope"],
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
                    "include_special": {"type": "boolean", "description": "Whether to include special/event expenses. Default false."},
                },
                "required": ["scope"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_budget",
            "description": "设置家庭共享月度预算。既支持单分类预算，也支持多个分类共用一个组合预算。预算对全家生效，支出按全家合计计算。",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "单分类预算时填写。'_total' 表示家庭总预算，其他如 '餐饮'、'交通' 等"},
                    "categories": {
                        "type": "array",
                        "description": "组合预算时填写多个分类，例如 ['餐饮','交通','超市']",
                        "items": {"type": "string", "enum": CATEGORIES},
                    },
                    "budget_name": {"type": "string", "description": "可选：组合预算名称，例如 '三项日常预算'"},
                    "amount": {"type": "number", "description": "每月预算金额"},
                    "note": {"type": "string", "description": "可选：这次调整预算的原因或备注"},
                },
                "required": ["amount"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_balance_status",
            "description": "Query who owes whom right now, optionally for a specific event/trip tag.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_tag": {"type": "string", "description": "Optional event/trip tag"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_spending_anomalies",
            "description": "Detect current-month spending anomalies compared with the trailing three months.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "description": "Query scope", "enum": ["me", "spouse", "family"]},
                    "include_special": {"type": "boolean", "description": "Whether to include special/event expenses. Default false."},
                },
                "required": ["scope"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_goal_progress",
            "description": "Query progress against monthly spending goals.",
            "parameters": {"type": "object", "properties": {}},
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
            "name": "query_budget_changes",
            "description": "查询最近的预算调整历史。可回答'预算改过什么'、'房租预算什么时候调过'等问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "可选：只查询某个预算分类，'_total' 表示总预算"},
                    "budget_name": {"type": "string", "description": "可选：只查询某个组合预算名称"},
                    "limit": {"type": "integer", "description": "最多返回多少条，默认10，最大30"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_exchange_rate",
            "description": "查询两种货币之间的汇率。优先使用在线汇率，失败时回退到缓存或参考汇率。",
            "parameters": {
                "type": "object",
                "properties": {
                    "base_currency": {"type": "string", "description": "基础货币，例如 USD/CNY/SGD"},
                    "quote_currency": {"type": "string", "description": "目标货币，例如 SGD。默认是系统默认货币。"},
                },
                "required": ["base_currency"],
            },
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
                    "include_special": {"type": "boolean", "description": "Whether to include special/event expenses. Default false."},
                },
                "required": ["scope"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_monthly_archive",
            "description": "查询历史月度账单汇总（已归档的月份）。可以回答'上个月花了多少'、'2月份开支'等问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "year": {"type": "integer", "description": "年份，如 2026"},
                    "month": {"type": "integer", "description": "月份，1-12"},
                    "scope": {"type": "string", "description": "查询范围", "enum": ["me", "spouse", "family"]},
                },
                "required": ["year", "month"],
            },
        },
    },
]

HANDLERS = {
    "query_monthly_total": skill_query_monthly_total,
    "query_category_total": skill_query_category_total,
    "query_category_items": skill_query_category_items,
    "query_recent_expenses": skill_query_recent_expenses,
    "query_recurring_status": skill_query_recurring_status,
    "query_period_comparison": skill_query_period_comparison,
    "query_summary": skill_query_summary,
    "query_balance_status": skill_query_balance_status,
    "query_spending_anomalies": skill_query_spending_anomalies,
    "query_goal_progress": skill_query_goal_progress,
    "set_budget": skill_set_budget,
    "query_budget": skill_query_budget,
    "query_budget_changes": skill_query_budget_changes,
    "query_exchange_rate": skill_query_exchange_rate,
    "get_spending_analysis": skill_get_spending_analysis,
    "query_monthly_archive": skill_query_monthly_archive,
}
