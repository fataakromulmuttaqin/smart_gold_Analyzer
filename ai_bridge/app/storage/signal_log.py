"""SQLite-backed audit log for webhook signals + LLM decisions.

Small on purpose: no ORM, just stdlib sqlite3 in a thread pool. Keeps a
row per accepted webhook call so you can review what the bridge decided
and why.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from app.config.settings import Settings, get_settings
from app.models.schemas import LLMDecision, MacroContext, TradingViewAlert
from app.utils.logging import logger


_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    symbol          TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    signal          TEXT NOT NULL,
    price           REAL NOT NULL,
    alert_json      TEXT NOT NULL,
    context_json    TEXT NOT NULL,
    decision_action TEXT NOT NULL,
    decision_conf   REAL NOT NULL,
    decision_json   TEXT NOT NULL,
    notified        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_signals_symbol_time
    ON signals(symbol, received_at DESC);
"""


class SignalLog:
    """Thin SQLite helper. Each instance opens its own connection lazily."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._path = Path(self.settings.sqlite_path).resolve()
        self._initialised = False

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema_blocking(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    async def init_schema(self) -> None:
        if self._initialised:
            return
        await asyncio.to_thread(self._init_schema_blocking)
        self._initialised = True
        logger.info("SignalLog schema ready at {}", self._path)

    def _insert_blocking(
        self,
        alert: TradingViewAlert,
        context: MacroContext,
        decision: LLMDecision,
        notified: bool,
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO signals (
                    symbol, timeframe, signal, price,
                    alert_json, context_json,
                    decision_action, decision_conf, decision_json,
                    notified
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert.symbol,
                    alert.timeframe,
                    alert.signal,
                    alert.price,
                    json.dumps(alert.model_dump(), default=str),
                    json.dumps(context.model_dump(), default=str),
                    decision.action,
                    decision.confidence,
                    json.dumps(decision.model_dump(), default=str),
                    1 if notified else 0,
                ),
            )
            return int(cur.lastrowid or 0)

    async def record(
        self,
        alert: TradingViewAlert,
        context: MacroContext,
        decision: LLMDecision,
        notified: bool = False,
    ) -> int:
        await self.init_schema()
        return await asyncio.to_thread(
            self._insert_blocking, alert, context, decision, notified
        )


__all__ = ["SignalLog"]
