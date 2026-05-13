"""Concrete guard implementations.

Each guard is a small class with an `evaluate(context)` method.
Guards are intentionally simple, stateless, and have no side effects.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.guards.chain import GuardVerdict, Verdict


@dataclass(slots=True)
class MaxDailyTradesGuard:
    """Block signals once N trades have been placed today."""

    name: str = "max_daily_trades"
    max_trades: int = 5

    def evaluate(self, context: dict[str, Any]) -> GuardVerdict:
        daily = context.get("daily_trades", 0)
        if daily >= self.max_trades:
            return GuardVerdict(
                verdict=Verdict.BLOCK,
                guard_name=self.name,
                reason=f"daily trade cap reached ({daily}/{self.max_trades})",
            )
        return GuardVerdict(verdict=Verdict.PASS, guard_name=self.name)


@dataclass(slots=True)
class DrawdownGuard:
    """Block or reduce when daily cumulative R exceeds a loss threshold."""

    name: str = "drawdown"
    max_dd_r: float = -3.0  # block if daily P&L below this
    reduce_threshold_r: float = -1.5  # reduce lot at this level

    def evaluate(self, context: dict[str, Any]) -> GuardVerdict:
        pnl_r = context.get("daily_pnl_r", 0.0)
        if pnl_r <= self.max_dd_r:
            return GuardVerdict(
                verdict=Verdict.BLOCK,
                guard_name=self.name,
                reason=f"daily drawdown {pnl_r:.2f}R exceeds limit {self.max_dd_r}R",
            )
        if pnl_r <= self.reduce_threshold_r:
            return GuardVerdict(
                verdict=Verdict.REDUCE,
                guard_name=self.name,
                reason=f"daily P&L {pnl_r:.2f}R below caution threshold",
                reduce_factor=0.5,
            )
        return GuardVerdict(verdict=Verdict.PASS, guard_name=self.name)


@dataclass(slots=True)
class SpreadGuard:
    """Block when bid-ask spread is too wide (slippage risk)."""

    name: str = "spread"
    max_spread_points: float = 50.0  # gold spread >50 points = abnormal

    def evaluate(self, context: dict[str, Any]) -> GuardVerdict:
        spread = context.get("spread_points")
        if spread is None:
            # No spread data available — fail open
            return GuardVerdict(verdict=Verdict.PASS, guard_name=self.name)
        if spread > self.max_spread_points:
            return GuardVerdict(
                verdict=Verdict.BLOCK,
                guard_name=self.name,
                reason=f"spread {spread:.1f} > max {self.max_spread_points:.1f}",
            )
        return GuardVerdict(verdict=Verdict.PASS, guard_name=self.name)


@dataclass(slots=True)
class NewsBlackoutGuard:
    """Reduce position size during high-impact news windows.

    The webhook pipeline sets ``context['news_window'] = True`` when the
    current time falls within ±N minutes of a scheduled high-impact event
    (NFP, CPI, FOMC). The guard doesn't fetch calendars itself — it just
    reacts to the flag.
    """

    name: str = "news_blackout"
    enabled: bool = True
    reduce_factor: float = 0.25  # quarter size during news

    def evaluate(self, context: dict[str, Any]) -> GuardVerdict:
        if not self.enabled:
            return GuardVerdict(verdict=Verdict.PASS, guard_name=self.name)
        in_window = context.get("news_window", False)
        if in_window:
            return GuardVerdict(
                verdict=Verdict.REDUCE,
                guard_name=self.name,
                reason="high-impact news window active",
                reduce_factor=self.reduce_factor,
            )
        return GuardVerdict(verdict=Verdict.PASS, guard_name=self.name)


__all__ = [
    "MaxDailyTradesGuard",
    "DrawdownGuard",
    "SpreadGuard",
    "NewsBlackoutGuard",
]
