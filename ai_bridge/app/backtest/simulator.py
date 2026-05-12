"""Trade simulator + metrics for the backtest harness.

Design choice: we roll our own minimal simulator instead of wiring vectorbt
into the core path, because the per-signal exit logic (ATR stop, ATR×RR
take-profit, optional timeout-in-bars) is much clearer expressed directly
than via vectorbt Signals. The results can still be cross-checked with
vectorbt via ``metrics_via_vectorbt()`` when the ``vectorbt`` package is
installed — otherwise that helper is skipped with a clear message.

Public API:
  * :func:`simulate_trades`  → list[TradeResult]
  * :func:`summarise`        → dict with win_rate, profit_factor, expectancy, etc.
  * :func:`metrics_via_vectorbt` (optional sanity cross-check)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

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
    take_price: float
    exit_price: float
    pnl_points: float           # raw price units
    pnl_r: float                # multiples of risk (R)
    outcome: str                # "win" | "loss" | "breakeven"
    reason: str                 # "tp" | "sl" | "timeout"
    bars_held: int
    extra: dict = field(default_factory=dict)


def _exit_long(
    bars: pd.DataFrame,
    entry_idx: int,
    entry_price: float,
    stop: float,
    take: float,
    max_bars: int,
) -> tuple[int, float, str]:
    """Walk forward bar-by-bar; return (exit_idx, exit_price, reason).

    Conservative tie-break: if both SL and TP are hit inside the same bar
    we assume the stop fired first (worst case for long).
    """
    last = min(len(bars) - 1, entry_idx + max_bars)
    for i in range(entry_idx + 1, last + 1):
        hi = float(bars["high"].iloc[i])
        lo = float(bars["low"].iloc[i])
        if lo <= stop:
            return i, stop, "sl"
        if hi >= take:
            return i, take, "tp"
    return last, float(bars["close"].iloc[last]), "timeout"


def _exit_short(
    bars: pd.DataFrame,
    entry_idx: int,
    entry_price: float,
    stop: float,
    take: float,
    max_bars: int,
) -> tuple[int, float, str]:
    last = min(len(bars) - 1, entry_idx + max_bars)
    for i in range(entry_idx + 1, last + 1):
        hi = float(bars["high"].iloc[i])
        lo = float(bars["low"].iloc[i])
        # Symmetric worst-case: SL (upside breach) before TP.
        if hi >= stop:
            return i, stop, "sl"
        if lo <= take:
            return i, take, "tp"
    return last, float(bars["close"].iloc[last]), "timeout"


def simulate_trades(
    df: pd.DataFrame,
    signals: Iterable,
    *,
    accept_fn=None,
    stop_atr_mult: float = 1.5,
    rr: float = 2.0,
    max_bars: int = 48,
) -> list[TradeResult]:
    """Walk each signal forward and record the exit.

    Args:
        df:        OHLCV DataFrame indexed by DatetimeIndex (same one used
                   by the engine).
        signals:   iterable of SignalRow (from app.backtest.signals).
        accept_fn: optional callable(signal_row) -> (accept: bool, decision_meta: dict).
                   Use this to plug in the LLM filter, a simple rule, or
                   return (True, {}) to accept every signal.
        stop_atr_mult: stop distance = ATR × this value.
        rr:        take profit = stop_distance × rr.
        max_bars:  timeout after this many bars if neither SL nor TP hit.
    """
    if df.empty:
        return []

    # df must be sorted; signals should come from the same df.
    bars = df.sort_index()
    idx_lookup = {ts: i for i, ts in enumerate(bars.index)}

    out: list[TradeResult] = []
    for sig in signals:
        if sig.ts not in idx_lookup:
            continue
        entry_idx = idx_lookup[sig.ts]
        # Skip signals too late to simulate meaningfully
        if entry_idx >= len(bars) - 1:
            continue

        decision_meta: dict = {}
        if accept_fn is not None:
            accept, decision_meta = accept_fn(sig)
            if not accept:
                continue

        side = "long" if "long" in sig.signal or "bull" in sig.signal else "short"
        stop_distance = stop_atr_mult * sig.atr
        if stop_distance <= 0 or math.isnan(stop_distance):
            continue

        entry_price = float(sig.price)
        if side == "long":
            stop = entry_price - stop_distance
            take = entry_price + stop_distance * rr
            exit_idx, exit_price, reason = _exit_long(
                bars, entry_idx, entry_price, stop, take, max_bars
            )
            pnl_points = exit_price - entry_price
        else:
            stop = entry_price + stop_distance
            take = entry_price - stop_distance * rr
            exit_idx, exit_price, reason = _exit_short(
                bars, entry_idx, entry_price, stop, take, max_bars
            )
            pnl_points = entry_price - exit_price

        pnl_r = pnl_points / stop_distance if stop_distance > 0 else 0.0
        if reason == "tp":
            outcome = "win"
        elif reason == "sl":
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
                take_price=take,
                exit_price=exit_price,
                pnl_points=float(pnl_points),
                pnl_r=float(pnl_r),
                outcome=outcome,
                reason=reason,
                bars_held=exit_idx - entry_idx,
                extra=decision_meta,
            )
        )
    return out


# ──────────────────────────────────────────────────────────────────────
# Metrics
# ──────────────────────────────────────────────────────────────────────
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

    # Cumulative R curve for drawdown + Sharpe
    r_series = np.array([t.pnl_r for t in trades], dtype=float)
    equity = np.cumsum(r_series)
    peak = np.maximum.accumulate(equity)
    drawdown = peak - equity
    max_dd = float(drawdown.max()) if drawdown.size else 0.0
    sharpe = float(r_series.mean() / r_series.std(ddof=1)) if len(r_series) > 1 and r_series.std(ddof=1) > 0 else None

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
    }


def metrics_via_vectorbt(trades: list[TradeResult]) -> dict | None:
    """Cross-check win rate + total R using vectorbt when available.

    Returns None (with a log hint) if the package is not installed, so
    the core backtest loop stays usable in minimal environments.
    """
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
        "sharpe_vbt": float(vbt.utils.math_.nanmean(pnl) / (vbt.utils.math_.nanstd(pnl) or 1)),
    }
    return stats


__all__ = ["TradeResult", "simulate_trades", "summarise", "metrics_via_vectorbt"]
