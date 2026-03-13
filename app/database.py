"""SQLite database initialization and connection management."""

import logging
import os
import sqlite3
from contextlib import contextmanager
from typing import Generator

from app.config import DATABASE_PATH

logger = logging.getLogger(__name__)

CREATE_TABLES_SQL = [
    # Expenses table
    """
    CREATE TABLE IF NOT EXISTS expenses (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        user_name   TEXT    NOT NULL DEFAULT '',
        category    TEXT    NOT NULL,
        amount      REAL    NOT NULL,
        note        TEXT    NOT NULL DEFAULT '',
        created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # Budgets table
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
    # API usage tracking table
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
]

CREATE_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_expenses_user_id    ON expenses(user_id);",
    "CREATE INDEX IF NOT EXISTS idx_expenses_category   ON expenses(category);",
    "CREATE INDEX IF NOT EXISTS idx_expenses_created_at ON expenses(created_at);",
    "CREATE INDEX IF NOT EXISTS idx_budgets_user_id     ON budgets(user_id);",
    "CREATE INDEX IF NOT EXISTS idx_api_usage_created   ON api_usage(created_at);",
]


def init_db() -> None:
    """Create the database file, tables, and indexes if they don't exist."""
    db_dir = os.path.dirname(DATABASE_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    with get_connection() as conn:
        for table_sql in CREATE_TABLES_SQL:
            conn.execute(table_sql)
        for idx_sql in CREATE_INDEX_SQL:
            conn.execute(idx_sql)
        conn.commit()
    logger.info("Database initialized at %s", DATABASE_PATH)


@contextmanager
def get_connection() -> Generator[sqlite3.Connection, None, None]:
    """Yield a SQLite connection with row_factory set to sqlite3.Row."""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
