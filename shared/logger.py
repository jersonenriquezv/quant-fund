"""
Centralized logging configuration using loguru.

Usage:
    from shared.logger import setup_logger
    logger = setup_logger("data_service")
    logger.info("OKX WebSocket connected")

Format (from CLAUDE.md):
    2026-03-03 14:30:00.123 | INFO     | data_service:connect:42 | OKX WebSocket connected

Output:
    - stdout: all levels (for Docker logs)
    - File: logs/{service}_{date}.log — daily rotation, 30-day retention
"""

import sys
from pathlib import Path

from loguru import logger

# Project root for log directory
_LOG_DIR = Path(__file__).parent.parent / "logs"

# Format from CLAUDE.md Logging section
_LOG_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | "
    "{name}:{function}:{line} | {message}"
)


def setup_logger(service_name: str) -> "logger":
    """Configure loguru for a specific service.

    Call once at service init. Returns the configured logger.
    Multiple calls with different service names add additional file sinks.

    Args:
        service_name: e.g. "data_service", "strategy_service"
    """
    _LOG_DIR.mkdir(exist_ok=True)

    # Remove default handler (has emoji/colors that pollute Docker logs)
    logger.remove()

    # stdout — all levels, no colors for clean Docker output
    logger.add(
        sys.stdout,
        format=_LOG_FORMAT,
        level="DEBUG",
        colorize=False,
    )

    # Daily rotated file per service
    logger.add(
        str(_LOG_DIR / f"{service_name}_{{time:YYYY-MM-DD}}.log"),
        format=_LOG_FORMAT,
        level="DEBUG",
        rotation="00:00",       # New file at midnight
        retention="30 days",
        compression="gz",       # Compress old logs
        enqueue=True,           # Thread-safe for async context
    )

    return logger
