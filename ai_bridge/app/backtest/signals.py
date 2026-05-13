"""Python port of SmartGold Pine signals — backtest-only.

Two engines ship by default:

  * ``smartgold``     — mirrors the **legacy v1** SMC Pine script
                        (structure bias + liquidity grab + EMA stack +
                        RSI/money-flow gates). Kept for back-compat and
                        A/B comparison against the new strategy.
  * ``psar_ema_vol``  — mirrors the **current v2** Pine script
                        (PSAR + EMA 20/50/100/200 + volume confirmation
                        on H1). Emits entry AND exit signals so the
                        backtest simulator can respect indicator exits
                        in addition to SL/TP.

Callers can add custom engines via :func:`register_engine`.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass(slots=True)
class SignalRow:
    """One backtest signal row.

    Keeps legacy fields as Optional so the `smartgold` engine stays
    backward compatible; new `psar_ema_vol` fields are also Optional.
    """

    ts: pd.Timestamp
    symbol: str
    timeframe: str
    signal: str           # strong_long | strong_short | long | short |
                          # exit_long | exit_short | bull_choch | bear_choch …
    price: float
    # EMA ribbon
    ema_fast: float = 0.0          # EMA 20 (new) or EMA 21 (legacy)
    ema_mid: float | None = None   # EMA 50 (new); None in legacy
    ema_slow: float = 0.0          # EMA 100 (new) or EMA 50 (legacy)
    ema_base: float = 0.0          # EMA 200
    # ATR for position sizing
    atr: float = 0.0
    # ── New strategy (PSAR + EMA + Volume) ──────────────────────────
    psar: float | None = None
    psar_below: bool | None = None
    volume: float | None = None
    volume_sma: float | None = None
    volume_ratio: float | None = None
    bull_trend: bool | None = None
    bear_trend: bool | None = None
    bars_since_entry: int | None = None
    exit_reason: str | None = None
    # ── Legacy SMC strategy (v1) ────────────────────────────────────
    ms_state: str | None = None
    rsi: float | None = None
    money_flow: float | None = None
    # Per-row metadata (for debugging / extra engines)
    extra: dict = field(default_factory=dict)


# ══════════════════════════════════════════════════════════════════════
# Shared indicator helpers
# ══════════════════════════════════════════════════════════════════════
def _ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def _sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n, min_periods=1).mean()


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
    recent_lo = low.rolling(liq_len, min_periods=1).min().shift(1)
    recent_hi = high.rolling(liq_len, min_periods=1).max().shift(1)
    bull = (low < recent_lo) & (close > low) & ((close - low) > (high - close) * 0.5)
    bear = (high > recent_hi) & (close < high) & ((high - close) > (close - low) * 0.5)
    return bull.fillna(False), bear.fillna(False)


def _ms_state(close: pd.Series, fast: pd.Series, slow: pd.Series, base: pd.Series) -> np.ndarray:
    bull = (fast > slow) & (slow > base)
    bear = (fast < slow) & (slow < base)
    return np.where(bull, "bullish", np.where(bear, "bearish", "neutral"))


# ══════════════════════════════════════════════════════════════════════
# Parabolic SAR (Welles Wilder, 1978)
# ══════════════════════════════════════════════════════════════════════
def _parabolic_sar(
    high: pd.Series,
    low: pd.Series,
    *,
    start: float = 0.02,
    increment: float = 0.02,
    maximum: float = 0.20,
) -> tuple[pd.Series, pd.Series]:
    """Return (psar, is_below_price_bool).

    Implementation follows Wilder's rules:
      - AF starts at `start`, incremented by `increment` each new EP,
        capped at `maximum`.
      - On trend flip, reset AF = start and swap EP ↔ prior SAR.
      - Uptrend SAR must not exceed prior two lows (and vice versa for
        downtrend). If it does, clamp to that boundary.
    """
    n = len(high)
    psar = np.full(n, np.nan, dtype=float)
    bull = np.zeros(n, dtype=bool)

    if n < 2:
        return pd.Series(psar, index=high.index), pd.Series(bull, index=high.index)

    # Initial trend inferred from first two bars
    is_bull = high.iloc[1] >= high.iloc[0]
    af = start
    ep = high.iloc[1] if is_bull else low.iloc[1]
    sar = low.iloc[0] if is_bull else high.iloc[0]

    psar[0] = sar
    bull[0] = is_bull

    for i in range(1, n):
        prev_sar = sar
        # Advance SAR
        sar = prev_sar + af * (ep - prev_sar)

        if is_bull:
            # SAR must not exceed min of previous 2 lows
            lo1 = low.iloc[i - 1]
            lo2 = low.iloc[i - 2] if i >= 2 else lo1
            sar = min(sar, lo1, lo2)
            # Flip?
            if low.iloc[i] < sar:
                is_bull = False
                sar = ep                # prior EP becomes new SAR
                ep = low.iloc[i]
                af = start
            else:
                if high.iloc[i] > ep:
                    ep = high.iloc[i]
                    af = min(af + increment, maximum)
        else:
            hi1 = high.iloc[i - 1]
            hi2 = high.iloc[i - 2] if i >= 2 else hi1
            sar = max(sar, hi1, hi2)
            if high.iloc[i] > sar:
                is_bull = True
                sar = ep
                ep = high.iloc[i]
                af = start
            else:
                if low.iloc[i] < ep:
                    ep = low.iloc[i]
                    af = min(af + increment, maximum)

        psar[i] = sar
        bull[i] = is_bull

    return (
        pd.Series(psar, index=high.index, name="psar"),
        pd.Series(bull, index=high.index, name="psar_below"),
    )


# ══════════════════════════════════════════════════════════════════════
# Engine: smartgold (legacy v1 — SMC)
# ══════════════════════════════════════════════════════════════════════
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
    """Return strong_long / strong_short rows (legacy SMC strategy)."""
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


# ══════════════════════════════════════════════════════════════════════
# Engine: psar_ema_vol (NEW v2 strategy — matches smart_gold_analyzer_v2_ai.pine)
# ══════════════════════════════════════════════════════════════════════
def psar_ema_vol_engine(
    df: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    ema_fast_len: int = 20,
    ema_mid_len: int = 50,
    ema_slow_len: int = 100,
    ema_base_len: int = 200,
    psar_start: float = 0.02,
    psar_increment: float = 0.02,
    psar_maximum: float = 0.20,
    vol_sma_len: int = 20,
    vol_entry_mult: float = 1.3,
    vol_strong_mult: float = 1.8,
    exit_min_bars: int = 4,
    exit_max_bars: int = 6,
    atr_len: int = 14,
    emit_weak: bool = False,
) -> list[SignalRow]:
    """Replay the PSAR + EMA Ribbon + Volume strategy and emit rows.

    Entry LONG (all must be true):
      close > EMA200; EMA20>EMA50>EMA100; close crossup EMA20;
      volume > SMA(vol,20) * vol_entry_mult; PSAR below price.

    Entry SHORT: mirror.

    Exit (priority): psar_flip > trend_break (close vs EMA20) >
                     time_max (>= exit_max_bars) >
                     volume_fade (>= exit_min_bars AND vol declining 2 bars).

    The emitted row sequence interleaves entries and exits in
    chronological order so the simulator can respect exit signals.
    """
    df = _normalise_ohlcv(df)

    ema20 = _ema(df["close"], ema_fast_len)
    ema50 = _ema(df["close"], ema_mid_len)
    ema100 = _ema(df["close"], ema_slow_len)
    ema200 = _ema(df["close"], ema_base_len)
    psar, psar_below = _parabolic_sar(
        df["high"], df["low"], start=psar_start,
        increment=psar_increment, maximum=psar_maximum,
    )
    vol_sma = _sma(df["volume"], vol_sma_len)
    # Volume ratio, guard div/0
    vol_ratio = (df["volume"] / vol_sma.replace(0.0, np.nan)).fillna(1.0)
    atr = _atr(df["high"], df["low"], df["close"], atr_len)

    close = df["close"]
    bull_trend = (close > ema200) & (ema20 > ema50) & (ema50 > ema100)
    bear_trend = (close < ema200) & (ema20 < ema50) & (ema50 < ema100)

    # Crossovers — close-based, strict (not equal)
    prev_close = close.shift(1)
    cross_up_ema20 = (close > ema20) & (prev_close <= ema20.shift(1))
    cross_dn_ema20 = (close < ema20) & (prev_close >= ema20.shift(1))
    cross_up_50 = (close > ema50) & (prev_close <= ema50.shift(1))
    cross_dn_50 = (close < ema50) & (prev_close >= ema50.shift(1))

    vol_rising = df["volume"] > vol_sma * vol_entry_mult
    vol_strong = df["volume"] > vol_sma * vol_strong_mult
    # 2-bar volume decline (for volume_fade exit)
    vol_declining = (df["volume"] < df["volume"].shift(1)) & (
        df["volume"].shift(1) < df["volume"].shift(2)
    )

    # Entry conditions per bar
    psar_below_mask = psar < close
    psar_above_mask = psar > close

    long_entry = bull_trend & cross_up_ema20 & vol_rising & psar_below_mask
    short_entry = bear_trend & cross_dn_ema20 & vol_rising & psar_above_mask

    strong_long = long_entry & vol_strong & cross_up_50
    strong_short = short_entry & vol_strong & cross_dn_50

    # Walk bars sequentially to track virtual position and emit entries + exits
    rows: list[SignalRow] = []
    pos_dir = 0        # +1 long, -1 short, 0 flat
    pos_bars = 0
    pos_entry_ts: pd.Timestamp | None = None

    # Warm-up: skip bars before the longest EMA has enough data
    warmup = max(ema_base_len, atr_len, vol_sma_len) + 2

    def _mk_row(
        ts: pd.Timestamp,
        i: int,
        name: str,
        *,
        bars_since_entry: int | None = None,
        exit_reason: str | None = None,
    ) -> SignalRow:
        return SignalRow(
            ts=ts,
            symbol=symbol,
            timeframe=timeframe,
            signal=name,
            price=float(close.iloc[i]),
            ema_fast=float(ema20.iloc[i]),
            ema_mid=float(ema50.iloc[i]),
            ema_slow=float(ema100.iloc[i]),
            ema_base=float(ema200.iloc[i]),
            atr=float(atr.iloc[i]) if not pd.isna(atr.iloc[i]) else 0.0,
            psar=float(psar.iloc[i]) if not pd.isna(psar.iloc[i]) else None,
            psar_below=bool(psar_below_mask.iloc[i]),
            volume=float(df["volume"].iloc[i]),
            volume_sma=float(vol_sma.iloc[i]) if not pd.isna(vol_sma.iloc[i]) else None,
            volume_ratio=float(vol_ratio.iloc[i]),
            bull_trend=bool(bull_trend.iloc[i]),
            bear_trend=bool(bear_trend.iloc[i]),
            bars_since_entry=bars_since_entry,
            exit_reason=exit_reason,
        )

    for i in range(len(df)):
        if i < warmup:
            continue
        ts = df.index[i]

        # ── EXIT check first (if in position) ───────────────────────
        if pos_dir != 0:
            pos_bars += 1
            reason: str | None = None
            if pos_dir == 1:
                if psar_above_mask.iloc[i]:
                    reason = "psar_flip"
                elif close.iloc[i] < ema20.iloc[i]:
                    reason = "trend_break"
                elif pos_bars >= exit_max_bars:
                    reason = "time_max"
                elif pos_bars >= exit_min_bars and vol_declining.iloc[i]:
                    reason = "volume_fade"
            else:  # pos_dir == -1
                if psar_below_mask.iloc[i]:
                    reason = "psar_flip"
                elif close.iloc[i] > ema20.iloc[i]:
                    reason = "trend_break"
                elif pos_bars >= exit_max_bars:
                    reason = "time_max"
                elif pos_bars >= exit_min_bars and vol_declining.iloc[i]:
                    reason = "volume_fade"

            if reason is not None:
                exit_name = "exit_long" if pos_dir == 1 else "exit_short"
                rows.append(
                    _mk_row(
                        ts, i, exit_name,
                        bars_since_entry=pos_bars,
                        exit_reason=reason,
                    )
                )
                pos_dir = 0
                pos_bars = 0
                pos_entry_ts = None
                # Continue to allow an entry on the same bar (rare but
                # faithful to Pine — once flat we can re-enter)
                # However Pine guards with `if pos_dir == 0` so that's OK.

        # ── ENTRY check (only if flat) ──────────────────────────────
        if pos_dir == 0:
            if strong_long.iloc[i]:
                rows.append(_mk_row(ts, i, "strong_long", bars_since_entry=0))
                pos_dir = 1
                pos_bars = 0
                pos_entry_ts = ts
            elif strong_short.iloc[i]:
                rows.append(_mk_row(ts, i, "strong_short", bars_since_entry=0))
                pos_dir = -1
                pos_bars = 0
                pos_entry_ts = ts
            elif emit_weak and long_entry.iloc[i] and not strong_long.iloc[i]:
                rows.append(_mk_row(ts, i, "long", bars_since_entry=0))
                pos_dir = 1
                pos_bars = 0
                pos_entry_ts = ts
            elif emit_weak and short_entry.iloc[i] and not strong_short.iloc[i]:
                rows.append(_mk_row(ts, i, "short", bars_since_entry=0))
                pos_dir = -1
                pos_bars = 0
                pos_entry_ts = ts

    return rows


# ══════════════════════════════════════════════════════════════════════
# Normalisation
# ══════════════════════════════════════════════════════════════════════
def _normalise_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    mapped = {c: c.lower() for c in df.columns}
    out = df.rename(columns=mapped).copy()
    required = {"open", "high", "low", "close"}
    missing = required - set(out.columns)
    if missing:
        raise ValueError(f"OHLCV missing columns: {sorted(missing)}")
    if "volume" not in out.columns:
        out["volume"] = 1.0
    # Coerce volume to numeric to prevent string arithmetic surprises
    out["volume"] = pd.to_numeric(out["volume"], errors="coerce").fillna(0.0)
    if not isinstance(out.index, pd.DatetimeIndex):
        raise ValueError("DataFrame index must be a DatetimeIndex")
    return out.sort_index()


# ══════════════════════════════════════════════════════════════════════
# Registry
# ══════════════════════════════════════════════════════════════════════
EngineFn = Callable[..., list[SignalRow]]

_ENGINES: dict[str, EngineFn] = {
    "smartgold": smartgold_engine,
    "psar_ema_vol": psar_ema_vol_engine,
}


def register_engine(name: str, fn: EngineFn) -> None:
    _ENGINES[name] = fn


def get_engine(name: str = "psar_ema_vol") -> EngineFn:
    """Return the engine callable by name.

    Default is the new PSAR+EMA+Volume engine. Pass ``"smartgold"``
    explicitly to use the legacy SMC engine.
    """
    if name not in _ENGINES:
        raise KeyError(
            f"unknown signal engine '{name}'; have {sorted(_ENGINES)}"
        )
    return _ENGINES[name]


__all__ = [
    "SignalRow",
    "smartgold_engine",
    "psar_ema_vol_engine",
    "register_engine",
    "get_engine",
]
