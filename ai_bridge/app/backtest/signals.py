"""Python port of the core SmartGold Pine signals — backtest-only.

The Pine script produces strong_long / strong_short on confirmed bars.
We reproduce the same logic in pandas so backtests run without a live
chart. This is NOT meant to replace the Pine indicator — it's a
faithful-enough facsimile for evaluating how different LLM prompts would
have filtered historical signals.

Supports multiple signal engines via a simple dispatch:
  * "smartgold"  — the default, mirrors the Pine v2 `strong_long`/`strong_short`
                   conditions: structure bias + liquidity grab + EMA stack +
                   RSI/money-flow gates.
  * other names  — caller-supplied engines, added via register_engine().
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(slots=True)
class SignalRow:
    """One backtest signal row. Mirrors TradingViewAlert fields."""

    ts: pd.Timestamp
    symbol: str
    timeframe: str
    signal: str           # "strong_long" | "strong_short"
    price: float
    ms_state: str         # "bullish" | "bearish" | "neutral"
    rsi: float
    atr: float
    money_flow: float
    ema_fast: float
    ema_slow: float
    ema_base: float


def _ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def _rsi(series: pd.Series, n: int = 14) -> pd.Series:
    """Classic Wilder RSI."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False).mean()


def _money_flow(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """Volume-weighted directional pressure, mapped to 0..100."""
    rng = (df["high"] - df["low"]).replace(0.0, np.nan)
    mf_raw = (df["close"] - df["open"]) / rng * df["volume"]
    mf_sum = mf_raw.rolling(n, min_periods=1).sum()
    vol_sum = df["volume"].rolling(n, min_periods=1).sum().replace(0.0, np.nan)
    return (mf_sum / vol_sum * 50.0 + 50.0).clip(0.0, 100.0).fillna(50.0)


def _liq_grab(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    liq_len: int = 20,
) -> tuple[pd.Series, pd.Series]:
    """Bull/bear liquidity sweep heuristic (matches Pine v2 definition)."""
    recent_lo = low.rolling(liq_len, min_periods=1).min().shift(1)
    recent_hi = high.rolling(liq_len, min_periods=1).max().shift(1)
    bull = (low < recent_lo) & (close > low) & ((close - low) > (high - close) * 0.5)
    bear = (high > recent_hi) & (close < high) & ((high - close) > (close - low) * 0.5)
    return bull.fillna(False), bear.fillna(False)


def _ms_state(close: pd.Series, fast: pd.Series, slow: pd.Series, base: pd.Series) -> pd.Series:
    """Coarse market structure bias from EMA stacking (cheap proxy for BOS/CHoCH)."""
    bull = (fast > slow) & (slow > base)
    bear = (fast < slow) & (slow < base)
    return np.where(bull, "bullish", np.where(bear, "bearish", "neutral"))


# ──────────────────────────────────────────────────────────────────────
# Engine: smartgold (Pine v2 port)
# ──────────────────────────────────────────────────────────────────────
def smartgold_engine(
    df: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    ema_fast: int = 21,
    ema_slow: int = 50,
    ema_base: int = 200,
    atr_len: int = 14,
    rsi_len: int = 14,
    liq_len: int = 20,
) -> list[SignalRow]:
    """Return strong_long / strong_short rows. Input df must be sorted ascending.

    Expected columns: open, high, low, close, volume (case-insensitive).
    Index must be a DatetimeIndex.
    """
    df = _normalise_ohlcv(df)

    ema_f = _ema(df["close"], ema_fast)
    ema_s = _ema(df["close"], ema_slow)
    ema_b = _ema(df["close"], ema_base)
    atr = _atr(df["high"], df["low"], df["close"], atr_len)
    rsi = _rsi(df["close"], rsi_len)
    mf = _money_flow(df)
    bull_grab, bear_grab = _liq_grab(df["high"], df["low"], df["close"], liq_len)
    ms = _ms_state(df["close"], ema_f, ema_s, ema_b)

    bull_trend = (ema_f > ema_s) & (ema_s > ema_b)
    bear_trend = (ema_f < ema_s) & (ema_s < ema_b)

    long_sig = (
        (pd.Series(ms, index=df.index) == "bullish")
        & bull_grab
        & (rsi < 60)
        & (ema_f > ema_s)
        & (mf > 50)
    )
    short_sig = (
        (pd.Series(ms, index=df.index) == "bearish")
        & bear_grab
        & (rsi > 40)
        & (ema_f < ema_s)
        & (mf < 50)
    )
    strong_long = long_sig & bull_trend & (rsi < 55) & (mf > 60)
    strong_short = short_sig & bear_trend & (rsi > 45) & (mf < 40)

    rows: list[SignalRow] = []
    for i, ts in enumerate(df.index):
        if strong_long.iloc[i]:
            name = "strong_long"
        elif strong_short.iloc[i]:
            name = "strong_short"
        else:
            continue
        # Skip warm-up NaNs — happens in the first ~ema_base bars.
        if pd.isna(atr.iloc[i]) or pd.isna(rsi.iloc[i]):
            continue
        rows.append(
            SignalRow(
                ts=ts,
                symbol=symbol,
                timeframe=timeframe,
                signal=name,
                price=float(df["close"].iloc[i]),
                ms_state=str(ms[i]),
                rsi=float(rsi.iloc[i]),
                atr=float(atr.iloc[i]),
                money_flow=float(mf.iloc[i]),
                ema_fast=float(ema_f.iloc[i]),
                ema_slow=float(ema_s.iloc[i]),
                ema_base=float(ema_b.iloc[i]),
            )
        )
    return rows


def _normalise_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Lowercase columns and ensure required OHLCV set present."""
    mapped = {c: c.lower() for c in df.columns}
    out = df.rename(columns=mapped).copy()
    required = {"open", "high", "low", "close"}
    missing = required - set(out.columns)
    if missing:
        raise ValueError(f"OHLCV missing columns: {sorted(missing)}")
    if "volume" not in out.columns:
        # yfinance/synthetic data without volume — use 1.0 to avoid div/0.
        out["volume"] = 1.0
    if not isinstance(out.index, pd.DatetimeIndex):
        raise ValueError("DataFrame index must be a DatetimeIndex")
    return out.sort_index()


# ──────────────────────────────────────────────────────────────────────
# Public engine registry
# ──────────────────────────────────────────────────────────────────────
EngineFn = Callable[..., list[SignalRow]]

_ENGINES: dict[str, EngineFn] = {"smartgold": smartgold_engine}


def register_engine(name: str, fn: EngineFn) -> None:
    _ENGINES[name] = fn


def get_engine(name: str = "smartgold") -> EngineFn:
    if name not in _ENGINES:
        raise KeyError(
            f"unknown signal engine '{name}'; have {sorted(_ENGINES)}"
        )
    return _ENGINES[name]


__all__ = [
    "SignalRow",
    "smartgold_engine",
    "register_engine",
    "get_engine",
]
