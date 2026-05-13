"""Concrete safety guard implementations.

Each guard is stateless or uses lightweight in-memory / SQLite state.
All guards are async-compatible and never raise — they return
``GuardResult(allowed=True)`` on any internal error (fail-open by design
so that a guard bug doesn't block all trading unexpectedly).
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.guards.chain import GuardResult
from app.models.schemas import LLMDecision, TradingViewAlert
from app.utils.logging import logger


# ══════════════════════════════════════════════════════════════════════════
# 1. Weekend Guard
# ══════════════════════════════════════════════════════════════════════════
class WeekendGuard:
    """Block trading on Saturday and Sunday (UTC).

    Gold markets are technically closed on weekends, but some brokers
    allow limit orders or early-open. This guard prevents accidental
    signals from firing when liquidity is near-zero.

    Also blocks Friday after ``friday_cutoff_hour`` UTC (default 21:00)
    to avoid holding over the weekend gap.
    """

    name = "weekend_guard"

    def __init__(self, friday_cutoff_hour: int = 21) -> None:
        self._friday_cutoff = friday_cutoff_hour

    async def check(
        self, alert: TradingViewAlert, decision: LLMDecision
    ) -> GuardResult:
        now = datetime.now(timezone.utc)
        weekday = now.weekday()  # Mon=0 … Sun=6

        # Saturday or Sunday
        if weekday >= 5:
            return GuardResult(
                allowed=False,
                guard_name=self.name,
                reason=f"Weekend (day={weekday}) — market closed, no trading",
            )

        # Friday after cutoff
        if weekday == 4 and now.hour >= self._friday_cutoff:
            return GuardResult(
                allowed=False,
                guard_name=self.name,
                reason=f"Friday past {self._friday_cutoff}:00 UTC — avoiding weekend gap risk",
            )

        return GuardResult(allowed=True)


# ══════════════════════════════════════════════════════════════════════════
# 2. Consecutive Loss Circuit Breaker
# ══════════════════════════════════════════════════════════════════════════
class ConsecutiveLossGuard:
    """Halt trading after N consecutive losing trades.

    Reads the last N outcomes from the signal log SQLite DB. If all are
    'loss', blocks new entries until a non-loss is recorded (manual reset
    or next win from a signal that slipped through before the breaker
    tripped).

    Only checks ENTRY signals — exit signals always pass.
    """

    name = "consecutive_loss_guard"

    def __init__(
        self,
        max_consecutive: int = 3,
        db_path: str = "./data/signals.db",
        cooldown_minutes: int = 60,
    ) -> None:
        self._max = max_consecutive
        self._db_path = Path(db_path).resolve()
        self._cooldown_minutes = cooldown_minutes
        self._tripped_at: datetime | None = None

    async def check(
        self, alert: TradingViewAlert, decision: LLMDecision
    ) -> GuardResult:
        # Exit signals always pass
        if alert.signal.startswith("exit_"):
            return GuardResult(allowed=True)

        # If already tripped, check cooldown
        if self._tripped_at is not None:
            elapsed = datetime.now(timezone.utc) - self._tripped_at
            if elapsed < timedelta(minutes=self._cooldown_minutes):
                remaining = self._cooldown_minutes - int(elapsed.total_seconds() / 60)
                return GuardResult(
                    allowed=False,
                    guard_name=self.name,
                    reason=(
                        f"Circuit breaker active — {self._max} consecutive losses. "
                        f"Auto-reset in {remaining} min."
                    ),
                )
            else:
                # Cooldown expired — reset
                self._tripped_at = None

        try:
            outcomes = await asyncio.to_thread(self._get_recent_outcomes)
        except Exception as exc:
            logger.warning("ConsecutiveLossGuard DB error: {} — allowing", exc)
            return GuardResult(allowed=True)

        if len(outcomes) >= self._max and all(o == "loss" for o in outcomes):
            self._tripped_at = datetime.now(timezone.utc)
            logger.warning(
                "Circuit breaker TRIPPED: {} consecutive losses — blocking for {} min",
                self._max, self._cooldown_minutes,
            )
            return GuardResult(
                allowed=False,
                guard_name=self.name,
                reason=(
                    f"Circuit breaker: {self._max} consecutive losses detected. "
                    f"Halting new entries for {self._cooldown_minutes} minutes."
                ),
            )

        return GuardResult(allowed=True)

    def _get_recent_outcomes(self) -> list[str]:
        """Read the last N trade outcomes from SQLite."""
        if not self._db_path.exists():
            return []
        conn = sqlite3.connect(self._db_path)
        try:
            rows = conn.execute(
                """
                SELECT outcome FROM signals
                WHERE outcome IS NOT NULL
                  AND decision_action IN ('execute', 'reduce')
                ORDER BY id DESC
                LIMIT ?
                """,
                (self._max,),
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()

    def reset(self) -> None:
        """Manual reset (callable from admin endpoint or CLI)."""
        self._tripped_at = None


# ══════════════════════════════════════════════════════════════════════════
# 3. Max Daily Trades Guard
# ══════════════════════════════════════════════════════════════════════════
class MaxDailyTradesGuard:
    """Limit the number of executed trades per UTC calendar day.

    Prevents runaway signal spam from burning through account margin.
    Counts only entries (not exits) with action in {execute, reduce}.
    """

    name = "max_daily_trades_guard"

    def __init__(
        self,
        max_trades: int = 5,
        db_path: str = "./data/signals.db",
    ) -> None:
        self._max = max_trades
        self._db_path = Path(db_path).resolve()

    async def check(
        self, alert: TradingViewAlert, decision: LLMDecision
    ) -> GuardResult:
        # Exit signals always pass
        if alert.signal.startswith("exit_"):
            return GuardResult(allowed=True)

        # Only block if decision would actually execute
        if decision.action not in ("execute", "reduce"):
            return GuardResult(allowed=True)

        try:
            count = await asyncio.to_thread(self._count_today)
        except Exception as exc:
            logger.warning("MaxDailyTradesGuard DB error: {} — allowing", exc)
            return GuardResult(allowed=True)

        if count >= self._max:
            return GuardResult(
                allowed=False,
                guard_name=self.name,
                reason=(
                    f"Daily trade limit reached ({count}/{self._max}). "
                    "No more entries until next UTC day."
                ),
            )

        return GuardResult(allowed=True)

    def _count_today(self) -> int:
        if not self._db_path.exists():
            return 0
        conn = sqlite3.connect(self._db_path)
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM signals
                WHERE date(received_at) = ?
                  AND decision_action IN ('execute', 'reduce')
                  AND signal NOT LIKE 'exit_%'
                """,
                (today,),
            ).fetchone()
            return row[0] if row else 0
        finally:
            conn.close()


