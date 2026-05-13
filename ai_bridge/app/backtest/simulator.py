"""Trade simulator + metrics for the backtest harness.

Two exit modes:
  * ``fixed_rr``  — ATR-based SL + RR take-profit + timeout.
  * ``indicator`` — SL (via ``app.risk``) + EMA-cross indicator exit.

Both modes support optional **breakeven shift**: once the trade has moved
``breakeven_trigger_r`` × stop_distance in favour, the SL is moved to
``entry ± breakeven_buffer_atr_mult × ATR``. This mirrors the live MT5
reconciler so backtest ↔ live match up.

Stop distance is computed via ``app.risk.stop_calculator`` so backtest
uses the same policy (hybrid / psar / atr) as the live executor.

Public API:
  * :func:`simulate_trades`  → list[TradeResult]
  * :func:`summarise`        → dict of metrics
  * :func:`metrics_via_vectorbt` (optional cross-check)
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.risk.breakeven import check_breakeven_long, check_breakeven_short
from app.risk.stop_calculator import (
    ATRStop,
    HybridATRPsarStop,
    PSARStop,
    StopCalculator,
)


@dataclass(slots=True)
class TradeResult:
    """Single simulated trade outcome."""

    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    side: str                   # "long" | "short"
    entry_price: float
    stop_price: float           # Final SL (may be breakeven-shifted)
    initial_stop: float         # SL as originally placed at entry
    take_price: float | None    # None in indicator mode
    exit_price: float
    pnl_points: float           # raw price units
    pnl_r: float                # multiples of ORIGINAL risk (not shifted)
    outcome: str                # "win" | "loss" | "breakeven"
    reason: str                 # tp | sl | sl_breakeven | indicator | timeout
    bars_held: int
    breakeven_shifted: bool = False
    sl_source: str = ""         # Which stop policy produced this
    sl_atr_mult: float = 0.0    # Effective ATR multiple used
    extra: dict = field(default_factory=dict)


def _build_stop_calc(policy: str, atr_mult: float, min_mult: float, max_mult: float) -> StopCalculator:
    """Instantiate stop policy from CLI-style string."""
    p = policy.lower()
    if p == "hybrid":
        return HybridATRPsarStop(min_atr_mult=min_mult, max_atr_mult=max_mult)
    if p == "psar":
        return PSARStop(atr_fallback_mult=atr_mult)
    return ATRStop(atr_mult=atr_mult)


def simulate_trades(
    df: pd.DataFrame,
    signals: Iterable,
    *,
    accept_fn=None,
    stop_atr_mult: float = 1.5,
    rr: float = 2.0,
    max_bars: int = 48,
    exit_mode: str = "fixed_rr",
    # ── Stop policy (shared with live MT5 executor) ──
    stop_policy: str = "atr",  # "atr" | "psar" | "hybrid"
    stop_min_atr_mult: float = 0.8,
    stop_max_atr_mult: float = 2.5,
    # ── Breakeven shift ──
    breakeven_enabled: bool = False,
    breakeven_trigger_r: float = 1.0,
    breakeven_buffer_atr_mult: float = 0.1,
) -> list[TradeResult]:
    """Walk each signal forward and record the exit.

    Args:
        df:            OHLCV DataFrame indexed by DatetimeIndex.
        signals:       iterable of SignalRow.
        accept_fn:     optional callable(sig) -> (accept, meta).
        stop_atr_mult: ATR multiple for "atr" policy (ignored if "hybrid"/"psar" and PSAR present).
        rr:            take-profit distance in R (only used in fixed_rr mode).
        max_bars:      timeout after this many bars.
        exit_mode:     "fixed_rr" or "indicator".
        stop_policy:   "atr" | "psar" | "hybrid" — selects SL calculator.
        stop_min_atr_mult / stop_max_atr_mult: bounds for hybrid policy.
        breakeven_enabled: if True, shift SL to breakeven once trigger_r hit.
        breakeven_trigger_r: R-multiple at which to shift SL.
        breakeven_buffer_atr_mult: new SL offset from entry (0.1×ATR default).
    """
    if df.empty:
        return []

    bars = df.sort_index()
    idx_lookup = {ts: i for i, ts in enumerate(bars.index)}

    # Indicator exit signals for "indicator" mode (EMA 21/50 cross)
    indicator_exit_long = None
    indicator_exit_short = None
    if exit_mode == "indicator":
        ema_f = bars["close"].ewm(span=21, adjust=False).mean()
        ema_s = bars["close"].ewm(span=50, adjust=False).mean()
        indicator_exit_long = (ema_f < ema_s) & (ema_f.shift(1) >= ema_s.shift(1))
        indicator_exit_short = (ema_f > ema_s) & (ema_f.shift(1) <= ema_s.shift(1))

    stop_calc = _build_stop_calc(
        stop_policy, stop_atr_mult, stop_min_atr_mult, stop_max_atr_mult,
    )

    out: list[TradeResult] = []
    for sig in signals:
        if sig.ts not in idx_lookup:
            continue
        entry_idx = idx_lookup[sig.ts]
        if entry_idx >= len(bars) - 1:
            continue

        decision_meta: dict = {}
        if accept_fn is not None:
            accept, decision_meta = accept_fn(sig)
            if not accept:
                continue

        side = "long" if "long" in sig.signal or "bull" in sig.signal else "short"
        entry_price = float(sig.price)

        # Compute stop distance via the SAME calculator as live MT5
        psar_val = getattr(sig, "psar", None)
        stop_res = stop_calc.calculate(
            side=side,
            entry_price=entry_price,
            atr=sig.atr if sig.atr > 0 else None,
            psar=psar_val,
        )
        stop_distance = stop_res.distance
        if stop_distance <= 0 or math.isnan(stop_distance):
            continue

        # ── Walk forward bar by bar ───────────────────────────────────
        if side == "long":
            current_sl = entry_price - stop_distance
            initial_sl = current_sl
            take = entry_price + stop_distance * rr
        else:
            current_sl = entry_price + stop_distance
            initial_sl = current_sl
            take = entry_price - stop_distance * rr

        last = min(len(bars) - 1, entry_idx + max_bars)
        exit_idx = last
        exit_price = float(bars["close"].iloc[last])
        reason = "timeout"
        was_shifted = False

        for i in range(entry_idx + 1, last + 1):
            hi = float(bars["high"].iloc[i])
            lo = float(bars["low"].iloc[i])
            close = float(bars["close"].iloc[i])

            # 1. Check SL / TP hits (SL wins tie-break — conservative)
            if side == "long":
                if lo <= current_sl:
                    exit_idx = i
                    exit_price = current_sl
                    reason = "sl_breakeven" if was_shifted and current_sl >= entry_price else "sl"
                    break
                if exit_mode == "fixed_rr" and hi >= take:
                    exit_idx = i
                    exit_price = take
                    reason = "tp"
                    break
                if exit_mode == "indicator" and indicator_exit_long is not None and indicator_exit_long.iloc[i]:
                    exit_idx = i
                    exit_price = close
                    reason = "indicator"
                    break
            else:  # short
                if hi >= current_sl:
                    exit_idx = i
                    exit_price = current_sl
                    reason = "sl_breakeven" if was_shifted and current_sl <= entry_price else "sl"
                    break
                if exit_mode == "fixed_rr" and lo <= take:
                    exit_idx = i
                    exit_price = take
                    reason = "tp"
                    break
                if exit_mode == "indicator" and indicator_exit_short is not None and indicator_exit_short.iloc[i]:
                    exit_idx = i
                    exit_price = close
                    reason = "indicator"
                    break

            # 2. Check breakeven shift (using intra-bar extreme, conservative = close)
            if breakeven_enabled and not was_shifted and sig.atr > 0:
                if side == "long":
                    be = check_breakeven_long(
                        entry_price=entry_price,
                        current_price=close,
                        current_stop=current_sl,
                        atr=sig.atr,
                        stop_distance=stop_distance,
                        trigger_r=breakeven_trigger_r,
                        buffer_atr_mult=breakeven_buffer_atr_mult,
                    )
                else:
                    be = check_breakeven_short(
                        entry_price=entry_price,
                        current_price=close,
                        current_stop=current_sl,
                        atr=sig.atr,
                        stop_distance=stop_distance,
                        trigger_r=breakeven_trigger_r,
                        buffer_atr_mult=breakeven_buffer_atr_mult,
                    )
                if be.should_shift and be.new_stop is not None:
                    current_sl = be.new_stop
                    was_shifted = True

        pnl_points = (exit_price - entry_price) if side == "long" else (entry_price - exit_price)
        pnl_r = pnl_points / stop_distance if stop_distance > 0 else 0.0

        if reason == "tp":
            outcome = "win"
        elif reason == "sl":
            outcome = "loss"
        elif reason == "sl_breakeven":
            outcome = "win" if pnl_points > 0 else "breakeven" if pnl_points == 0 else "loss"
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
                stop_price=current_sl,
                initial_stop=initial_sl,
                take_price=take if exit_mode == "fixed_rr" else None,
                exit_price=exit_price,
                pnl_points=float(pnl_points),
                pnl_r=float(pnl_r),
                outcome=outcome,
                reason=reason,
                bars_held=exit_idx - entry_idx,
                breakeven_shifted=was_shifted,
                sl_source=stop_res.source,
                sl_atr_mult=round(stop_res.atr_mult_effective, 3),
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
            "trades": 0, "wins": 0, "losses": 0, "breakevens": 0,
            "win_rate": None, "avg_win_r": None, "avg_loss_r": None,
            "expectancy_r": None, "profit_factor": None,
            "total_r": 0.0, "max_drawdown_r": 0.0, "sharpe_r": None,
            "breakeven_shift_pct": None,
        }

    wins = [t for t in trades if t.outcome == "win"]
    losses = [t for t in trades if t.outcome == "loss"]
    bes = [t for t in trades if t.outcome == "breakeven"]
    shifted = [t for t in trades if t.breakeven_shifted]

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
        if len(r_series) > 1 and r_series.std(ddof=1) > 0 else None
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
        "breakeven_shift_pct": round(len(shifted) / len(trades), 4),
    }


def metrics_via_vectorbt(trades: list[TradeResult]) -> dict | None:
    """Cross-check via vectorbt when available (optional dep)."""
    try:
        import vectorbt as vbt  # type: ignore[import-not-found]
    except ImportError:
        return None

    if not trades:
        return {"trades": 0, "win_rate": None, "total_r": 0.0}

    pnl = pd.Series([t.pnl_r for t in trades])
    return {
        "trades": int(len(pnl)),
        "win_rate": float((pnl > 0).mean()),
        "total_r": float(pnl.sum()),
        "sharpe_vbt": float(
            vbt.utils.math_.nanmean(pnl) / (vbt.utils.math_.nanstd(pnl) or 1)
        ),
    }


__all__ = ["TradeResult", "simulate_trades", "summarise", "metrics_via_vectorbt"]
