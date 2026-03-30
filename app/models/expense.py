"""Data models for expenses."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Expense:
    """Represents a single expense record."""

    user_id: int
    user_name: str
    category: str
    amount: float
    note: str
    currency: str = "SGD"
    amount_sgd: float = 0.0
    event_tag: str = ""
    ledger_type: str = "regular"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    id: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "category": self.category,
            "amount": self.amount,
            "currency": self.currency,
            "amount_sgd": self.amount_sgd,
            "note": self.note,
            "event_tag": self.event_tag,
            "ledger_type": self.ledger_type,
            "created_at": self.created_at,
        }


@dataclass
class ParsedExpense:
    """Result from MiniMax parsing."""

    intent: str  # "expense" | "query" | "unknown"
    category: Optional[str] = None
    amount: Optional[float] = None
    note: Optional[str] = None
    query_type: Optional[str] = None  # "monthly_total" | "category_total" | "summary"
    scope: str = "me"  # "me" | "spouse" | "family"
    raw_text: str = ""
