"""SmartGold AI Bridge — FastAPI entry point.

Run with:
    uvicorn app.main:app --host 0.0.0.0 --port 8080

or via Docker (see docker/docker-compose.yml).
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.webhook import _signal_log, router
from app.config.settings import get_settings
from app.utils.logging import configure_logging, logger


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
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(router)


@app.get("/")
async def root() -> dict:
    return {
        "service": "smartgold-ai-bridge",
        "docs": "/docs",
        "health": "/health",
        "webhook": "/webhook/tradingview",
    }
