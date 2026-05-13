#!/usr/bin/env python3
"""Comprehensive offline smoke test for the AI Bridge.

This script validates the core pipeline components WITHOUT needing a running
server, network access, or external API keys. It exercises:

  1. Settings loading and validation
  2. Signal engine (smartgold + psar_ema_vol) on synthetic data
  3. Simulator (both exit modes: fixed_rr and indicator)
  4. Guard chain (all guards: pass, block, reduce scenarios)
  5. Schema validation (TradingViewAlert, BridgeResponse)
  6. Backtest prompt_eval integration (baseline variant on synthetic data)

Usage:
    python scripts/smoke_test.py            # run all tests
    python scripts/smoke_test.py --verbose  # with detailed output

Exit codes:
    0 — all tests passed
    1 — one or more tests failed
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

# Ensure project root on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

VERBOSE = False
_results: list[tuple[str, bool, str]] = []


def _log(msg: str) -> None:
    if VERBOSE:
        print(f"  {msg}")


def _pass(name: str, detail: str = "") -> None:
    _results.append((name, True, detail))
    print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))


def _fail(name: str, detail: str = "") -> None:
    _results.append((name, False, detail))
    print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


# ═══════════════════════════════════════════════════════════════════════
# Synthetic OHLCV generator
# ═══════════════════════════════════════════════════════════════════════
def _make_synthetic_ohlcv(bars: int = 500, trend: str = "up") -> pd.DataFrame:
    """Generate synthetic OHLCV data with a clear trend for testing."""
    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=bars, freq="h", tz="UTC")

    if trend == "up":
        drift = 0.02
    elif trend == "down":
        drift = -0.02
    else:
        drift = 0.0

    close = np.zeros(bars)
    close[0] = 2000.0
    for i in range(1, bars):
        close[i] = close[i - 1] * (1 + drift / 100 + np.random.randn() * 0.003)

    high = close * (1 + np.abs(np.random.randn(bars)) * 0.001 + 0.0005)
    low = close * (1 - np.abs(np.random.randn(bars)) * 0.001 - 0.0005)
    open_ = (close + np.random.randn(bars) * 0.5)
    volume = np.random.randint(100, 10000, size=bars).astype(float)

    df = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=dates,
    )
    return df


# ═══════════════════════════════════════════════════════════════════════
# Test: Settings
# ═══════════════════════════════════════════════════════════════════════
def test_settings() -> None:
    """Validate settings load correctly with defaults."""
    import os
    os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
    os.environ.setdefault("LLM_MOCK_MODE", "true")

    from app.config.settings import Settings, get_settings

    get_settings.cache_clear()
    s = get_settings()

    assert isinstance(s, Settings), "get_settings() must return Settings"
    assert 0.0 <= s.min_confidence <= 1.0, f"min_confidence out of range: {s.min_confidence}"
    assert s.guard_max_daily_trades > 0, "guard_max_daily_trades must be positive"
    assert s.guard_max_daily_drawdown_r < 0, "guard_max_daily_drawdown_r must be negative"
    assert s.guard_max_spread_points > 0, "guard_max_spread_points must be positive"
    _pass("settings_load", f"env={s.app_env} guards_configured=True")


# ═══════════════════════════════════════════════════════════════════════
# Test: Signal engines
# ═══════════════════════════════════════════════════════════════════════
def test_smartgold_engine() -> None:
    """SmartGold engine produces signals on synthetic uptrend data."""
    from app.backtest.signals import smartgold_engine

    df = _make_synthetic_ohlcv(bars=500, trend="up")
    signals = smartgold_engine(df, symbol="XAUUSD", timeframe="60")

    # May or may not produce signals on synthetic data (depends on conditions)
    assert isinstance(signals, list), "engine must return a list"
    _pass("smartgold_engine", f"{len(signals)} signals from 500 bars")


def test_psar_ema_vol_engine() -> None:
    """PSAR+EMA+Vol engine runs without error on synthetic data."""
    from app.backtest.signals import psar_ema_vol_engine

    df = _make_synthetic_ohlcv(bars=500, trend="up")
    signals = psar_ema_vol_engine(df, symbol="XAUUSD", timeframe="60")

    assert isinstance(signals, list), "engine must return a list"
    for sig in signals[:5]:
        assert sig.signal in ("strong_long", "strong_short")
        assert sig.atr > 0
    _pass("psar_ema_vol_engine", f"{len(signals)} signals from 500 bars")


def test_engine_registry() -> None:
    """get_engine dispatches correctly."""
    from app.backtest.signals import get_engine

    sg = get_engine("smartgold")
    psar = get_engine("psar_ema_vol")
    assert callable(sg)
    assert callable(psar)

    try:
        get_engine("nonexistent")
        _fail("engine_registry", "should have raised KeyError")
        return
    except KeyError:
        pass
    _pass("engine_registry", "dispatch works for smartgold, psar_ema_vol")


# ═══════════════════════════════════════════════════════════════════════
# Test: Simulator
# ═══════════════════════════════════════════════════════════════════════
def test_simulator_fixed_rr() -> None:
    """Simulator produces trades in fixed_rr mode."""
    from app.backtest.signals import SignalRow, smartgold_engine
    from app.backtest.simulator import simulate_trades, summarise

    df = _make_synthetic_ohlcv(bars=500, trend="up")
    signals = smartgold_engine(df, symbol="XAUUSD", timeframe="60")

    # If no signals from smartgold, create a synthetic one
    if not signals:
        signals = [
            SignalRow(
                ts=df.index[250],
                symbol="XAUUSD",
                timeframe="60",
                signal="strong_long",
                price=float(df["close"].iloc[250]),
                ms_state="bullish",
                rsi=50.0,
                atr=3.0,
                money_flow=65.0,
                ema_fast=float(df["close"].iloc[250]),
                ema_slow=float(df["close"].iloc[250]) - 1,
                ema_base=float(df["close"].iloc[250]) - 5,
            )
        ]

    trades = simulate_trades(df, signals, exit_mode="fixed_rr")
    assert isinstance(trades, list)
    metrics = summarise(trades)
    assert "win_rate" in metrics
    assert "total_r" in metrics
    _pass("simulator_fixed_rr", f"{len(trades)} trades, total_R={metrics['total_r']}")


def test_simulator_indicator_exit() -> None:
    """Simulator supports indicator exit mode."""
    from app.backtest.signals import SignalRow
    from app.backtest.simulator import simulate_trades

    df = _make_synthetic_ohlcv(bars=300, trend="up")
    # Create a synthetic signal
    sig = SignalRow(
        ts=df.index[100],
        symbol="XAUUSD",
        timeframe="60",
        signal="strong_long",
        price=float(df["close"].iloc[100]),
        ms_state="bullish",
        rsi=50.0,
        atr=3.0,
        money_flow=65.0,
        ema_fast=float(df["close"].iloc[100]),
        ema_slow=float(df["close"].iloc[100]) - 1,
        ema_base=float(df["close"].iloc[100]) - 5,
    )

    trades = simulate_trades(df, [sig], exit_mode="indicator")
    assert isinstance(trades, list)
    assert len(trades) >= 1, "should produce at least 1 trade"
    # Indicator exits should produce reason in (sl, tp, timeout, indicator)
    assert trades[0].reason in ("sl", "tp", "timeout", "indicator")
    _pass("simulator_indicator_exit", f"exit_reason={trades[0].reason}")


# ═══════════════════════════════════════════════════════════════════════
# Test: Guards
# ═══════════════════════════════════════════════════════════════════════
def test_guard_max_daily_trades() -> None:
    """MaxDailyTradesGuard blocks when limit reached."""
    from app.guards.guards import MaxDailyTradesGuard
    from app.guards.chain import Verdict

    guard = MaxDailyTradesGuard(max_trades=3)

    # Under limit — pass
    v = guard.evaluate({"daily_trades": 2})
    assert v.verdict == Verdict.PASS

    # At limit — block
    v = guard.evaluate({"daily_trades": 3})
    assert v.verdict == Verdict.BLOCK
    _pass("guard_max_daily_trades", "pass@2, block@3")


def test_guard_drawdown() -> None:
    """DrawdownGuard reduces and blocks at thresholds."""
    from app.guards.guards import DrawdownGuard
    from app.guards.chain import Verdict

    guard = DrawdownGuard(max_dd_r=-3.0, reduce_threshold_r=-1.5)

    # Normal — pass
    v = guard.evaluate({"daily_pnl_r": 0.0})
    assert v.verdict == Verdict.PASS

    # Below caution — reduce
    v = guard.evaluate({"daily_pnl_r": -2.0})
    assert v.verdict == Verdict.REDUCE
    assert v.reduce_factor == 0.5

    # Below max — block
    v = guard.evaluate({"daily_pnl_r": -3.5})
    assert v.verdict == Verdict.BLOCK
    _pass("guard_drawdown", "pass@0, reduce@-2, block@-3.5")


def test_guard_spread() -> None:
    """SpreadGuard blocks on wide spreads, passes on None."""
    from app.guards.guards import SpreadGuard
    from app.guards.chain import Verdict

    guard = SpreadGuard(max_spread_points=50.0)

    # No data — fail open
    v = guard.evaluate({})
    assert v.verdict == Verdict.PASS

    # Normal spread — pass
    v = guard.evaluate({"spread_points": 20.0})
    assert v.verdict == Verdict.PASS

    # Wide spread — block
    v = guard.evaluate({"spread_points": 100.0})
    assert v.verdict == Verdict.BLOCK
    _pass("guard_spread", "pass@None, pass@20, block@100")


def test_guard_news_blackout() -> None:
    """NewsBlackoutGuard reduces during news window."""
    from app.guards.guards import NewsBlackoutGuard
    from app.guards.chain import Verdict

    guard = NewsBlackoutGuard(enabled=True, reduce_factor=0.25)

    v = guard.evaluate({"news_window": False})
    assert v.verdict == Verdict.PASS

    v = guard.evaluate({"news_window": True})
    assert v.verdict == Verdict.REDUCE
    assert v.reduce_factor == 0.25

    # Disabled — always pass
    guard_off = NewsBlackoutGuard(enabled=False)
    v = guard_off.evaluate({"news_window": True})
    assert v.verdict == Verdict.PASS
    _pass("guard_news_blackout", "pass@no_news, reduce@news, pass@disabled")


def test_guard_chain() -> None:
    """GuardChain short-circuits on BLOCK, accumulates REDUCE."""
    from app.guards.chain import GuardChain, Verdict
    from app.guards.guards import DrawdownGuard, MaxDailyTradesGuard, SpreadGuard

    chain = GuardChain()
    chain.add(MaxDailyTradesGuard(max_trades=5))
    chain.add(DrawdownGuard(max_dd_r=-3.0))
    chain.add(SpreadGuard(max_spread_points=50.0))

    # All pass
    verdict, results = chain.run({"daily_trades": 1, "daily_pnl_r": 0.0, "spread_points": 10.0})
    assert verdict == Verdict.PASS
    assert len(results) == 3

    # Block on trades
    verdict, results = chain.run({"daily_trades": 5, "daily_pnl_r": 0.0, "spread_points": 10.0})
    assert verdict == Verdict.BLOCK
    assert len(results) == 1  # short-circuits

    # Reduce on drawdown
    verdict, results = chain.run({"daily_trades": 1, "daily_pnl_r": -2.0, "spread_points": 10.0})
    assert verdict == Verdict.REDUCE
    _pass("guard_chain", "pass/block/reduce scenarios correct")


# ═══════════════════════════════════════════════════════════════════════
# Test: Schema validation
# ═══════════════════════════════════════════════════════════════════════
def test_schema_validation() -> None:
    """TradingViewAlert validates correctly."""
    from app.models.schemas import TradingViewAlert

    # Valid payload
    payload = {
        "secret": "test",
        "symbol": "XAUUSD",
        "timeframe": "60",
        "signal": "strong_long",
        "price": 3245.67,
        "time": "2026-05-12T18:00:00Z",
        "ms_state": "bullish",
        "rsi": 52.0,
        "atr": 8.40,
        "money_flow": 65.0,
        "ema_fast": 3243.10,
        "ema_slow": 3220.40,
        "ema_base": 3175.70,
    }
    alert = TradingViewAlert.model_validate(payload)
    assert alert.symbol == "XAUUSD"
    assert alert.signal == "strong_long"
    assert alert.price == 3245.67
    _pass("schema_validation", "TradingViewAlert parses correctly")


# ═══════════════════════════════════════════════════════════════════════
# Test: Backtest integration (prompt_eval)
# ═══════════════════════════════════════════════════════════════════════
def test_backtest_integration() -> None:
    """run_backtest works end-to-end with baseline variant on synthetic data."""
    import os
    os.environ["LLM_MOCK_MODE"] = "true"
    os.environ["WEBHOOK_SECRET"] = "test"

    from app.config.settings import get_settings
    get_settings.cache_clear()

    from app.backtest.prompt_eval import baseline_accept_all, run_backtest

    df = _make_synthetic_ohlcv(bars=500, trend="up")
    report = run_backtest(
        df,
        symbol="XAUUSD",
        timeframe="60",
        variants=[baseline_accept_all()],
        engine_name="smartgold",
        exit_mode="fixed_rr",
    )

    assert "input" in report
    assert "variants" in report
    assert report["input"]["engine"] == "smartgold"
    assert report["input"]["exit_mode"] == "fixed_rr"
    assert "baseline_accept_all" in report["variants"]
    _pass(
        "backtest_integration",
        f"engine=smartgold signals={report['input']['signals']}",
    )


def test_backtest_psar_engine() -> None:
    """run_backtest works with psar_ema_vol engine."""
    from app.backtest.prompt_eval import baseline_accept_all, run_backtest

    df = _make_synthetic_ohlcv(bars=500, trend="down")
    report = run_backtest(
        df,
        symbol="XAUUSD",
        timeframe="60",
        variants=[baseline_accept_all()],
        engine_name="psar_ema_vol",
        exit_mode="indicator",
    )

    assert report["input"]["engine"] == "psar_ema_vol"
    assert report["input"]["exit_mode"] == "indicator"
    _pass(
        "backtest_psar_engine",
        f"engine=psar_ema_vol signals={report['input']['signals']}",
    )


# ═══════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════
ALL_TESTS = [
    test_settings,
    test_smartgold_engine,
    test_psar_ema_vol_engine,
    test_engine_registry,
    test_simulator_fixed_rr,
    test_simulator_indicator_exit,
    test_guard_max_daily_trades,
    test_guard_drawdown,
    test_guard_spread,
    test_guard_news_blackout,
    test_guard_chain,
    test_schema_validation,
    test_backtest_integration,
    test_backtest_psar_engine,
]


def main() -> int:
    global VERBOSE
    parser = argparse.ArgumentParser(description="AI Bridge comprehensive offline smoke test")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    VERBOSE = args.verbose

    print("\n═══ SMART GOLD AI BRIDGE — OFFLINE SMOKE TEST ═══\n")

    for test_fn in ALL_TESTS:
        try:
            test_fn()
        except Exception as exc:
            _fail(test_fn.__name__, str(exc))
            if VERBOSE:
                traceback.print_exc()

    # Summary
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = sum(1 for _, ok, _ in _results if not ok)
    total = len(_results)

    print(f"\n{'─' * 50}")
    print(f"Results: {passed}/{total} passed, {failed} failed")

    if failed:
        print("\nFailed tests:")
        for name, ok, detail in _results:
            if not ok:
                print(f"  - {name}: {detail}")
        print("\n[SMOKE TEST FAILED]")
        return 1

    print("\n[SMOKE TEST PASSED]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
