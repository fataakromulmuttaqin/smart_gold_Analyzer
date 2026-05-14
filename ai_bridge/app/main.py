"""SmartGold AI Bridge — FastAPI entry point.

Run with:
    uvicorn app.main:app --host 0.0.0.0 --port 8080

or via Docker (see docker/docker-compose.yml).
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.dashboard import router as dashboard_router
from app.api.webhook import _signal_log
from app.api.webhook import router as webhook_router
from app.config.settings import get_settings
from app.executor.factory import build_executor
from app.monitor.heartbeat import get_monitor
from app.utils.logging import configure_logging, logger

_UI_DIR = Path(__file__).resolve().parent / "ui"

# Breakeven reconciler: every N seconds, ask the executor to scan open
# positions and shift SL to breakeven if any have hit the trigger.
# Noop when executor is NoopExecutor or SL_BREAKEVEN_ENABLED=false.
_BREAKEVEN_INTERVAL_SECONDS = 10


async def _breakeven_loop():
    """Background task: periodically reconcile open positions for breakeven shift.

    Includes exponential backoff: if the reconciler fails repeatedly (e.g.
    session expired, network issue), we increase the interval to avoid
    hammering the server. Resets to normal interval on success.
    """
    settings = get_settings()
    if not settings.sl_breakeven_enabled:
        logger.info("Breakeven loop disabled (SL_BREAKEVEN_ENABLED=false)")
        return

    executor = build_executor(settings)
    reconciler = getattr(executor, "reconcile_breakeven", None)
    if reconciler is None:
        logger.info(
            "Breakeven loop inactive: executor '{}' doesn't implement "
            "reconcile_breakeven (safe — usually NoopExecutor on Linux)",
            getattr(executor, "name", "?"),
        )
        return

    logger.info(
        "Breakeven loop active: trigger={}R buffer={}×ATR every {}s",
        settings.sl_breakeven_trigger_r,
        settings.sl_breakeven_buffer_atr_mult,
        _BREAKEVEN_INTERVAL_SECONDS,
    )

    consecutive_failures = 0
    max_backoff = 300  # cap at 5 minutes between retries

    try:
        while True:
            try:
                shifted = await reconciler()
                if shifted:
                    logger.info("Breakeven: shifted {} positions this cycle", shifted)
                # Success — reset backoff
                consecutive_failures = 0
                await asyncio.sleep(_BREAKEVEN_INTERVAL_SECONDS)
            except Exception as exc:  # noqa: BLE001
                consecutive_failures += 1
                # Exponential backoff: 10s, 20s, 40s, 80s, 160s, 300s (capped)
                backoff = min(
                    _BREAKEVEN_INTERVAL_SECONDS * (2 ** consecutive_failures),
                    max_backoff,
                )
                logger.warning(
                    "Breakeven reconciler raised (failure #{}, next retry in {}s): {}",
                    consecutive_failures, backoff, exc,
                )
                await asyncio.sleep(backoff)
    except asyncio.CancelledError:
        logger.info("Breakeven loop cancelled cleanly")
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info(
        "SmartGold AI Bridge starting (env={}, model={}, mock={}, sl_policy={})",
        settings.app_env,
        settings.minimax_model,
        settings.llm_mock_mode,
        settings.sl_policy,
    )
    # Pre-create SQLite schema so first request is fast.
    try:
        await _signal_log.init_schema()
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to init SignalLog schema: {}", exc)

    # Start breakeven reconciler as background task
    be_task = asyncio.create_task(_breakeven_loop(), name="breakeven_loop")

    # ── Monitor: startup notification + heartbeat loop ───────────────
    monitor = get_monitor(settings)
    await monitor.notify_startup()
    monitor.start_heartbeat(silence_hours=settings.monitor_silence_hours)

    try:
        yield
    finally:
        logger.info("SmartGold AI Bridge shutting down")
        # Send shutdown notification before stopping
        await monitor.notify_shutdown()
        await monitor.stop_heartbeat()
        be_task.cancel()
        try:
            await be_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


app = FastAPI(
    title="SmartGold AI Bridge",
    description=(
        "LLM-augmented decision layer for the SmartGold Analyzer Pro "
        "TradingView indicator."
    ),
    version="0.3.0",
    lifespan=lifespan,
)

# ── Routers ──────────────────────────────────────────────────────────
app.include_router(webhook_router)
app.include_router(dashboard_router)

# ── Static UI ───────────────────────────────────────────────────────
if _UI_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=_UI_DIR, html=True), name="ui")
else:  # pragma: no cover
    logger.warning("UI directory not found at {} — dashboard disabled", _UI_DIR)


@app.get("/", include_in_schema=False)
async def root():
    """Redirect the bare domain to the dashboard."""
    if _UI_DIR.is_dir():
        return RedirectResponse(url="/ui/", status_code=307)
    return {
        "service": "smartgold-ai-bridge",
        "docs": "/docs",
        "health": "/health",
        "webhook": "/webhook/tradingview",
    }
