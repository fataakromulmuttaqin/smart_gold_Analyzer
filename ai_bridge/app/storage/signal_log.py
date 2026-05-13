"""SQLite-backed audit log for webhook signals + LLM decisions.

Small on purpose: no ORM, just stdlib sqlite3 in a thread pool. Keeps a
row per accepted webhook call so you can review what the bridge decided
and why.

Schema v2 additions (2026-05): executor columns + trade outcome columns.
Older databases are migrated in-place via idempotent ALTER TABLE statements
in ``_init_schema_blocking`` — existing rows keep their data.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

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
    notified        INTEGER NOT NULL DEFAULT 0,
    -- v2: executor snapshot
    execution_placed   INTEGER NOT NULL DEFAULT 0,
    execution_json     TEXT,
    -- v2: outcome tracking (filled later by a reconciler / manual update)
    outcome         TEXT,         -- 'win' | 'loss' | 'breakeven' | NULL
    pnl             REAL,         -- account-currency PnL, nullable
    closed_at       TEXT          -- ISO timestamp when outcome was set
);
CREATE INDEX IF NOT EXISTS idx_signals_symbol_time
    ON signals(symbol, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_signal_outcome
    ON signals(signal, outcome);
"""

# Columns that older DBs may be missing. We try to add each one; failures
# (e.g. already exists) are ignored so migration is idempotent.
_MIGRATIONS = [
    "ALTER TABLE signals ADD COLUMN execution_placed INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE signals ADD COLUMN execution_json TEXT",
    "ALTER TABLE signals ADD COLUMN outcome TEXT",
    "ALTER TABLE signals ADD COLUMN pnl REAL",
    "ALTER TABLE signals ADD COLUMN closed_at TEXT",
]


