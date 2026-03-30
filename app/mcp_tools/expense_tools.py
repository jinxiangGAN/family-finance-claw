"""MCP Tool: Expense management — record, delete, export."""

from app.services.skills import (
    skill_delete_expense_by_id,
    skill_delete_last,
    skill_export_csv,
    skill_record_expense,
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
    "export_csv": skill_export_csv,
}
