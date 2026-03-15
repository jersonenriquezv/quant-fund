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


def _is_testing() -> bool:
    """Detect if running under pytest."""
    return "pytest" in sys.modules or "_pytest" in sys.modules


def setup_logger(service_name: str, file_level: str = "DEBUG") -> "logger":
    """Configure loguru for a specific service.

    Call once at service init. Returns the configured logger.
    Multiple calls with different service names add additional file sinks.
    File sinks are skipped when running under pytest to prevent test output
    (Mock objects, fixture data) from polluting production log files.

    Args:
        service_name: e.g. "data_service", "strategy_service"
        file_level: minimum level for file sink (default DEBUG, use INFO for scripts)
    """
    # Remove default handler (has emoji/colors that pollute Docker logs)
    logger.remove()

    if _is_testing():
        # Tests: stderr only, WARNING+ to keep test output clean
        logger.add(
            sys.stderr,
            format=_LOG_FORMAT,
            level="WARNING",
            colorize=False,
        )
        return logger

    _LOG_DIR.mkdir(exist_ok=True)

    # stdout — all levels, no colors for clean Docker output
    logger.add(
        sys.stdout,
        format=_LOG_FORMAT,
        level="DEBUG",
        colorize=False,
    )

    # Daily rotated file per service
    try:
        logger.add(
            str(_LOG_DIR / f"{service_name}_{{time:YYYY-MM-DD}}.log"),
            format=_LOG_FORMAT,
            level=file_level,
            rotation="00:00",       # New file at midnight
            retention="30 days",
            compression="gz",       # Compress old logs
            enqueue=True,           # Thread-safe for async context
        )
    except PermissionError:
        # Log files owned by another process (e.g. bot running as root).
        # Continue with stdout-only logging for scripts/backtests.
        pass

    return logger
