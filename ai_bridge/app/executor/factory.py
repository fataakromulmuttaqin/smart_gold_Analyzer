"""Executor factory — picks the best available executor based on env config.

Priority order:
  1. cTrader  (CTRADER_ENABLED=true) — headless Linux native, recommended
  2. MT5      (MT5_ENABLED=true)     — Windows/Wine only
  3. Noop     (default)              — logs only, no broker

Called once at startup by webhook.py; result is kept as a process-global.
If the executor is Noop but cTrader gets configured later (e.g. token
added via .env hot-reload), build_executor() is re-evaluated on next call.
"""
from __future__ import annotations

from app.config.settings import Settings, get_settings
from app.executor.base import Executor, NoopExecutor
from app.utils.logging import logger

# Module-level cache. Set to None to force re-evaluation.
_cached_executor: Executor | None = None
_cached_is_noop: bool = False


def build_executor(settings: Settings | None = None) -> Executor:
    """Build or return cached executor.

    If the previously-built executor was Noop (no broker configured), we
    re-evaluate every time in case the user has since configured cTrader.
    Once a real executor is built, it's cached permanently.
    """
    global _cached_executor, _cached_is_noop

    # If we have a real (non-noop) executor cached, reuse it
    if _cached_executor is not None and not _cached_is_noop:
        return _cached_executor

    s = settings or get_settings()

    # ── Priority 1: cTrader MCP ────────────────────────────────────────
    if s.ctrader_enabled:
        try:
            from app.executor.ctrader import CTraderExecutor
        except Exception as exc:  # noqa: BLE001
            logger.warning("Executor: failed to import CTraderExecutor: {}", exc)
            _cached_executor = NoopExecutor()
            _cached_is_noop = True
            return _cached_executor

        if s.ctrader_is_configured:
            ex = CTraderExecutor(settings=s)
            logger.info(
                "Executor: ctrader MCP (lazy connect on first signal) — symbol={}",
                s.ctrader_symbol,
            )
            _cached_executor = ex
            _cached_is_noop = False
            return ex
        else:
            logger.warning(
                "Executor: CTRADER_ENABLED=true but CTRADER_TOKEN is empty. "
                "Generate token from cTrader platform settings. "
                "Falling back to noop (will re-check next signal)."
            )
            _cached_executor = NoopExecutor()
            _cached_is_noop = True
            return _cached_executor

    # ── Priority 2: MT5 (legacy, Windows only) ───────────────────────
    if s.mt5_enabled:
        try:
            from app.executor.mt5 import MT5Executor
        except Exception as exc:  # noqa: BLE001
            logger.warning("Executor: falling back to noop — failed to import MT5: {}", exc)
            _cached_executor = NoopExecutor()
            _cached_is_noop = True
            return _cached_executor

        ex = MT5Executor(settings=s)
        if ex._lazy_init():  # noqa: SLF001 — intentional: pre-warm
            logger.info("Executor: mt5 initialised")
            _cached_executor = ex
            _cached_is_noop = False
            return ex
        else:
            logger.warning(
                "Executor: mt5 configured but not ready ({}). Falling back to noop.",
                ex._init_error,
            )
            _cached_executor = NoopExecutor()
            _cached_is_noop = True
            return _cached_executor

    # ── Priority 3: Noop (signal-only mode) ──────────────────────────
    logger.info("Executor: noop (no broker configured)")
    _cached_executor = NoopExecutor()
    _cached_is_noop = True
    return _cached_executor


__all__ = ["build_executor"]
