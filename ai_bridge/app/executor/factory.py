"""Executor factory — picks the best available executor based on env config.

Priority order:
  1. cTrader  (CTRADER_ENABLED=true) — headless Linux native, recommended
  2. MT5      (MT5_ENABLED=true)     — Windows/Wine only
  3. Noop     (default)              — logs only, no broker

Called once at startup by webhook.py; result is kept as a process-global.
"""
from __future__ import annotations

from app.config.settings import Settings, get_settings
from app.executor.base import Executor, NoopExecutor
from app.utils.logging import logger


def build_executor(settings: Settings | None = None) -> Executor:
    s = settings or get_settings()

    # ── Priority 1: cTrader MCP ────────────────────────────────────────
    if s.ctrader_enabled:
        try:
            from app.executor.ctrader import CTraderExecutor
        except Exception as exc:  # noqa: BLE001
            logger.warning("Executor: failed to import CTraderExecutor: {}", exc)
            return NoopExecutor()

        ex = CTraderExecutor(settings=s)
        # cTrader uses lazy connect (on first execute()) so we don't
        # block startup. Just verify token is present.
        if s.ctrader_is_configured:
            logger.info(
                "Executor: ctrader MCP (lazy connect on first signal) — symbol={}",
                s.ctrader_symbol,
            )
            return ex
        else:
            logger.warning(
                "Executor: CTRADER_ENABLED=true but CTRADER_TOKEN is empty. "
                "Generate token from cTrader platform settings. "
                "Falling back to noop."
            )
            return NoopExecutor()

    # ── Priority 2: MT5 (legacy, Windows only) ───────────────────────
    if s.mt5_enabled:
        try:
            from app.executor.mt5 import MT5Executor
        except Exception as exc:  # noqa: BLE001
            logger.warning("Executor: falling back to noop — failed to import MT5: {}", exc)
            return NoopExecutor()

        ex = MT5Executor(settings=s)
        if ex._lazy_init():  # noqa: SLF001 — intentional: pre-warm
            logger.info("Executor: mt5 initialised")
            return ex
        else:
            logger.warning(
                "Executor: mt5 configured but not ready ({}). Falling back to noop.",
                ex._init_error,
            )
            return NoopExecutor()

    # ── Priority 3: Noop (signal-only mode) ──────────────────────────
    logger.info("Executor: noop (no broker configured)")
    return NoopExecutor()


__all__ = ["build_executor"]
