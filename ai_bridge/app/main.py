"""SmartGold AI Bridge — FastAPI entry point.

Run with:
    uvicorn app.main:app --host 0.0.0.0 --port 8080

or via Docker (see docker/docker-compose.yml).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.dashboard import router as dashboard_router
from app.api.webhook import _signal_log, router as webhook_router
from app.config.settings import get_settings
from app.utils.logging import configure_logging, logger


_UI_DIR = Path(__file__).resolve().parent / "ui"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info(
        "SmartGold AI Bridge starting (env={}, model={}, mock={})",
        settings.app_env,
        settings.minimax_model,
        settings.llm_mock_mode,
    )
    # Pre-create SQLite schema so first request is fast.
    try:
        await _signal_log.init_schema()
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to init SignalLog schema: {}", exc)
    yield
    logger.info("SmartGold AI Bridge shutting down")


app = FastAPI(
    title="SmartGold AI Bridge",
    description=(
        "LLM-augmented decision layer for the SmartGold Analyzer Pro "
        "TradingView indicator."
    ),
    version="0.2.0",
    lifespan=lifespan,
)

# ── Routers ──────────────────────────────────────────────────────────
# Webhook + health (public-facing through Caddy)
app.include_router(webhook_router)
# Read-only dashboard JSON API
app.include_router(dashboard_router)

# ── Static UI (vanilla HTML + JS) ────────────────────────────────────
# Served at /ui/. Root redirects there for convenience.
if _UI_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=_UI_DIR, html=True), name="ui")
else:  # pragma: no cover — only hit in weird dev setups
    logger.warning("UI directory not found at {} — dashboard disabled", _UI_DIR)


@app.get("/", include_in_schema=False)
async def root():
    """Redirect the bare domain to the dashboard (if UI is mounted)."""
    if _UI_DIR.is_dir():
        return RedirectResponse(url="/ui/", status_code=307)
    return {
        "service": "smartgold-ai-bridge",
        "docs": "/docs",
        "health": "/health",
        "webhook": "/webhook/tradingview",
    }
