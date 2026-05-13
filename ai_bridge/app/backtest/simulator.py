"""Trade simulator + metrics for the backtest harness.

Two execution modes are supported via ``exit_mode``:

  * ``"fixed"``   — classic ATR-based SL + RR take-profit + timeout-in-bars.
                    Use this for strategies that don't emit explicit exits
                    (legacy ``smartgold`` engine).
  * ``"indicator"`` — respect ``exit_long`` / ``exit_short`` signals emitted
                      by the engine. SL is still honoured (as emergency
                      stop) but take-profit is **not** used; the indicator
                      decides when to close. This is the correct mode for
                      the new ``psar_ema_vol`` engine.

Both modes return the same ``TradeResult`` schema so :func:`summarise`
works uniformly.

Public API:
  * :func:`simulate_trades`     → list[TradeResult]
  * :func:`summarise`           → dict with win_rate, profit_factor, etc.
  * :func:`metrics_via_vectorbt` (optional cross-check)
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass(slots=True)
class TradeResult:
    """Single simulated trade outcome."""

    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    side: str                   # "long" | "short"
    entry_price: float
    stop_price: float
    take_price: float | None    # None in indicator-exit mode
    exit_price: float
    pnl_points: float
    pnl_r: float
    outcome: str                # "win" | "loss" | "breakeven"
    reason: str                 # tp | sl | timeout | indicator_exit | psar_flip | trend_break | time_max | volume_fade
    bars_held: int
    extra: dict = field(default_factory=dict)


# ══════════════════════════════════════════════════════════════════════
# Internal helpers — bar-walking for "fixed" mode
# ══════════════════════════════════════════════════════════════════════
def _exit_long_fixed(
    bars: pd.DataFrame,
    entry_idx: int,
    stop: float,
    take: float,
    max_bars: int,
) -> tuple[int, float, str]:
    last = min(len(bars) - 1, entry_idx + max_bars)
    for i in range(entry_idx + 1, last + 1):
        hi = float(bars["high"].iloc[i])
        lo = float(bars["low"].iloc[i])
        if lo <= stop:
            return i, stop, "sl"
        if hi >= take:
            return i, take, "tp"
    return last, float(bars["close"].iloc[last]), "timeout"


def _exit_short_fixed(
    bars: pd.DataFrame,
    entry_idx: int,
    stop: float,
    take: float,
    max_bars: int,
) -> tuple[int, float, str]:
    last = min(len(bars) - 1, entry_idx + max_bars)
    for i in range(entry_idx + 1, last + 1):
        hi = float(bars["high"].iloc[i])
        lo = float(bars["low"].iloc[i])
        if hi >= stop:
            return i, stop, "sl"
        if lo <= take:
            return i, take, "tp"
    return last, float(bars["close"].iloc[last]), "timeout"


# ══════════════════════════════════════════════════════════════════════
# Internal helpers — "indicator" mode
# ══════════════════════════════════════════════════════════════════════
def _walk_until_exit(
    bars: pd.DataFrame,
    entry_idx: int,
    side: str,
    stop: float,
    max_bars: int,
    exits_by_idx: dict[int, dict],
) -> tuple[int, float, str]:
    """Walk forward and exit on: engine exit signal, emergency stop, or
    max_bars timeout.

    ``exits_by_idx`` maps bar_index -> {'signal': 'exit_long', 'reason': ...}
    """
    last = min(len(bars) - 1, entry_idx + max_bars)
    for i in range(entry_idx + 1, last + 1):
        hi = float(bars["high"].iloc[i])
        lo = float(bars["low"].iloc[i])
        # Emergency stop still respected (protects against runaway gaps)
        if side == "long" and lo <= stop:
            return i, stop, "sl_emergency"
        if side == "short" and hi >= stop:
            return i, stop, "sl_emergency"
        # Engine-emitted exit on this bar?
        ex = exits_by_idx.get(i)
        if ex is not None:
            want = "exit_long" if side == "long" else "exit_short"
            if ex["signal"] == want:
                return i, float(bars["close"].iloc[i]), ex.get("reason", "indicator_exit")
    return last, float(bars["close"].iloc[last]), "time_max"


# ══════════════════════════════════════════════════════════════════════
# Public simulator
# ══════════════════════════════════════════════════════════════════════
def simulate_trades(
    df: pd.DataFrame,
    signals: Iterable,
    *,
    accept_fn=None,
    stop_atr_mult: float = 1.5,
    rr: float = 2.0,
    max_bars: int = 48,
    exit_mode: str = "fixed",
) -> list[TradeResult]:
    """Walk each entry signal forward and record the exit.

    Args:
        df:            OHLCV DataFrame (DatetimeIndex).
        signals:       iterable of SignalRow (entries and/or exits).
        accept_fn:     optional callable(signal_row) -> (accept, meta).
                       Only called for ENTRY signals.
        stop_atr_mult: stop distance = ATR × this value.
        rr:            take profit = stop_distance × rr (fixed mode only).
        max_bars:      safety timeout in bars.
        exit_mode:     "fixed" (SL/TP/timeout) or "indicator"
                       (engine-emitted exits + emergency stop).
    """
    if exit_mode not in ("fixed", "indicator"):
        raise ValueError(f"exit_mode must be 'fixed' or 'indicator', got {exit_mode!r}")
    if df.empty:
        return []

    bars = df.sort_index()
    idx_lookup = {ts: i for i, ts in enumerate(bars.index)}

    # Split signals into entries and exits (exits only used in indicator mode)
    entry_names = {"long", "short", "strong_long", "strong_short",
                   "bull_choch", "bear_choch", "bull_bos", "bear_bos",
                   "bull_grab", "bear_grab"}
    exit_names = {"exit_long", "exit_short"}

    sig_list = list(signals)
    exits_by_idx: dict[int, dict] = {}
    for sig in sig_list:
        if sig.signal in exit_names and sig.ts in idx_lookup:
            exits_by_idx[idx_lookup[sig.ts]] = {
                "signal": sig.signal,
                "reason": getattr(sig, "exit_reason", None) or "indicator_exit",
            }

    out: list[TradeResult] = []
    # Sort entries chronologically, skip ones inside an already-open position
    entry_sigs = [s for s in sig_list if s.signal in entry_names]
    entry_sigs.sort(key=lambda s: s.ts)

    cursor_idx = -1  # bar index we've simulated up to; skip overlaps
    for sig in entry_sigs:
        if sig.ts not in idx_lookup:
            continue
        entry_idx = idx_lookup[sig.ts]
        if entry_idx <= cursor_idx:
            # Skip — still inside a previously opened trade
            continue
        if entry_idx >= len(bars) - 1:
            continue

        decision_meta: dict = {}
        if accept_fn is not None:
            accept, decision_meta = accept_fn(sig)
            if not accept:
                continue

        side = "long" if ("long" in sig.signal or "bull" in sig.signal) else "short"
        stop_distance = stop_atr_mult * (sig.atr or 0.0)
        if stop_distance <= 0 or math.isnan(stop_distance):
            continue

        entry_price = float(sig.price)

        if exit_mode == "fixed":
            if side == "long":
                stop = entry_price - stop_distance
                take = entry_price + stop_distance * rr
                exit_idx, exit_price, reason = _exit_long_fixed(
                    bars, entry_idx, stop, take, max_bars
                )
                pnl_points = exit_price - entry_price
            else:
                stop = entry_price + stop_distance
                take = entry_price - stop_distance * rr
                exit_idx, exit_price, reason = _exit_short_fixed(
                    bars, entry_idx, stop, take, max_bars
                )
                pnl_points = entry_price - exit_price
            take_ref: float | None = take
        else:  # indicator
            if side == "long":
                stop = entry_price - stop_distance
            else:
                stop = entry_price + stop_distance
            exit_idx, exit_price, reason = _walk_until_exit(
                bars, entry_idx, side, stop, max_bars, exits_by_idx,
            )
            pnl_points = (
                exit_price - entry_price if side == "long"
                else entry_price - exit_price
            )
            take_ref = None

        pnl_r = pnl_points / stop_distance if stop_distance > 0 else 0.0
        if reason == "tp":
            outcome = "win"
        elif reason in ("sl", "sl_emergency"):
            outcome = "loss"
        else:
            outcome = (
                "win" if pnl_points > 0
                else "loss" if pnl_points < 0
                else "breakeven"
            )

        out.append(
            TradeResult(
                entry_ts=sig.ts,
                exit_ts=bars.index[exit_idx],
                side=side,
                entry_price=entry_price,
                stop_price=stop,
                take_price=take_ref,
                exit_price=exit_price,
                pnl_points=float(pnl_points),
                pnl_r=float(pnl_r),
                outcome=outcome,
                reason=reason,
                bars_held=exit_idx - entry_idx,
                extra=decision_meta,
            )
        )
        cursor_idx = exit_idx

    return out


# ══════════════════════════════════════════════════════════════════════
# Metrics
# ══════════════════════════════════════════════════════════════════════
def summarise(trades: list[TradeResult]) -> dict:
    """Aggregate standard performance metrics across a list of trades."""
    if not trades:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "breakevens": 0,
            "win_rate": None,
            "avg_win_r": None,
            "avg_loss_r": None,
            "expectancy_r": None,
            "profit_factor": None,
            "total_r": 0.0,
            "max_drawdown_r": 0.0,
            "sharpe_r": None,
            "avg_bars_held": None,
        }

    wins = [t for t in trades if t.outcome == "win"]
    losses = [t for t in trades if t.outcome == "loss"]
    bes = [t for t in trades if t.outcome == "breakeven"]

    gross_win_r = sum(t.pnl_r for t in wins)
    gross_loss_r = abs(sum(t.pnl_r for t in losses))
    decisive = len(wins) + len(losses)

    win_rate = (len(wins) / decisive) if decisive > 0 else None
    avg_win_r = (gross_win_r / len(wins)) if wins else None
    avg_loss_r = (gross_loss_r / len(losses)) if losses else None
    expectancy = (
        (win_rate * avg_win_r - (1 - win_rate) * avg_loss_r)
        if win_rate is not None and avg_win_r is not None and avg_loss_r is not None
        else None
    )
    profit_factor = (
        (gross_win_r / gross_loss_r)
        if gross_loss_r > 0
        else (float("inf") if gross_win_r > 0 else None)
    )

    r_series = np.array([t.pnl_r for t in trades], dtype=float)
    equity = np.cumsum(r_series)
    peak = np.maximum.accumulate(equity)
    drawdown = peak - equity
    max_dd = float(drawdown.max()) if drawdown.size else 0.0
    sharpe = (
        float(r_series.mean() / r_series.std(ddof=1))
        if len(r_series) > 1 and r_series.std(ddof=1) > 0
        else None
    )
    avg_bars = (
        float(np.mean([t.bars_held for t in trades])) if trades else None
    )

    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "breakevens": len(bes),
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "avg_win_r": round(avg_win_r, 3) if avg_win_r is not None else None,
        "avg_loss_r": round(avg_loss_r, 3) if avg_loss_r is not None else None,
        "expectancy_r": round(expectancy, 3) if expectancy is not None else None,
        "profit_factor": (
            round(profit_factor, 3)
            if profit_factor is not None and math.isfinite(profit_factor)
            else profit_factor
        ),
        "total_r": round(float(r_series.sum()), 3),
        "max_drawdown_r": round(max_dd, 3),
        "sharpe_r": round(sharpe, 3) if sharpe is not None else None,
        "avg_bars_held": round(avg_bars, 2) if avg_bars is not None else None,
    }


def metrics_via_vectorbt(trades: list[TradeResult]) -> dict | None:
    """Cross-check win rate + total R using vectorbt when available."""
    try:
        import vectorbt as vbt  # type: ignore[import-not-found]
    except ImportError:
        return None

    if not trades:
        return {"trades": 0, "win_rate": None, "total_r": 0.0}

    pnl = pd.Series([t.pnl_r for t in trades])
    stats = {
        "trades": int(len(pnl)),
        "win_rate": float((pnl > 0).mean()),
        "total_r": float(pnl.sum()),
        "sharpe_vbt": float(
            vbt.utils.math_.nanmean(pnl) / (vbt.utils.math_.nanstd(pnl) or 1)
        ),
    }
    return stats


__all__ = ["TradeResult", "simulate_trades", "summarise", "metrics_via_vectorbt"]