# ══════════════════════════════════════════════════════════════════════════
# 4. Drawdown Guard
# ══════════════════════════════════════════════════════════════════════════
class DrawdownGuard:
    """Halt trading if daily realised PnL exceeds a negative threshold.

    Reads the sum of ``pnl`` column for today from the signal log. If
    the total is below ``-max_daily_loss_usd``, blocks new entries.

    Note: this requires outcomes to be filled in the DB (via
    ``SignalLog.update_outcome``). If no outcomes exist yet, the guard
    passes by default (fail-open).
    """

    name = "drawdown_guard"

    def __init__(
        self,
        max_daily_loss_usd: float = 500.0,
        db_path: str = "./data/signals.db",
    ) -> None:
        self._max_loss = max_daily_loss_usd
        self._db_path = Path(db_path).resolve()

    async def check(
        self, alert: TradingViewAlert, decision: LLMDecision
    ) -> GuardResult:
        if alert.signal.startswith("exit_"):
            return GuardResult(allowed=True)

        if decision.action not in ("execute", "reduce"):
            return GuardResult(allowed=True)

        try:
            daily_pnl = await asyncio.to_thread(self._get_daily_pnl)
        except Exception as exc:
            logger.warning("DrawdownGuard DB error: {} — allowing", exc)
            return GuardResult(allowed=True)

        if daily_pnl is not None and daily_pnl < -self._max_loss:
            return GuardResult(
                allowed=False,
                guard_name=self.name,
                reason=(
                    f"Daily drawdown limit hit (PnL today: ${daily_pnl:.2f}, "
                    f"limit: -${self._max_loss:.2f}). Halting until next UTC day."
                ),
            )

        return GuardResult(allowed=True)

    def _get_daily_pnl(self) -> float | None:
        if not self._db_path.exists():
            return None
        conn = sqlite3.connect(self._db_path)
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            row = conn.execute(
                """
                SELECT SUM(pnl) AS total FROM signals
                WHERE date(received_at) = ?
                  AND pnl IS NOT NULL
                """,
                (today,),
            ).fetchone()
            return row[0] if row and row[0] is not None else None
        finally:
            conn.close()


# ══════════════════════════════════════════════════════════════════════════
# 5. News Blackout Guard
# ══════════════════════════════════════════════════════════════════════════
class NewsBlackoutGuard:
    """Block trading during scheduled high-impact news windows.

    Maintains a simple list of (weekday, hour_utc) pairs representing
    known recurring events (NFP, FOMC, CPI). A buffer of ±30 minutes is
    applied around each event.

    For a production setup, integrate with an economic calendar API and
    populate ``blackout_windows`` dynamically.
    """

    name = "news_blackout_guard"

    # Default recurring events (month-agnostic, just day+hour):
    # NFP = first Friday of month at 12:30 UTC → we block Friday 12:00-13:30
    # FOMC = Wednesday 18:00 UTC (Fed announcement)
    # CPI = ~Tuesday/Wednesday 12:30 UTC
    # We use a simplified static schedule; override via constructor.
    _DEFAULT_WINDOWS: list[tuple[int, int, int]] = [
        # (weekday, hour_start, hour_end) — all UTC
        (2, 12, 14),   # Wed CPI window
        (2, 17, 19),   # Wed FOMC window
        (4, 12, 14),   # Fri NFP window
    ]

    def __init__(
        self,
        blackout_windows: list[tuple[int, int, int]] | None = None,
    ) -> None:
        self._windows = blackout_windows or self._DEFAULT_WINDOWS

    async def check(
        self, alert: TradingViewAlert, decision: LLMDecision
    ) -> GuardResult:
        if alert.signal.startswith("exit_"):
            return GuardResult(allowed=True)

        now = datetime.now(timezone.utc)
        weekday = now.weekday()
        hour = now.hour

        for wd, h_start, h_end in self._windows:
            if weekday == wd and h_start <= hour < h_end:
                return GuardResult(
                    allowed=False,
                    guard_name=self.name,
                    reason=(
                        f"News blackout window active (day={weekday}, "
                        f"hour={hour}, window={h_start}:00-{h_end}:00 UTC). "
                        "Avoiding high-impact event volatility."
                    ),
                )

        return GuardResult(allowed=True)


__all__ = [
    "WeekendGuard",
    "ConsecutiveLossGuard",
    "MaxDailyTradesGuard",
    "DrawdownGuard",
    "NewsBlackoutGuard",
]
