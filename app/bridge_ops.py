"""Whitelisted bridge operations for the Telegram -> Codex workflow.

This module exposes a narrow CLI so Codex can read and write finance facts
through approved repository helpers instead of ad-hoc shell logic.
"""

from __future__ import annotations

import argparse
import json
import re
from typing import Any

from app.core.memory import get_memory_manager, get_recent_memories, store_memory
from app.database import init_db
from app.services.expense_service import get_recent_expenses
from app.services.skills import execute_skill

_CJK_RE = re.compile(r"[\u3400-\u9fff]")


def snapshot(user_id: int) -> dict[str, Any]:
    mm = get_memory_manager()
    return {
        "recent_expenses": [
            {
                "id": exp.id,
                "category": exp.category,
                "amount": exp.amount,
                "currency": exp.currency,
                "note": exp.note,
                "event_tag": exp.event_tag,
                "ledger_type": exp.ledger_type,
                "created_at": exp.created_at,
            }
            for exp in get_recent_expenses(user_id, limit=8)
        ],
        "recent_memories": get_recent_memories(user_id, limit=8),
        "profile": mm.get_all_profile_keys(user_id),
    }


def run_skill(user_id: int, user_name: str, name: str, params: dict[str, Any]) -> dict[str, Any]:
    return execute_skill(name, user_id, user_name, params)


def store_memory_entry(
    *,
    user_id: int,
    content: str,
    category: str = "general",
    importance: int = 5,
    shared: bool = False,
) -> dict[str, Any]:
    if _CJK_RE.search(content):
        return {
            "success": False,
            "message": "Memory content must be stored in English. Rewrite it into concise English first.",
        }
    target_user_id = 0 if shared else user_id
    memory_id = store_memory(
        target_user_id,
        content,
        category=category,
        importance=importance,
    )
    return {"success": True, "memory_id": memory_id}


def main() -> None:
    parser = argparse.ArgumentParser(description="Whitelisted bridge operations")
    sub = parser.add_subparsers(dest="command", required=True)

    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("--user-id", type=int, required=True)

    skill = sub.add_parser("skill")
    skill.add_argument("--user-id", type=int, required=True)
    skill.add_argument("--user-name", required=True)
    skill.add_argument("--name", required=True)
    skill.add_argument("--params-json", default="{}")

    store = sub.add_parser("store-memory")
    store.add_argument("--user-id", type=int, required=True)
    store.add_argument("--content", required=True)
    store.add_argument("--category", default="general")
    store.add_argument("--importance", type=int, default=5)
    store.add_argument("--shared", action="store_true")

    args = parser.parse_args()
    init_db()

    if args.command == "snapshot":
        print(json.dumps(snapshot(args.user_id), ensure_ascii=False))
        return

    if args.command == "skill":
        params = json.loads(args.params_json or "{}")
        result = run_skill(args.user_id, args.user_name, args.name, params)
        print(json.dumps(result, ensure_ascii=False))
        return

    if args.command == "store-memory":
        print(
            json.dumps(
                store_memory_entry(
                    user_id=args.user_id,
                    content=args.content,
                    category=args.category,
                    importance=args.importance,
                    shared=args.shared,
                ),
                ensure_ascii=False,
            )
        )
        return


if __name__ == "__main__":
    main()
