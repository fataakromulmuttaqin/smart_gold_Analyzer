"""Executor protocol + result + noop implementation.

An Executor receives a (alert, decision) pair and optionally places an
order with a broker. Two implementations ship with the bridge:

  * :class:`NoopExecutor` — always returns ``placed=False`` with a note.
    Used when no broker is configured. Safe default; never touches real money.
  * :class:`MT5Executor` (in :mod:`app.executor.mt5`) — places an order via
    the MetaTrader5 Python SDK. Opt-in, Windows/Wine only, only active when
    ``MT5_ENABLED=true`` AND the ``MetaTrader5`` package imports successfully.

Both return the same :class:`ExecutionResult` so the webhook handler can
treat them uniformly. Any executor error must never raise — we collapse
to ``placed=False`` with ``error=...`` so the dashboard can surface it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from app.models.schemas import LLMDecision, TradingViewAlert


@dataclass(slots=True)
class ExecutionResult:
    """Outcome of a (possibly skipped) execution attempt."""

    placed: bool
    note: str = ""
    error: str | None = None
    # Populated on success so the dashboard/audit log can display them.
    order_id: int | None = None
    symbol: str | None = None
    side: str | None = None        # "buy" | "sell"
    volume: float | None = None
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "placed": self.placed,
            "note": self.note,
            "error": self.error,
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side,
            "volume": self.volume,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "extra": self.extra,
        }


class Executor(Protocol):
    """Minimal async protocol every executor must implement."""

    name: str

    async def execute(
        self,
        alert: TradingViewAlert,
        decision: LLMDecision,
    ) -> ExecutionResult:
        ...


class NoopExecutor:
    """Safe default: logs only, never places an order.

    Used when MT5 (or any other executor) is disabled or failed to init.
    """

    name = "noop"

    async def execute(
        self,
        alert: TradingViewAlert,
        decision: LLMDecision,
    ) -> ExecutionResult:
        return ExecutionResult(
            placed=False,
            note=(
                "executor=noop — no broker configured; decision was "
                f"'{decision.action}' at confidence {decision.confidence:.2f}"
            ),
        )


__all__ = ["Executor", "ExecutionResult", "NoopExecutor"]
