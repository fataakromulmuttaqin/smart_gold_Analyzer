"""Breakeven stop shift logic.

When a position moves ``trigger_r`` multiples of risk in favour, the stop
is moved to ``entry ± buffer_atr_mult × ATR`` (slightly in profit) —
converting a potential loser into a scratch trade.

Live (MT5) side: called periodically (e.g. every 10s) to reconcile open
positions. The check compares current price to entry and trigger, and
sends a modify-order request via MT5 if the trigger has been reached
AND the current SL is still at or below the breakeven target.

Backtest side: integrated into the simulator walk to apply the same
logic bar-by-bar.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class BreakevenCheckResult:
    should_shift: bool
    new_stop: float | None = None
    reason: str = ""


def check_breakeven_long(
    *,
    entry_price: float,
    current_price: float,
    current_stop: float,
    atr: float,
    stop_distance: float,     # Original stop distance (entry - original SL)
    trigger_r: float,         # Shift when price has moved this many R
    buffer_atr_mult: float,   # New SL offset above entry (anti-breakeven-stopout)
) -> BreakevenCheckResult:
    """For a LONG position, check if we should shift SL to breakeven."""
    if stop_distance <= 0 or trigger_r <= 0:
        return BreakevenCheckResult(False, reason="invalid_inputs")

    gain = current_price - entry_price
    gain_r = gain / stop_distance

    if gain_r < trigger_r:
        return BreakevenCheckResult(
            False,
            reason=f"gain_r={gain_r:.2f} < trigger_r={trigger_r:.2f}",
        )

    new_stop = entry_price + buffer_atr_mult * atr
    # Only shift UP (never loosen an already-tightened stop)
    if new_stop <= current_stop:
        return BreakevenCheckResult(
            False,
            new_stop=new_stop,
            reason=f"already_tighter (current={current_stop:.4f}, target={new_stop:.4f})",
        )

    return BreakevenCheckResult(
        True,
        new_stop=new_stop,
        reason=f"gain_r={gain_r:.2f} ≥ trigger, shifting to {new_stop:.4f}",
    )


def check_breakeven_short(
    *,
    entry_price: float,
    current_price: float,
    current_stop: float,
    atr: float,
    stop_distance: float,     # Original stop distance (original SL - entry)
    trigger_r: float,
    buffer_atr_mult: float,
) -> BreakevenCheckResult:
    """For a SHORT position, check if we should shift SL to breakeven."""
    if stop_distance <= 0 or trigger_r <= 0:
        return BreakevenCheckResult(False, reason="invalid_inputs")

    gain = entry_price - current_price
    gain_r = gain / stop_distance

    if gain_r < trigger_r:
        return BreakevenCheckResult(
            False,
            reason=f"gain_r={gain_r:.2f} < trigger_r={trigger_r:.2f}",
        )

    new_stop = entry_price - buffer_atr_mult * atr
    # Only shift DOWN for short
    if new_stop >= current_stop:
        return BreakevenCheckResult(
            False,
            new_stop=new_stop,
            reason=f"already_tighter (current={current_stop:.4f}, target={new_stop:.4f})",
        )

    return BreakevenCheckResult(
        True,
        new_stop=new_stop,
        reason=f"gain_r={gain_r:.2f} ≥ trigger, shifting to {new_stop:.4f}",
    )


__all__ = [
    "BreakevenCheckResult",
    "check_breakeven_long",
    "check_breakeven_short",
]