class SignalLog:
    """Thin SQLite helper. Each instance opens its own connection lazily."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._path = Path(self.settings.sqlite_path).resolve()
        self._initialised = False

    # ──────────────────────────────────────────────────────────────────
    # Schema
    # ──────────────────────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema_blocking(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            # Best-effort migration for DBs created by an older bridge version.
            for stmt in _MIGRATIONS:
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError:
                    # Column already exists — expected on fresh schema above
                    # and on already-migrated DBs.
                    pass
            conn.commit()

    async def init_schema(self) -> None:
        if self._initialised:
            return
        await asyncio.to_thread(self._init_schema_blocking)
        self._initialised = True
        logger.info("SignalLog schema ready at {}", self._path)

    # ──────────────────────────────────────────────────────────────────
    # Write
    # ──────────────────────────────────────────────────────────────────
    def _insert_blocking(
        self,
        alert: TradingViewAlert,
        context: MacroContext,
        decision: LLMDecision,
        notified: bool,
        execution: Any | None,
    ) -> int:
        exec_placed = 0
        exec_json: str | None = None
        if execution is not None:
            try:
                exec_placed = 1 if getattr(execution, "placed", False) else 0
                exec_dict = (
                    execution.to_dict()
                    if hasattr(execution, "to_dict")
                    else dict(execution)
                )
                exec_json = json.dumps(exec_dict, default=str)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not serialise execution: {}", exc)
                exec_placed = 0
                exec_json = None

        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO signals (
                    symbol, timeframe, signal, price,
                    alert_json, context_json,
                    decision_action, decision_conf, decision_json,
                    notified, execution_placed, execution_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    exec_placed,
                    exec_json,
                ),
            )
            return int(cur.lastrowid or 0)

    async def record(
        self,
        alert: TradingViewAlert,
        context: MacroContext,
        decision: LLMDecision,
        notified: bool = False,
        execution: Any | None = None,
    ) -> int:
        await self.init_schema()
        return await asyncio.to_thread(
            self._insert_blocking, alert, context, decision, notified, execution
        )

    def _update_outcome_blocking(
        self, signal_id: int, outcome: str, pnl: float | None
    ) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE signals "
                "SET outcome = ?, pnl = ?, closed_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (outcome, pnl, signal_id),
            )
            conn.commit()
            return cur.rowcount > 0

    async def update_outcome(
        self, signal_id: int, outcome: str, pnl: float | None = None
    ) -> bool:
        """Mark a signal as win/loss/breakeven after the trade closes."""
        if outcome not in {"win", "loss", "breakeven"}:
            raise ValueError(
                "outcome must be one of: 'win', 'loss', 'breakeven'"
            )
        await self.init_schema()
        return await asyncio.to_thread(
            self._update_outcome_blocking, signal_id, outcome, pnl
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
            "       decision_action, decision_conf, notified, "
            "       execution_placed, outcome, pnl "
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
        for key in ("alert_json", "context_json", "decision_json", "execution_json"):
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

            placed_row = conn.execute(
                "SELECT COUNT(*) AS c FROM signals "
                "WHERE received_at >= datetime('now', ?) AND execution_placed = 1",
                (cutoff,),
            ).fetchone()
            placed = placed_row["c"] if placed_row else 0

        return {
            "window_hours": since_hours,
            "total": total,
            "by_action": by_action,
            "top_signals": by_signal,
            "avg_confidence": round(avg_conf, 3) if avg_conf is not None else None,
            "notified": notified,
            "orders_placed": placed,
        }

    async def stats(self, since_hours: int = 24) -> dict:
        """Aggregate counters for the dashboard summary row."""
        await self.init_schema()
        return await asyncio.to_thread(self._stats_blocking, since_hours)

    # ──────────────────────────────────────────────────────────────────
    # Win-rate aggregation (only considers rows with a non-null outcome)
    # ──────────────────────────────────────────────────────────────────
    def _winrate_blocking(self, since_hours: int) -> dict:
        cutoff = f"-{max(1, int(since_hours))} hours"
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT signal,
                       SUM(CASE WHEN outcome='win'       THEN 1 ELSE 0 END) AS wins,
                       SUM(CASE WHEN outcome='loss'      THEN 1 ELSE 0 END) AS losses,
                       SUM(CASE WHEN outcome='breakeven' THEN 1 ELSE 0 END) AS breakevens,
                       COUNT(outcome) AS closed,
                       COALESCE(SUM(pnl), 0) AS total_pnl,
                       COALESCE(AVG(pnl), 0) AS avg_pnl
                FROM signals
                WHERE received_at >= datetime('now', ?)
                  AND outcome IS NOT NULL
                GROUP BY signal
                ORDER BY closed DESC
                """,
                (cutoff,),
            ).fetchall()

            overall = conn.execute(
                """
                SELECT SUM(CASE WHEN outcome='win'  THEN 1 ELSE 0 END) AS wins,
                       SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) AS losses,
                       SUM(CASE WHEN outcome='breakeven' THEN 1 ELSE 0 END) AS breakevens,
                       COUNT(outcome) AS closed,
                       COALESCE(SUM(pnl), 0) AS total_pnl
                FROM signals
                WHERE received_at >= datetime('now', ?)
                  AND outcome IS NOT NULL
                """,
                (cutoff,),
            ).fetchone()

        def _row(r: sqlite3.Row | dict) -> dict:
            d = dict(r)
            wins = int(d.get("wins") or 0)
            losses = int(d.get("losses") or 0)
            bes = int(d.get("breakevens") or 0)
            closed = int(d.get("closed") or 0)
            # Classic win-rate convention: breakevens excluded from denominator.
            decisive = wins + losses
            wr = (wins / decisive) if decisive > 0 else None
            return {
                "signal": d.get("signal"),
                "wins": wins,
                "losses": losses,
                "breakevens": bes,
                "closed": closed,
                "win_rate": round(wr, 4) if wr is not None else None,
                "total_pnl": round(float(d.get("total_pnl") or 0), 2),
                "avg_pnl": round(float(d.get("avg_pnl") or 0), 2) if "avg_pnl" in d else None,
            }

        by_signal = [_row(r) for r in rows]
        overall_summary = _row(overall) if overall else {
            "wins": 0, "losses": 0, "breakevens": 0,
            "closed": 0, "win_rate": None, "total_pnl": 0.0,
        }
        overall_summary["signal"] = "__all__"

        return {
            "window_hours": since_hours,
            "overall": overall_summary,
            "by_signal": by_signal,
        }

    async def winrate_by_signal(self, since_hours: int = 24 * 30) -> dict:
        """Win/loss breakdown per Pine-signal name within a lookback window.

        Requires outcome to have been filled in (via update_outcome). Until
        outcomes exist, returns empty lists — the dashboard renders an
        "awaiting trade outcomes" hint.
        """
        await self.init_schema()
        return await asyncio.to_thread(self._winrate_blocking, since_hours)


__all__ = ["SignalLog"]
