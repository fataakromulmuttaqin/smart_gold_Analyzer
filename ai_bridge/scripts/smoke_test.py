#!/usr/bin/env python3
"""Comprehensive smoke test for the SmartGold AI Bridge.

Tests three layers WITHOUT requiring external services (no LLM API, no
broker, no TradingView, no internet):

  1. **Schema validation** — ensures the new PSAR+EMA+Volume payload
     parses correctly via TradingViewAlert, including exit signals.
  2. **Signal engine** — runs psar_ema_vol on synthetic OHLCV data and
     verifies entries + exits are emitted with correct field population.
  3. **End-to-end webhook** — spins up the FastAPI app in-process using
     TestClient and fires sample payloads, asserting HTTP codes and
     response shape.

Usage (from ai_bridge/ directory):
    # Offline mode (recommended for CI / first-time validation):
    python scripts/smoke_test.py

    # Against a running server (same as old behaviour):
    python scripts/smoke_test.py --live --url http://127.0.0.1:8080 --secret $WEBHOOK_SECRET

Exit code 0 = all checks passed, 1 = failure.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Project root on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ══════════════════════════════════════════════════════════════════════════
# Test 1: Schema validation (offline, no server)
# ══════════════════════════════════════════════════════════════════════════
def test_schema_validation() -> bool:
    """Parse sample payloads against TradingViewAlert pydantic model."""
    from app.models.schemas import TradingViewAlert

    # New strategy entry payload
    entry_payload = {
        "secret": "test123",
        "symbol": "XAUUSD",
        "timeframe": "60",
        "signal": "strong_long",
        "price": 2345.67,
        "time": "2026-05-12T18:00:00Z",
        "ema_fast": 2344.10,
        "ema_mid": 2338.80,
        "ema_slow": 2330.40,
        "ema_base": 2290.70,
        "psar": 2339.55,
        "psar_below": True,
        "atr": 3.21,
        "volume": 1850.0,
        "volume_sma": 980.0,
        "volume_ratio": 1.888,
        "bull_trend": True,
        "bear_trend": False,
        "bars_since_entry": 0,
        "exit_reason": "",
    }

    # New strategy exit payload
    exit_payload = {
        "secret": "test123",
        "symbol": "XAUUSD",
        "timeframe": "60",
        "signal": "exit_long",
        "price": 2349.10,
        "time": "2026-05-12T22:00:00Z",
        "ema_fast": 2347.50,
        "ema_mid": 2340.20,
        "ema_slow": 2331.10,
        "ema_base": 2291.00,
        "psar": 2350.80,
        "psar_below": False,
        "atr": 3.45,
        "volume": 720.0,
        "volume_sma": 960.0,
        "volume_ratio": 0.75,
        "bull_trend": True,
        "bear_trend": False,
        "bars_since_entry": 5,
        "exit_reason": "psar_flip",
    }

    # Legacy v1 payload (backward compat)
    legacy_payload = {
        "secret": "test123",
        "symbol": "XAUUSD",
        "timeframe": "60",
        "signal": "bull_choch",
        "price": 2340.0,
        "ms_state": "bullish",
        "rsi": 52.3,
        "atr": 3.0,
        "money_flow": 67.5,
        "ema_fast": 2340.5,
        "ema_slow": 2332.1,
        "ema_base": 2290.7,
    }

    # Extra field payload (extra="allow" test)
    extra_payload = {
        "secret": "test123",
        "symbol": "XAUUSD",
        "timeframe": "60",
        "signal": "long",
        "price": 2350.0,
        "custom_field_xyz": 42.0,  # unknown field — should NOT fail
    }

    errors = []
    for name, payload in [
        ("entry", entry_payload),
        ("exit", exit_payload),
        ("legacy", legacy_payload),
        ("extra_fields", extra_payload),
    ]:
        try:
            alert = TradingViewAlert.model_validate(payload)
            assert alert.symbol == "XAUUSD", f"{name}: symbol mismatch"
            assert alert.price > 0, f"{name}: price must be positive"
        except Exception as e:
            errors.append(f"  {name}: {e}")

    # Test invalid signal name → should fail validation
    bad_payload = {**entry_payload, "signal": "invalid_signal_xyz"}
    try:
        TradingViewAlert.model_validate(bad_payload)
        errors.append("  bad_signal: expected validation error but got none")
    except Exception:
        pass  # Expected

    # Test missing required field
    incomplete = {"secret": "x", "symbol": "X"}
    try:
        TradingViewAlert.model_validate(incomplete)
        errors.append("  incomplete: expected validation error but got none")
    except Exception:
        pass  # Expected

    if errors:
        print("[FAIL] Schema validation:")
        for e in errors:
            print(e)
        return False
    print("[OK  ] Schema validation: 6 cases passed (entry, exit, legacy, extra, bad_signal, incomplete)")
    return True


# ══════════════════════════════════════════════════════════════════════════
# Test 2: Signal engine on synthetic data
# ══════════════════════════════════════════════════════════════════════════
def test_signal_engine() -> bool:
    """Generate synthetic gold-like OHLCV and run psar_ema_vol engine."""
    import numpy as np
    import pandas as pd

    from app.backtest.signals import psar_ema_vol_engine

    np.random.seed(42)
    n_bars = 1000  # ~41 days of H1 data

    # Simulate trending gold price with mean-reversion + momentum
    prices = np.zeros(n_bars)
    prices[0] = 2300.0
    trend = 0.0
    for i in range(1, n_bars):
        # Regime change every ~200 bars
        if i % 200 == 0:
            trend = np.random.choice([-0.3, 0.0, 0.3])
        noise = np.random.normal(0, 2.0)
        prices[i] = prices[i - 1] + trend + noise

    # Build OHLCV
    opens = prices + np.random.uniform(-1, 1, n_bars)
    highs = np.maximum(prices, opens) + np.abs(np.random.normal(0, 1.5, n_bars))
    lows = np.minimum(prices, opens) - np.abs(np.random.normal(0, 1.5, n_bars))
    volumes = np.random.lognormal(mean=6.5, sigma=0.8, size=n_bars)

    idx = pd.date_range("2025-01-01", periods=n_bars, freq="h", tz="UTC")
    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": prices, "volume": volumes},
        index=idx,
    )

    # Run engine
    signals = psar_ema_vol_engine(
        df, symbol="XAUUSD", timeframe="60", emit_weak=True
    )

    errors = []

    if len(signals) == 0:
        errors.append("  No signals generated from 1000-bar synthetic data")
    else:
        entries = [s for s in signals if "exit" not in s.signal]
        exits = [s for s in signals if "exit" in s.signal]

        if len(entries) == 0:
            errors.append("  No entry signals generated")
        if len(exits) == 0:
            errors.append("  No exit signals generated")

        # Validate field population on first entry
        if entries:
            e = entries[0]
            if e.psar is None:
                errors.append("  Entry signal missing PSAR value")
            if e.volume is None:
                errors.append("  Entry signal missing volume")
            if e.volume_ratio is None:
                errors.append("  Entry signal missing volume_ratio")
            if e.ema_mid is None:
                errors.append("  Entry signal missing ema_mid (EMA50)")
            if e.bull_trend is None and e.bear_trend is None:
                errors.append("  Entry signal missing trend state")
            if e.bars_since_entry != 0:
                errors.append(f"  Entry bars_since_entry should be 0, got {e.bars_since_entry}")

        # Validate exit signal
        if exits:
            ex = exits[0]
            if ex.exit_reason is None or ex.exit_reason == "":
                errors.append("  Exit signal missing exit_reason")
            if ex.bars_since_entry is None or ex.bars_since_entry < 1:
                errors.append(f"  Exit bars_since_entry should be >= 1, got {ex.bars_since_entry}")

        # Verify entry-exit pairing (no two entries without an exit)
        pos = 0
        pair_errors = 0
        for s in signals:
            if "exit" not in s.signal:
                if pos != 0:
                    pair_errors += 1
                pos = 1 if "long" in s.signal else -1
            else:
                pos = 0
        if pair_errors > 0:
            errors.append(f"  {pair_errors} unpaired entries (entry without preceding exit)")

    if errors:
        print("[FAIL] Signal engine:")
        for e in errors:
            print(e)
        return False

    print(
        f"[OK  ] Signal engine: {len(signals)} signals "
        f"({len(entries)} entries, {len(exits)} exits) from 1000 bars"
    )
    return True


# ══════════════════════════════════════════════════════════════════════════
# Test 3: Simulator with indicator exits
# ══════════════════════════════════════════════════════════════════════════
def test_simulator() -> bool:
    """Run the simulator in 'indicator' exit mode and validate metrics."""
    import numpy as np
    import pandas as pd

    from app.backtest.signals import psar_ema_vol_engine
    from app.backtest.simulator import simulate_trades, summarise

    np.random.seed(123)
    n_bars = 800

    prices = np.cumsum(np.random.normal(0.1, 2.0, n_bars)) + 2300.0
    opens = prices + np.random.uniform(-0.5, 0.5, n_bars)
    highs = np.maximum(prices, opens) + np.abs(np.random.normal(0, 1.2, n_bars))
    lows = np.minimum(prices, opens) - np.abs(np.random.normal(0, 1.2, n_bars))
    volumes = np.random.lognormal(mean=6.5, sigma=0.7, size=n_bars)

    idx = pd.date_range("2025-03-01", periods=n_bars, freq="h", tz="UTC")
    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": prices, "volume": volumes},
        index=idx,
    )

    signals = psar_ema_vol_engine(df, symbol="XAUUSD", timeframe="60", emit_weak=True)
    trades = simulate_trades(df, signals, exit_mode="indicator", stop_atr_mult=1.5)
    metrics = summarise(trades)

    errors = []
    if metrics["trades"] == 0:
        errors.append("  Simulator produced 0 trades from engine signals")
    else:
        # Check for indicator-exit reasons
        reasons = set(t.reason for t in trades)
        indicator_reasons = {"psar_flip", "trend_break", "time_max", "volume_fade"}
        if not reasons.intersection(indicator_reasons):
            errors.append(
                f"  No indicator-exit reasons found (got: {reasons}). "
                "Expected at least one of: psar_flip, trend_break, time_max, volume_fade"
            )
        if metrics["win_rate"] is not None and not (0.0 <= metrics["win_rate"] <= 1.0):
            errors.append(f"  Win rate out of range: {metrics['win_rate']}")

    if errors:
        print("[FAIL] Simulator (indicator mode):")
        for e in errors:
            print(e)
        return False

    print(
        f"[OK  ] Simulator: {metrics['trades']} trades, "
        f"WR={metrics['win_rate']}, PF={metrics['profit_factor']}, "
        f"reasons={sorted(set(t.reason for t in trades))}"
    )
    return True


# ══════════════════════════════════════════════════════════════════════════
# Test 4: End-to-end webhook (in-process, no network)
# ══════════════════════════════════════════════════════════════════════════
def test_webhook_e2e() -> bool:
    """Use FastAPI TestClient to exercise the webhook handler."""
    # Set env before importing app to ensure mock mode
    os.environ.setdefault("WEBHOOK_SECRET", "smoke_test_secret")
    os.environ.setdefault("LLM_MOCK_MODE", "true")
    os.environ.setdefault("MT5_ENABLED", "false")
    os.environ.setdefault("ENABLE_MACRO_CONTEXT", "false")

    # Clear cached settings so env vars take effect
    from app.config.settings import get_settings
    get_settings.cache_clear()

    try:
        from fastapi.testclient import TestClient
    except ImportError:
        print("[SKIP] Webhook E2E: fastapi[testclient] not installed (pip install httpx)")
        return True  # Not a failure — just skip

    from app.main import app
    client = TestClient(app)

    errors = []

    # 4a. GET /health
    resp = client.get("/health")
    if resp.status_code != 200:
        errors.append(f"  /health returned {resp.status_code}")
    else:
        data = resp.json()
        if data.get("llm_mock_mode") is not True:
            errors.append(f"  /health: expected mock_mode=true, got {data.get('llm_mock_mode')}")

    # 4b. POST valid entry
    entry = {
        "secret": "smoke_test_secret",
        "symbol": "XAUUSD",
        "timeframe": "60",
        "signal": "strong_long",
        "price": 2345.67,
        "ema_fast": 2344.0,
        "ema_mid": 2338.0,
        "ema_slow": 2330.0,
        "ema_base": 2290.0,
        "psar": 2339.0,
        "psar_below": True,
        "atr": 3.2,
        "volume": 1800.0,
        "volume_sma": 1000.0,
        "volume_ratio": 1.8,
        "bull_trend": True,
        "bear_trend": False,
        "bars_since_entry": 0,
        "exit_reason": "",
    }
    resp = client.post("/webhook/tradingview", json=entry)
    if resp.status_code != 200:
        errors.append(f"  POST entry: expected 200, got {resp.status_code} — {resp.text[:200]}")
    else:
        body = resp.json()
        required = {"accepted", "alert", "context", "decision"}
        missing = required - set(body.keys())
        if missing:
            errors.append(f"  POST entry: response missing keys {missing}")
        if body.get("decision", {}).get("action") not in ("execute", "skip", "reduce"):
            errors.append(f"  POST entry: unexpected action={body.get('decision', {}).get('action')}")

    # 4c. POST valid exit
    time.sleep(0.1)  # avoid cooldown
    exit_sig = {
        "secret": "smoke_test_secret",
        "symbol": "XAUUSD",
        "timeframe": "60",
        "signal": "exit_long",
        "price": 2349.0,
        "psar": 2350.0,
        "psar_below": False,
        "atr": 3.4,
        "volume": 700.0,
        "volume_sma": 950.0,
        "volume_ratio": 0.74,
        "bull_trend": True,
        "bear_trend": False,
        "bars_since_entry": 5,
        "exit_reason": "psar_flip",
    }
    resp = client.post("/webhook/tradingview", json=exit_sig)
    if resp.status_code not in (200, 429):  # 429 ok if cooldown
        errors.append(f"  POST exit: expected 200/429, got {resp.status_code}")

    # 4d. Bad secret → 401
    bad = {**entry, "secret": "wrong", "signal": "short"}
    resp = client.post("/webhook/tradingview", json=bad)
    if resp.status_code != 401:
        errors.append(f"  POST bad_secret: expected 401, got {resp.status_code}")

    # 4e. Invalid payload → 422
    resp = client.post("/webhook/tradingview", json={"secret": "smoke_test_secret"})
    if resp.status_code != 422:
        errors.append(f"  POST incomplete: expected 422, got {resp.status_code}")

    if errors:
        print("[FAIL] Webhook E2E:")
        for e in errors:
            print(e)
        return False

    print("[OK  ] Webhook E2E: /health, entry, exit, auth, validation all passed")
    return True


# ══════════════════════════════════════════════════════════════════════════
# Test 5: LLM prompt builder (no API call — just string assembly)
# ══════════════════════════════════════════════════════════════════════════
def test_prompt_builder() -> bool:
    """Verify prompts.build_user_prompt produces valid JSON blocks."""
    from app.engine.prompts import SYSTEM_PROMPT, build_user_prompt
    from app.models.schemas import MacroContext, TradingViewAlert

    alert = TradingViewAlert(
        secret="x",
        symbol="XAUUSD",
        timeframe="60",
        signal="strong_long",
        price=2345.0,
        ema_fast=2344.0,
        ema_mid=2338.0,
        ema_slow=2330.0,
        ema_base=2290.0,
        psar=2339.0,
        psar_below=True,
        atr=3.2,
        volume=1800.0,
        volume_sma=1000.0,
        volume_ratio=1.8,
        bull_trend=True,
        bear_trend=False,
        bars_since_entry=0,
        exit_reason="",
    )
    ctx = MacroContext(
        dxy_price=104.5,
        dxy_change_pct=-0.2,
        us10y_yield=4.35,
        us10y_change_bp=-3.0,
        news_headlines=["Fed signals pause"],
    )

    errors = []

    prompt = build_user_prompt(alert, ctx)
    if "SIGNAL:" not in prompt:
        errors.append("  Missing 'SIGNAL:' section")
    if "MACRO CONTEXT:" not in prompt:
        errors.append("  Missing 'MACRO CONTEXT:' section")
    if "ENTRY signal" not in prompt:
        errors.append("  Missing ENTRY signal hint for non-exit signal")
    if "ema20" not in prompt:
        errors.append("  Missing ema20 field in signal block")
    if "psar" not in prompt:
        errors.append("  Missing psar field in signal block")
    if "volume_ratio" not in prompt:
        errors.append("  Missing volume_ratio in signal block")

    # Test exit signal hint
    alert_exit = TradingViewAlert(
        secret="x", symbol="XAUUSD", timeframe="60",
        signal="exit_long", price=2349.0,
        bars_since_entry=5, exit_reason="psar_flip",
    )
    prompt_exit = build_user_prompt(alert_exit, ctx)
    if "EXIT signal" not in prompt_exit:
        errors.append("  Missing EXIT signal hint for exit_long")

    # System prompt should mention PSAR
    if "PSAR" not in SYSTEM_PROMPT:
        errors.append("  System prompt missing PSAR strategy description")

    if errors:
        print("[FAIL] Prompt builder:")
        for e in errors:
            print(e)
        return False

    print("[OK  ] Prompt builder: entry/exit prompts correctly assembled")
    return True


# ══════════════════════════════════════════════════════════════════════════
# Live server test (legacy mode — same as old smoke_test.py)
# ══════════════════════════════════════════════════════════════════════════
def test_live_server(base_url: str, secret: str) -> bool:
    """POST to a running server and validate responses."""
    errors = []

    def _get(url: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, {"detail": e.read().decode("utf-8", errors="replace")}

    def _post(url: str, payload: dict) -> tuple[int, dict]:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"detail": raw}

    base = base_url.rstrip("/")

    # Health
    status, body = _get(f"{base}/health")
    if status != 200:
        errors.append(f"  /health: {status}")
    else:
        print(f"[ok  ] /health: mock={body.get('llm_mock_mode')} model={body.get('model')}")

    # Valid entry
    payload = {
        "secret": secret,
        "symbol": "XAUUSD",
        "timeframe": "60",
        "signal": "strong_long",
        "price": 2345.67,
        "ema_fast": 2344.0,
        "ema_mid": 2338.0,
        "ema_slow": 2330.0,
        "ema_base": 2290.0,
        "psar": 2339.0,
        "psar_below": True,
        "atr": 3.2,
        "volume": 1800.0,
        "volume_sma": 1000.0,
        "volume_ratio": 1.8,
        "bull_trend": True,
        "bear_trend": False,
        "bars_since_entry": 0,
        "exit_reason": "",
    }
    status, body = _post(f"{base}/webhook/tradingview", payload)
    if status == 429:
        print("[warn] Cooldown active — skipping full validation")
    elif status != 200:
        errors.append(f"  POST entry: {status} — {body}")
    else:
        d = body.get("decision", {})
        print(f"[ok  ] Entry: action={d.get('action')} conf={d.get('confidence', 0):.2f}")

    # Bad secret
    payload["secret"] = "wrong"
    status, _ = _post(f"{base}/webhook/tradingview", payload)
    if status not in (401, 429):
        errors.append(f"  Bad secret: expected 401, got {status}")
    else:
        print(f"[ok  ] Auth: bad secret rejected ({status})")

    if errors:
        print("[FAIL] Live server test:")
        for e in errors:
            print(e)
        return False

    print("[OK  ] Live server: all checks passed")
    return True


# ══════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════
def main() -> int:
    parser = argparse.ArgumentParser(description="SmartGold AI Bridge Smoke Test")
    parser.add_argument(
        "--live", action="store_true",
        help="Test against a running server instead of in-process",
    )
    parser.add_argument("--url", default="http://127.0.0.1:8080", help="Server URL (live mode)")
    parser.add_argument("--secret", default=os.environ.get("WEBHOOK_SECRET", ""), help="Webhook secret (live mode)")
    args = parser.parse_args()

    print("═" * 50)
    print("  SmartGold AI Bridge — Smoke Test")
    print("═" * 50)
    print()

    if args.live:
        if not args.secret:
            print("[ERR] --secret or $WEBHOOK_SECRET required for live mode")
            return 2
        results = [test_live_server(args.url, args.secret)]
    else:
        results = [
            test_schema_validation(),
            test_signal_engine(),
            test_simulator(),
            test_prompt_builder(),
            test_webhook_e2e(),
        ]

    print()
    passed = sum(results)
    total = len(results)
    if passed == total:
        print(f"{'═' * 50}")
        print(f"  ALL {total} TESTS PASSED")
        print(f"{'═' * 50}")
        return 0
    else:
        print(f"{'═' * 50}")
        print(f"  {total - passed}/{total} TESTS FAILED")
        print(f"{'═' * 50}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
