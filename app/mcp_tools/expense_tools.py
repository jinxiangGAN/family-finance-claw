"""MCP Tool: Expense management — record, delete, export."""

from app.services.skills import (
    skill_delete_expense_by_id,
    skill_delete_last,
    skill_export_csv,
    skill_record_settlement,
    skill_record_expense,
    skill_set_recurring_rule,
    skill_set_spending_goal,
)
from app.config import CATEGORIES, CURRENCY

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "record_expense",
            "description": (
                "Record a single expense. Call this when the user mentions a specific spending amount and category. "
                "Supports multi-currency (auto-converts to default) and event/trip tags."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "Expense category", "enum": CATEGORIES},
                    "amount": {"type": "number", "description": "Amount spent (numeric)"},
                    "note": {"type": "string", "description": "Brief description of the expense"},
                    "currency": {"type": "string", "description": f"ISO currency code, default: {CURRENCY}"},
                    "event_tag": {"type": "string", "description": "Event/trip tag (leave empty to auto-use the active event tag)"},
                    "ledger_type": {"type": "string", "description": "regular for normal spending, special for project/trip spending", "enum": ["regular", "special"]},
                },
                "required": ["category", "amount", "note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_last_expense",
            "description": "Delete the user's most recently recorded expense. Use when the user says 'undo', 'delete last', or indicates a mistake.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_expense_by_id",
            "description": "Delete a specific expense by its ID. Use after confirming the exact record the user wants removed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expense_id": {"type": "integer", "description": "The expense ID to delete"},
                },
                "required": ["expense_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_recurring_rule",
            "description": "Create or update a recurring bill such as rent, subscriptions, or utilities.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Recurring bill name"},
                    "category": {"type": "string", "description": "Expense category", "enum": CATEGORIES},
                    "amount": {"type": "number", "description": "Recurring amount"},
                    "currency": {"type": "string", "description": f"Currency code, default: {CURRENCY}"},
                    "due_day": {"type": "integer", "description": "Monthly due day (1-31)"},
                    "match_text": {"type": "string", "description": "Optional keyword to identify matching expense notes"},
                    "note": {"type": "string", "description": "Optional note"},
                    "shared": {"type": "boolean", "description": "Whether this is a family-shared recurring bill"},
                },
                "required": ["name", "category", "amount", "due_day"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_settlement",
            "description": "Record a settlement transfer between family members after AA or an advance payment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "from_user_id": {"type": "integer", "description": "Payer user id"},
                    "to_user_id": {"type": "integer", "description": "Receiver user id"},
                    "amount": {"type": "number", "description": "Settlement amount"},
                    "currency": {"type": "string", "description": f"Currency code, default: {CURRENCY}"},
                    "note": {"type": "string", "description": "Optional note"},
                    "event_tag": {"type": "string", "description": "Optional event/trip tag"},
                },
                "required": ["from_user_id", "to_user_id", "amount"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_spending_goal",
            "description": "Set or update a monthly spending goal for a category or total spending.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "Target category or _total", "enum": ["_total", *CATEGORIES]},
                    "target_amount": {"type": "number", "description": "Monthly target ceiling"},
                    "shared": {"type": "boolean", "description": "Whether this goal is family shared"},
                    "include_special": {"type": "boolean", "description": "Whether to include special/event spending"},
                    "note": {"type": "string", "description": "Optional note"},
                },
                "required": ["category", "target_amount"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_csv",
            "description": "Export expense records to a CSV file. Call when the user says 'export', 'download data', etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "description": "Export scope", "enum": ["me", "family"]},
                    "event_tag": {"type": "string", "description": "Only export expenses under this event tag"},
                },
            },
        },
    },
]

HANDLERS = {
    "record_expense": skill_record_expense,
    "delete_last_expense": skill_delete_last,
    "delete_expense_by_id": skill_delete_expense_by_id,
    "set_recurring_rule": skill_set_recurring_rule,
    "record_settlement": skill_record_settlement,
    "set_spending_goal": skill_set_spending_goal,
    "export_csv": skill_export_csv,
}
