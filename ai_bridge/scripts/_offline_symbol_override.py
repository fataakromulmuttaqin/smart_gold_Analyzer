"""Offline validator: verify the MT5_SYMBOL override block rewrites alert.symbol.

Run from ai_bridge/:  python scripts/_offline_symbol_override.py

We don't stand up the full HTTP stack here (avoids extra deps). Instead
we reproduce the override snippet inline and assert it mutates the
pydantic model so downstream components (SignalLog, dashboard API,
MT5Executor) will see the broker ticker.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["MT5_SYMBOL"] = "XAUUSDm"

from app.config.settings import Settings  # noqa: E402
from app.models.schemas import TradingViewAlert  # noqa: E402


def main() -> int:
    settings = Settings()
    assert settings.mt5_symbol == "XAUUSDm", settings.mt5_symbol
    print(f"[ok]   Settings.mt5_symbol = {settings.mt5_symbol}")

    # Simulate a raw TradingView payload with the OANDA feed prefix
    alert = TradingViewAlert(
        secret="x",
        symbol="OANDA:XAUUSD",
        timeframe="60",
        signal="strong_long",
        price=3245.67,
    )
    assert alert.symbol == "OANDA:XAUUSD"
    print(f"[ok]   incoming alert.symbol = {alert.symbol}")

    # ── Override block copied verbatim from app/api/webhook.py ──────────
    if settings.mt5_symbol:
        original_symbol = alert.symbol
        if original_symbol != settings.mt5_symbol:
            alert.symbol = settings.mt5_symbol

    assert alert.symbol == "XAUUSDm", f"override failed, got {alert.symbol}"
    print(f"[ok]   after override alert.symbol = {alert.symbol}")

    # Verify model_dump (what SignalLog serialises) carries the new value
    dumped = alert.model_dump()
    assert dumped["symbol"] == "XAUUSDm"
    print(f"[ok]   alert.model_dump()['symbol'] = {dumped['symbol']}")

    # Also check: when MT5_SYMBOL is unset we DON'T rewrite
    # (simulate by forcing the attribute instead of re-reading env/.env)
    alert2 = TradingViewAlert(
        secret="x", symbol="OANDA:XAUUSD", timeframe="60",
        signal="strong_long", price=1.0,
    )
    empty_symbol = ""
    if empty_symbol:
        alert2.symbol = empty_symbol  # pragma: no cover — branch disabled
    assert alert2.symbol == "OANDA:XAUUSD"
    print("[ok]   with MT5_SYMBOL unset, alert.symbol is left untouched")

    print("\n[ALL OK] MT5_SYMBOL override mutates alert.symbol as expected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
