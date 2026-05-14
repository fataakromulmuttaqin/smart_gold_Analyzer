"""Concrete guard implementations.

Each guard is a small class with an `evaluate(context)` method.
Guards are intentionally simple, stateless, and have no side effects.

Updated 2026-05: Guards rescaled for gold at ~$4,500-$5,000 (was $2,000).
- MaxATRGuard: threshold raised from $12 to $50 + percentage-based mode
- SpreadGuard: threshold raised from 50 to 150 points
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
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
    """Block when bid-ask spread is too wide (slippage risk).

    Rescaled for gold at $4,500-$5,000 (2026):
    - Normal spread: 30-80 points
    - Abnormal: >150 points (was >50 at $2,000 gold)
    """

    name: str = "spread"
    max_spread_points: float = 150.0  # raised from 50 for $4700 gold

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


@dataclass(slots=True)
class MaxATRGuard:
    """Block trading when ATR indicates extreme volatility.

    Rescaled for gold at $4,500-$5,000 (2026):
    - Normal ATR H1: $15-35 (was $8-15 at $2,000 gold)
    - Absolute threshold: $50 (was $12)
    - Percentage mode (recommended): blocks if ATR > 0.8% of entry price
      e.g. at $4,700 → blocks if ATR > $37.6

    Supports two modes (set via env):
    - MAX_ATR_USE_PCT=true  → percentage-based (auto-scales with price)
    - MAX_ATR_USE_PCT=false → absolute threshold (legacy)
    """

    name: str = "max_atr"
    max_atr: float = 50.0  # raised from 12 for $4700 gold
    max_atr_pct: float = 0.8  # block if ATR > 0.8% of entry price
    use_pct_mode: bool = True  # default to percentage mode

    def __post_init__(self):
        # Allow env overrides for pct mode
        env_use_pct = os.getenv("MAX_ATR_USE_PCT", "").lower()
        if env_use_pct:
            self.use_pct_mode = env_use_pct == "true"
        env_pct = os.getenv("MAX_ATR_PCT_BLOCK", "")
        if env_pct:
            self.max_atr_pct = float(env_pct)

    def evaluate(self, context: dict[str, Any]) -> GuardVerdict:
        alert = context.get("alert")
        atr = getattr(alert, "atr", None) if alert else None
        if atr is None or atr <= 0:
            # No ATR to check — fail open
            return GuardVerdict(verdict=Verdict.PASS, guard_name=self.name)

        # Determine threshold
        if self.use_pct_mode:
            price = getattr(alert, "price", None) or 4700.0
            threshold = price * (self.max_atr_pct / 100.0)
        else:
            threshold = self.max_atr

        if atr > threshold:
            return GuardVerdict(
                verdict=Verdict.BLOCK,
                guard_name=self.name,
                reason=(
                    f"ATR ${atr:.2f} > threshold ${threshold:.2f} "
                    f"({atr / (getattr(alert, 'price', None) or 4700.0) * 100:.1f}% of price) "
                    f"— extreme volatility"
                ),
            )
        return GuardVerdict(verdict=Verdict.PASS, guard_name=self.name)


__all__ = [
    "MaxDailyTradesGuard",
    "DrawdownGuard",
    "SpreadGuard",
    "NewsBlackoutGuard",
    "MaxATRGuard",
]
