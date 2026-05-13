"""Executor factory — picks MT5Executor if enabled & importable, else Noop.

Called once at startup by webhook.py; result is kept as a process-global.
"""
from __future__ import annotations

from app.config.settings import Settings, get_settings
from app.executor.base import Executor, NoopExecutor
from app.utils.logging import logger


def build_executor(settings: Settings | None = None) -> Executor:
    s = settings or get_settings()

    if not s.mt5_enabled:
        logger.info("Executor: noop (MT5_ENABLED=false)")
        return NoopExecutor()

    try:
        from app.executor.mt5 import MT5Executor
    except Exception as exc:  # noqa: BLE001 — defensive, covers any import-time error
        logger.warning("Executor: falling back to noop — failed to import MT5: {}", exc)
        return NoopExecutor()

    ex = MT5Executor(settings=s)
    # Try to initialise eagerly so we log the outcome at startup. If it
    # fails we still return the MT5Executor — its execute() collapses to
    # a structured error per call, so the bridge keeps running.
    if ex._lazy_init():  # noqa: SLF001 — intentional: pre-warm
        logger.info("Executor: mt5 initialised")
    else:
        logger.warning(
            "Executor: mt5 configured but not ready ({}). Falling back to noop.",
            ex._init_error,
        )
        return NoopExecutor()
    return ex


__all__ = ["build_executor"]
