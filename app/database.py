"""SQLite database initialization and connection management.

Memory tables follow a 3-tier architecture:
  - core_profiles: persistent user profiles & financial goals
  - episodic_memories: important events with optional vector embedding
  - memories_fts: FTS5 index for text-based recall
"""

import logging
import os
import sqlite3
from contextlib import contextmanager
from typing import Generator

from app.config import DATABASE_PATH

logger = logging.getLogger(__name__)

CREATE_TABLES_SQL = [
    # ── Core business tables ──
    """
    CREATE TABLE IF NOT EXISTS expenses (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        user_name   TEXT    NOT NULL DEFAULT '',
        category    TEXT    NOT NULL,
        amount      REAL    NOT NULL,
        currency    TEXT    NOT NULL DEFAULT 'SGD',
        amount_sgd  REAL    NOT NULL DEFAULT 0,
        note        TEXT    NOT NULL DEFAULT '',
        event_tag   TEXT    NOT NULL DEFAULT '',
        ledger_type TEXT    NOT NULL DEFAULT 'regular',
        created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS budgets (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        category    TEXT    NOT NULL DEFAULT '_total',
        monthly_limit REAL  NOT NULL,
        updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, category)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS budget_changes (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        budget_user_id  INTEGER NOT NULL,
        category        TEXT    NOT NULL DEFAULT '_total',
        old_limit       REAL,
        new_limit       REAL    NOT NULL,
        changed_by_id   INTEGER NOT NULL,
        changed_by_name TEXT    NOT NULL DEFAULT '',
        note            TEXT    NOT NULL DEFAULT '',
        created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS budget_groups (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id       INTEGER NOT NULL,
        name          TEXT    NOT NULL,
        monthly_limit REAL    NOT NULL,
        updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, name)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS budget_group_categories (
        group_id   INTEGER NOT NULL,
        category   TEXT    NOT NULL,
        PRIMARY KEY (group_id, category)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS budget_group_changes (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        budget_user_id    INTEGER NOT NULL,
        group_name        TEXT    NOT NULL,
        old_limit         REAL,
        new_limit         REAL    NOT NULL,
        old_categories    TEXT    NOT NULL DEFAULT '',
        new_categories    TEXT    NOT NULL DEFAULT '',
        changed_by_id     INTEGER NOT NULL,
        changed_by_name   TEXT    NOT NULL DEFAULT '',
        note              TEXT    NOT NULL DEFAULT '',
        created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS budget_alert_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        year        INTEGER NOT NULL,
        month       INTEGER NOT NULL,
        category    TEXT    NOT NULL DEFAULT '_total',
        alert_level TEXT    NOT NULL,
        sent_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(year, month, category, alert_level)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS api_usage (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         INTEGER NOT NULL,
        prompt_tokens   INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        total_tokens    INTEGER NOT NULL DEFAULT 0,
        model           TEXT    NOT NULL DEFAULT '',
        created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        tag         TEXT    NOT NULL,
        description TEXT    NOT NULL DEFAULT '',
        is_active   INTEGER NOT NULL DEFAULT 1,
        status      TEXT    NOT NULL DEFAULT 'active',
        created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, tag)
    );
    """,

    # ── Memory Layer: Tier 1 — Core Profile ──
    # Persistent user profile: financial goals, preferences, personality traits.
    # One row per (user_id, key). Updated in-place via UPSERT.
    """
    CREATE TABLE IF NOT EXISTS core_profiles (
        user_id     INTEGER NOT NULL,
        key         TEXT    NOT NULL,
        value       TEXT    NOT NULL DEFAULT '',
        updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, key)
    );
    """,

    # ── Memory Layer: Tier 3 — Episodic Memory ──
    # Important events, decisions, spending patterns from past conversations.
    # Optional embedding BLOB for vector search (struct-packed float32 array).
    """
    CREATE TABLE IF NOT EXISTS episodic_memories (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        content     TEXT    NOT NULL,
        category    TEXT    NOT NULL DEFAULT 'general',
        importance  INTEGER NOT NULL DEFAULT 5,
        embedding   BLOB,
        is_active   INTEGER NOT NULL DEFAULT 1,
        archived_at TIMESTAMP,
        supersedes_memory_id INTEGER,
        created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,

    # ── Monthly archive snapshots ──
    # Auto-generated on the 1st of each month for the previous month.
    # user_id=0 → family total; individual user_id → personal breakdown.
    """
    CREATE TABLE IF NOT EXISTS monthly_summaries (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        year        INTEGER NOT NULL,
        month       INTEGER NOT NULL,
        user_id     INTEGER NOT NULL,
        category    TEXT    NOT NULL,
        total       REAL    NOT NULL DEFAULT 0,
        currency    TEXT    NOT NULL DEFAULT 'SGD',
        created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(year, month, user_id, category)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS monthly_reports (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        year          INTEGER NOT NULL,
        month         INTEGER NOT NULL,
        user_id       INTEGER NOT NULL,
        total         REAL    NOT NULL DEFAULT 0,
        currency      TEXT    NOT NULL DEFAULT 'SGD',
        report_text   TEXT    NOT NULL DEFAULT '',
        report_payload TEXT   NOT NULL DEFAULT '{}',
        created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(year, month, user_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS fx_rates (
        base_currency   TEXT    NOT NULL,
        quote_currency  TEXT    NOT NULL,
        rate            REAL    NOT NULL,
        effective_date  TEXT    NOT NULL DEFAULT '',
        source          TEXT    NOT NULL DEFAULT 'live',
        fetched_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (base_currency, quote_currency)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS recurring_rules (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id       INTEGER NOT NULL,
        name          TEXT    NOT NULL,
        category      TEXT    NOT NULL DEFAULT '其他',
        amount        REAL    NOT NULL,
        currency      TEXT    NOT NULL DEFAULT 'SGD',
        due_day       INTEGER NOT NULL DEFAULT 1,
        match_text    TEXT    NOT NULL DEFAULT '',
        note          TEXT    NOT NULL DEFAULT '',
        is_active     INTEGER NOT NULL DEFAULT 1,
        created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, name)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS settlement_records (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        from_user_id  INTEGER NOT NULL,
        to_user_id    INTEGER NOT NULL,
        amount        REAL    NOT NULL,
        currency      TEXT    NOT NULL DEFAULT 'SGD',
        amount_sgd    REAL    NOT NULL DEFAULT 0,
        note          TEXT    NOT NULL DEFAULT '',
        event_tag     TEXT    NOT NULL DEFAULT '',
        created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS spending_goals (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         INTEGER NOT NULL,
        category        TEXT    NOT NULL DEFAULT '_total',
        target_amount   REAL    NOT NULL,
        currency        TEXT    NOT NULL DEFAULT 'SGD',
        period          TEXT    NOT NULL DEFAULT 'monthly',
        include_special INTEGER NOT NULL DEFAULT 0,
        note            TEXT    NOT NULL DEFAULT '',
        is_active       INTEGER NOT NULL DEFAULT 1,
        created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, category, period)
    );
    """,

    # Legacy flat memories table (kept for backward compat migration)
    """
    CREATE TABLE IF NOT EXISTS memories (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        content     TEXT    NOT NULL,
        category    TEXT    NOT NULL DEFAULT 'general',
        importance  INTEGER NOT NULL DEFAULT 5,
        created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
]

# FTS5 virtual tables
CREATE_FTS_SQL = [
    """CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
       USING fts5(content, content_rowid='rowid');""",
    """CREATE VIRTUAL TABLE IF NOT EXISTS episodic_fts
       USING fts5(content, content_rowid='rowid');""",
]

CREATE_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_expenses_user_id    ON expenses(user_id);",
    "CREATE INDEX IF NOT EXISTS idx_expenses_category   ON expenses(category);",
    "CREATE INDEX IF NOT EXISTS idx_expenses_created_at ON expenses(created_at);",
    "CREATE INDEX IF NOT EXISTS idx_expenses_event_tag  ON expenses(event_tag);",
    "CREATE INDEX IF NOT EXISTS idx_expenses_ledger_type ON expenses(ledger_type);",
    "CREATE INDEX IF NOT EXISTS idx_budgets_user_id     ON budgets(user_id);",
    "CREATE INDEX IF NOT EXISTS idx_budget_changes_budget_user_id ON budget_changes(budget_user_id);",
    "CREATE INDEX IF NOT EXISTS idx_budget_changes_created_at ON budget_changes(created_at);",
    "CREATE INDEX IF NOT EXISTS idx_budget_groups_user_id ON budget_groups(user_id);",
    "CREATE INDEX IF NOT EXISTS idx_budget_group_categories_group_id ON budget_group_categories(group_id);",
    "CREATE INDEX IF NOT EXISTS idx_budget_group_changes_budget_user_id ON budget_group_changes(budget_user_id);",
    "CREATE INDEX IF NOT EXISTS idx_budget_group_changes_created_at ON budget_group_changes(created_at);",
    "CREATE INDEX IF NOT EXISTS idx_budget_alert_events_ym ON budget_alert_events(year, month);",
    "CREATE INDEX IF NOT EXISTS idx_api_usage_created   ON api_usage(created_at);",
    "CREATE INDEX IF NOT EXISTS idx_events_user_id      ON events(user_id);",
    "CREATE INDEX IF NOT EXISTS idx_memories_user_id    ON memories(user_id);",
    "CREATE INDEX IF NOT EXISTS idx_memories_category   ON memories(category);",
    "CREATE INDEX IF NOT EXISTS idx_memories_importance  ON memories(importance);",
    "CREATE INDEX IF NOT EXISTS idx_episodic_user_id    ON episodic_memories(user_id);",
    "CREATE INDEX IF NOT EXISTS idx_episodic_category   ON episodic_memories(category);",
    "CREATE INDEX IF NOT EXISTS idx_episodic_importance  ON episodic_memories(importance);",
    "CREATE INDEX IF NOT EXISTS idx_episodic_active      ON episodic_memories(is_active);",
    "CREATE INDEX IF NOT EXISTS idx_episodic_supersedes  ON episodic_memories(supersedes_memory_id);",
    "CREATE INDEX IF NOT EXISTS idx_core_profiles_user  ON core_profiles(user_id);",
    "CREATE INDEX IF NOT EXISTS idx_monthly_summaries_ym ON monthly_summaries(year, month);",
    "CREATE INDEX IF NOT EXISTS idx_monthly_reports_ym ON monthly_reports(year, month);",
    "CREATE INDEX IF NOT EXISTS idx_fx_rates_fetched_at ON fx_rates(fetched_at);",
    "CREATE INDEX IF NOT EXISTS idx_recurring_rules_user_id ON recurring_rules(user_id);",
    "CREATE INDEX IF NOT EXISTS idx_recurring_rules_due_day ON recurring_rules(due_day);",
    "CREATE INDEX IF NOT EXISTS idx_settlement_records_created_at ON settlement_records(created_at);",
    "CREATE INDEX IF NOT EXISTS idx_settlement_records_event_tag ON settlement_records(event_tag);",
    "CREATE INDEX IF NOT EXISTS idx_spending_goals_user_id ON spending_goals(user_id);",
]

# Migrations (idempotent, errors silenced)
MIGRATIONS = [
    "ALTER TABLE expenses ADD COLUMN currency TEXT NOT NULL DEFAULT 'SGD';",
    "ALTER TABLE expenses ADD COLUMN amount_sgd REAL NOT NULL DEFAULT 0;",
    "ALTER TABLE expenses ADD COLUMN event_tag TEXT NOT NULL DEFAULT '';",
    "ALTER TABLE expenses ADD COLUMN ledger_type TEXT NOT NULL DEFAULT 'regular';",
    "ALTER TABLE events ADD COLUMN status TEXT NOT NULL DEFAULT 'active';",
    "ALTER TABLE episodic_memories ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1;",
    "ALTER TABLE episodic_memories ADD COLUMN archived_at TIMESTAMP;",
    "ALTER TABLE episodic_memories ADD COLUMN supersedes_memory_id INTEGER;",
]

# Category renames: old_name → new_name (applied to expenses + budgets on startup)
_CATEGORY_RENAMES = {
    "水电": "水电网",
    "生活": "超市",   # old "生活" was mostly supermarket/daily items
}


def init_db() -> None:
    """Create the database file, tables, indexes, and FTS virtual tables."""
    db_dir = os.path.dirname(DATABASE_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    with get_connection() as conn:
        for table_sql in CREATE_TABLES_SQL:
            conn.execute(table_sql)
        for fts_sql in CREATE_FTS_SQL:
            try:
                conn.execute(fts_sql)
            except sqlite3.OperationalError:
                pass
        for idx_sql in CREATE_INDEX_SQL:
            conn.execute(idx_sql)
        for migration in MIGRATIONS:
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass
        # Migrate legacy memories → episodic_memories
        _migrate_legacy_memories(conn)
        # Rename old categories → new names
        _migrate_category_renames(conn)
        conn.commit()
    logger.info("Database initialized at %s", DATABASE_PATH)


def _migrate_category_renames(conn: sqlite3.Connection) -> None:
    """Rename old expense/budget categories to new names (idempotent)."""
    for old_name, new_name in _CATEGORY_RENAMES.items():
        try:
            cur = conn.execute(
                "UPDATE expenses SET category = ? WHERE category = ?",
                (new_name, old_name),
            )
            if cur.rowcount > 0:
                logger.info("Migrated %d expenses: '%s' → '%s'", cur.rowcount, old_name, new_name)

            cur = conn.execute(
                "UPDATE budgets SET category = ? WHERE category = ?",
                (new_name, old_name),
            )
            if cur.rowcount > 0:
                logger.info("Migrated %d budgets: '%s' → '%s'", cur.rowcount, old_name, new_name)
        except Exception:
            logger.exception("Failed to rename category '%s' → '%s'", old_name, new_name)


def _migrate_legacy_memories(conn: sqlite3.Connection) -> None:
    """One-time migration: copy old memories into episodic_memories if needed."""
    try:
        count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        ep_count = conn.execute("SELECT COUNT(*) FROM episodic_memories").fetchone()[0]
        if count > 0 and ep_count == 0:
            conn.execute(
                "INSERT INTO episodic_memories (user_id, content, category, importance, created_at) "
                "SELECT user_id, content, category, importance, created_at FROM memories"
            )
            # Also populate episodic FTS
            rows = conn.execute("SELECT id, content FROM episodic_memories").fetchall()
            for r in rows:
                try:
                    conn.execute("INSERT INTO episodic_fts (rowid, content) VALUES (?, ?)", (r["id"], r["content"]))
                except Exception:
                    pass
            logger.info("Migrated %d legacy memories to episodic_memories", count)
    except Exception:
        pass


@contextmanager
def get_connection() -> Generator[sqlite3.Connection, None, None]:
    """Yield a SQLite connection with row_factory set to sqlite3.Row.

    Uses WAL journal mode for safe concurrent reads/writes (avoids
    'database is locked' when two family members record expenses
    at the same time via Telegram's multi-threaded handler).
    """
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    try:
        yield conn
    finally:
        conn.close()
