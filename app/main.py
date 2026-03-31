"""Entry point for Family Finance Claw 🦞."""

import logging
import os
import shutil
import subprocess
import sys

from app.config import BOT_BACKEND, CODEX_BIN, CODEX_HOME, CODEX_MODEL, CODEX_WORKDIR, DATABASE_PATH, TELEGRAM_BOT_TOKEN
from app.database import init_db
from app.bot.handlers import build_application

_log_dir = os.path.dirname(DATABASE_PATH) or "data"
os.makedirs(_log_dir, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"{_log_dir}/claw_debug.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def main() -> None:
    # Validate required config
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set. Please check your .env file.")
        sys.exit(1)
    logger.info("Bot backend: %s", BOT_BACKEND)
    logger.info("Codex binary: %s", CODEX_BIN)
    if CODEX_MODEL:
        logger.info("Codex model override: %s", CODEX_MODEL)
    codex_path = shutil.which(CODEX_BIN)
    if not codex_path:
        logger.error("Codex binary '%s' not found in PATH.", CODEX_BIN)
        sys.exit(1)
    logger.info("Resolved Codex path: %s", codex_path)
    try:
        subprocess.run([CODEX_BIN, "--version"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except Exception:
        logger.exception("Failed to execute Codex CLI")
        sys.exit(1)
    if not os.path.isdir(CODEX_HOME):
        logger.error("CODEX_HOME does not exist: %s", CODEX_HOME)
        sys.exit(1)
    if not os.listdir(CODEX_HOME):
        logger.error("CODEX_HOME is empty: %s. Run 'codex login' first and mount the resulting directory.", CODEX_HOME)
        sys.exit(1)
    if not os.path.isdir(CODEX_WORKDIR):
        logger.error("CODEX_WORKDIR does not exist: %s", CODEX_WORKDIR)
        sys.exit(1)

    # Initialize database
    init_db()

    # Build and run bot (polling mode)
    logger.info("Starting Family Finance Claw 🦞 (Telegram -> Codex bridge)...")
    app = build_application()
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
