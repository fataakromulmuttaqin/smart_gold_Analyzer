"""Guard protocol + chain orchestrator."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.models.schemas import LLMDecision, TradingViewAlert


@dataclass(slots=True)
class GuardResult:
    """Outcome of a guard check."""

    allowed: bool
    guard_name: str = ""
    reason: str = ""


class Guard(Protocol):
    """Protocol that all safety guards must implement."""

    name: str

    async def check(
        self,
        alert: TradingViewAlert,
        decision: LLMDecision,
    ) -> GuardResult:
        """Return GuardResult(allowed=True) to pass, or (allowed=False, reason=...)."""
        ...


class GuardChain:
    """Runs multiple guards in sequence; first rejection wins.

    Usage:
        chain = GuardChain([WeekendGuard(), ConsecutiveLossGuard(...)])
        result = await chain.check(alert, decision)
        if not result.allowed:
            # downgrade decision to skip
    """

    def __init__(self, guards: list[Guard] | None = None) -> None:
        self._guards: list[Guard] = guards or []

    def add(self, guard: Guard) -> None:
        self._guards.append(guard)

    @property
    def guards(self) -> list[Guard]:
        return list(self._guards)

    async def check(
        self,
        alert: TradingViewAlert,
        decision: LLMDecision,
    ) -> GuardResult:
        """Run all guards in order. First rejection stops the chain."""
        for guard in self._guards:
            result = await guard.check(alert, decision)
            if not result.allowed:
                return result
        return GuardResult(allowed=True)


__all__ = ["Guard", "GuardChain", "GuardResult"]
