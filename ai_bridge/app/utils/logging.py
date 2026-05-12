"""Project-wide loguru configuration helper."""
from __future__ import annotations

import sys

from loguru import logger


def configure_logging(level: str = "INFO") -> None:
    """Configure loguru sink once at process start.

    Keeps output single-line JSON-friendly in production but colored in
    development.
    """
    logger.remove()
    logger.add(
        sys.stderr,
        level=level.upper(),
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> "
            "- <level>{message}</level>"
        ),
        backtrace=False,
        diagnose=False,
        enqueue=False,
    )


__all__ = ["logger", "configure_logging"]
