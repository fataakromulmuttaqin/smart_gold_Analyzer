"""Guard chain runner.

Iterates through registered guards and short-circuits on the first BLOCK.
REDUCE verdicts accumulate (e.g. reduce lot by max of all reduction factors).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from app.utils.logging import logger


class Verdict(str, Enum):
    PASS = "pass"
    BLOCK = "block"
    REDUCE = "reduce"


@dataclass(slots=True)
class GuardVerdict:
    """Result from a single guard evaluation."""

    verdict: Verdict
    guard_name: str
    reason: str = ""
    reduce_factor: float = 1.0  # 1.0 = no reduction; 0.5 = halve lot

    @property
    def blocked(self) -> bool:
        return self.verdict == Verdict.BLOCK


class Guard(Protocol):
    """Protocol that all guards must satisfy."""

    name: str

    def evaluate(self, context: dict[str, Any]) -> GuardVerdict:
        """Evaluate the guard against the given context dict.

        Context keys (all optional — guards must handle missing keys):
          - decision: DecisionResult from the LLM
          - alert: TradingViewAlert
          - macro: MacroContext
          - settings: Settings
          - daily_trades: int (number of trades placed today)
          - daily_pnl_r: float (cumulative R for the day)
          - spread_points: float (current bid-ask spread)
          - news_window: bool (True if within ±N min of high-impact news)
        """
        ...


@dataclass
class GuardChain:
    """Ordered list of guards. Runs all; returns combined verdict."""

    guards: list[Guard] = field(default_factory=list)

    def add(self, guard: Guard) -> "GuardChain":
        self.guards.append(guard)
        return self

    def run(self, context: dict[str, Any]) -> tuple[Verdict, list[GuardVerdict]]:
        """Execute all guards and return (final_verdict, individual_results).

        Short-circuits on first BLOCK. Accumulates REDUCE factors.
        """
        results: list[GuardVerdict] = []
        combined_reduce = 1.0

        for guard in self.guards:
            try:
                v = guard.evaluate(context)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Guard '{}' raised: {}", guard.name, exc)
                v = GuardVerdict(
                    verdict=Verdict.PASS,
                    guard_name=guard.name,
                    reason=f"error (fail-open): {exc}",
                )
            results.append(v)

            if v.verdict == Verdict.BLOCK:
                logger.info(
                    "Guard BLOCK: {} — {}", guard.name, v.reason
                )
                return Verdict.BLOCK, results

            if v.verdict == Verdict.REDUCE:
                combined_reduce = min(combined_reduce, v.reduce_factor)
                logger.info(
                    "Guard REDUCE: {} — factor={:.2f} reason={}",
                    guard.name,
                    v.reduce_factor,
                    v.reason,
                )

        if combined_reduce < 1.0:
            return Verdict.REDUCE, results
        return Verdict.PASS, results


def build_default_chain(settings: Any = None) -> GuardChain:
    """Build the default guard chain from settings.

    Import here to avoid circular deps — guards module imports are deferred.
    """
    from app.guards.guards import (
        DrawdownGuard,
        MaxDailyTradesGuard,
        NewsBlackoutGuard,
        SpreadGuard,
    )

    chain = GuardChain()

    if settings is None:
        from app.config.settings import get_settings
        settings = get_settings()

    chain.add(MaxDailyTradesGuard(max_trades=settings.guard_max_daily_trades))
    chain.add(DrawdownGuard(max_dd_r=settings.guard_max_daily_drawdown_r))
    chain.add(SpreadGuard(max_spread_points=settings.guard_max_spread_points))
    chain.add(NewsBlackoutGuard(enabled=settings.guard_news_blackout))

    return chain


__all__ = ["Guard", "GuardChain", "GuardVerdict", "Verdict", "build_default_chain"]
