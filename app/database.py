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
    "CREATE INDEX IF NOT EXISTS idx_budgets_user_id     ON budgets(user_id);",
    "CREATE INDEX IF NOT EXISTS idx_api_usage_created   ON api_usage(created_at);",
    "CREATE INDEX IF NOT EXISTS idx_events_user_id      ON events(user_id);",
    "CREATE INDEX IF NOT EXISTS idx_memories_user_id    ON memories(user_id);",
    "CREATE INDEX IF NOT EXISTS idx_memories_category   ON memories(category);",
    "CREATE INDEX IF NOT EXISTS idx_memories_importance  ON memories(importance);",
    "CREATE INDEX IF NOT EXISTS idx_episodic_user_id    ON episodic_memories(user_id);",
    "CREATE INDEX IF NOT EXISTS idx_episodic_category   ON episodic_memories(category);",
    "CREATE INDEX IF NOT EXISTS idx_episodic_importance  ON episodic_memories(importance);",
    "CREATE INDEX IF NOT EXISTS idx_core_profiles_user  ON core_profiles(user_id);",
    "CREATE INDEX IF NOT EXISTS idx_monthly_summaries_ym ON monthly_summaries(year, month);",
]

# Migrations (idempotent, errors silenced)
MIGRATIONS = [
    "ALTER TABLE expenses ADD COLUMN currency TEXT NOT NULL DEFAULT 'SGD';",
    "ALTER TABLE expenses ADD COLUMN amount_sgd REAL NOT NULL DEFAULT 0;",
    "ALTER TABLE expenses ADD COLUMN event_tag TEXT NOT NULL DEFAULT '';",
]


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
        conn.commit()
    logger.info("Database initialized at %s", DATABASE_PATH)


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
