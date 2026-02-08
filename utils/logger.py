"""
Logging Setup
=============
Sets up clean, readable logging for the entire bot.

Why this matters:
- When the bot runs overnight, you need to know exactly what it did
- Every trade, every decision, every error gets logged with context
- Uses 'structlog' for structured logs that are easy to read AND parse

Log levels (from most to least detail):
- DEBUG: Every little detail (API calls, intermediate calculations)
- INFO: Normal operations (trades executed, wallets scored, signals detected)
- WARNING: Something unexpected but not broken (rate limit hit, slow API)
- ERROR: Something broke (trade failed, API down, database error)
"""

import sys
import logging
from pathlib import Path

import structlog


def setup_logging(log_level: str = "INFO", log_dir: str | None = None) -> None:
    """
    Configure logging for the entire application.

    Args:
        log_level: How much detail to show (DEBUG, INFO, WARNING, ERROR)
        log_dir: Optional directory to also save logs to a file
    """
    # Convert string level to Python's logging constant
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    # Set up basic Python logging (structlog builds on top of this)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=numeric_level,
    )

    # If a log directory is specified, also write logs to a file
    if log_dir:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path / "bot.log")
        file_handler.setLevel(numeric_level)
        logging.getLogger().addHandler(file_handler)

    # Configure structlog for clean, colorful terminal output
    structlog.configure(
        processors=[
            # Add log level (INFO, WARNING, etc.)
            structlog.stdlib.add_log_level,
            # Add timestamp
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S"),
            # Pretty printing for terminal
            structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty()),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(module_name: str) -> structlog.stdlib.BoundLogger:
    """
    Get a logger for a specific module.

    Usage:
        from utils.logger import get_logger
        logger = get_logger(__name__)
        logger.info("trade_executed", token="PEPE", amount_sol=0.01)

    The module_name shows up in logs so you know which part of the bot
    generated each message.
    """
    return structlog.get_logger(module_name)
