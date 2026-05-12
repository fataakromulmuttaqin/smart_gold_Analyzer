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

    # ══════════════════════════════════════════════════════════════════
    # Read-side — used by the dashboard API
    # ══════════════════════════════════════════════════════════════════

    def _list_recent_blocking(
        self,
        limit: int,
        offset: int,
        action: str | None,
        symbol: str | None,
    ) -> list[dict]:
        sql = (
            "SELECT id, received_at, symbol, timeframe, signal, price, "
            "       decision_action, decision_conf, notified "
            "FROM signals"
        )
        clauses: list[str] = []
        params: list[object] = []
        if action:
            clauses.append("decision_action = ?")
            params.append(action)
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    async def list_recent(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        action: str | None = None,
        symbol: str | None = None,
    ) -> list[dict]:
        """Return newest-first list of signals for the dashboard."""
        await self.init_schema()
        # Clamp to sane bounds so a big ?limit= can't thrash the sqlite cache.
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        return await asyncio.to_thread(
            self._list_recent_blocking, limit, offset, action, symbol
        )

    def _get_by_id_blocking(self, signal_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM signals WHERE id = ?", (signal_id,)
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        # Expand JSON columns so the detail view doesn't have to parse twice.
        for key in ("alert_json", "context_json", "decision_json"):
            raw = data.get(key)
            if raw:
                try:
                    data[key.replace("_json", "")] = json.loads(raw)
                except (TypeError, ValueError):
                    data[key.replace("_json", "")] = None
        return data

    async def get_by_id(self, signal_id: int) -> dict | None:
        await self.init_schema()
        return await asyncio.to_thread(self._get_by_id_blocking, signal_id)

    def _stats_blocking(self, since_hours: int) -> dict:
        cutoff = f"-{max(1, int(since_hours))} hours"
        with self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM signals "
                "WHERE received_at >= datetime('now', ?)",
                (cutoff,),
            ).fetchone()["c"]

            by_action_rows = conn.execute(
                "SELECT decision_action, COUNT(*) AS c FROM signals "
                "WHERE received_at >= datetime('now', ?) "
                "GROUP BY decision_action",
                (cutoff,),
            ).fetchall()
            by_action = {r["decision_action"]: r["c"] for r in by_action_rows}
            for a in ("execute", "skip", "reduce"):
                by_action.setdefault(a, 0)

            by_signal_rows = conn.execute(
                "SELECT signal, COUNT(*) AS c FROM signals "
                "WHERE received_at >= datetime('now', ?) "
                "GROUP BY signal ORDER BY c DESC LIMIT 10",
                (cutoff,),
            ).fetchall()
            by_signal = [{"signal": r["signal"], "count": r["c"]} for r in by_signal_rows]

            avg_conf_row = conn.execute(
                "SELECT AVG(decision_conf) AS avg_conf FROM signals "
                "WHERE received_at >= datetime('now', ?)",
                (cutoff,),
            ).fetchone()
            avg_conf = avg_conf_row["avg_conf"] if avg_conf_row else None

            notified_row = conn.execute(
                "SELECT COUNT(*) AS c FROM signals "
                "WHERE received_at >= datetime('now', ?) AND notified = 1",
                (cutoff,),
            ).fetchone()
            notified = notified_row["c"] if notified_row else 0

        return {
            "window_hours": since_hours,
            "total": total,
            "by_action": by_action,
            "top_signals": by_signal,
            "avg_confidence": round(avg_conf, 3) if avg_conf is not None else None,
            "notified": notified,
        }

    async def stats(self, since_hours: int = 24) -> dict:
        """Aggregate counters for the dashboard summary row."""
        await self.init_schema()
        return await asyncio.to_thread(self._stats_blocking, since_hours)


__all__ = ["SignalLog"]
