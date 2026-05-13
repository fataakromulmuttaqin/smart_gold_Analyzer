"""Standalone sanity check for the backtest simulator, using *only* stdlib.

Because the sandbox where this repo is developed cannot install numpy/pandas
from PyPI, we can't run the real backtest harness here. This script instead
rewrites a handful of helper functions in pure Python and exercises the same
bar-by-bar exit logic against a hand-crafted series, confirming:

  * a tp-winner resolves as "tp" with pnl_r ≈ rr
  * a sl-loser resolves as "sl" with pnl_r ≈ -1
  * a timeout bar resolves as "timeout" with the closing price

CI and VPS runs use the real ``scripts/backtest.py`` with numpy/pandas.
"""
from __future__ import annotations


# ── Tiny drop-ins for the exit helpers (mirror app.backtest.simulator) ──
def _exit_long(bars, entry_idx, entry_price, stop, take, max_bars):
    last = min(len(bars) - 1, entry_idx + max_bars)
    for i in range(entry_idx + 1, last + 1):
        hi = bars[i]["high"]
        lo = bars[i]["low"]
        if lo <= stop:
            return i, stop, "sl"
        if hi >= take:
            return i, take, "tp"
    return last, bars[last]["close"], "timeout"


def _exit_short(bars, entry_idx, entry_price, stop, take, max_bars):
    last = min(len(bars) - 1, entry_idx + max_bars)
    for i in range(entry_idx + 1, last + 1):
        hi = bars[i]["high"]
        lo = bars[i]["low"]
        if hi >= stop:
            return i, stop, "sl"
        if lo <= take:
            return i, take, "tp"
    return last, bars[last]["close"], "timeout"


def main() -> int:
    # Scenario A: long that hits TP at bar 3 (rr=2)
    bars_a = [
        {"high": 100.5, "low": 99.5, "close": 100.0},
        {"high": 101.0, "low": 99.0, "close": 100.5},
        {"high": 102.0, "low": 100.5, "close": 101.5},
        {"high": 103.5, "low": 101.2, "close": 103.2},  # TP hit (take = 103.0)
        {"high": 104.0, "low": 102.0, "close": 103.8},
    ]
    entry = 100.0
    stop_distance = 1.5
    stop = entry - stop_distance          # 98.5
    take = entry + stop_distance * 2.0    # 103.0
    exit_idx, exit_price, reason = _exit_long(bars_a, 0, entry, stop, take, max_bars=10)
    assert reason == "tp", reason
    assert exit_idx == 3
    assert exit_price == take
    pnl_r = (exit_price - entry) / stop_distance
    assert abs(pnl_r - 2.0) < 1e-9, pnl_r
    print("[ok]   long TP scenario: exit_idx=3 reason=tp pnl_r=2.0")

    # Scenario B: long that hits SL first
    bars_b = [
        {"high": 100.5, "low": 99.5, "close": 100.0},
        {"high": 100.6, "low": 98.3, "close": 99.0},   # SL hit (stop = 98.5)
        {"high": 105.0, "low": 99.0, "close": 104.0},
    ]
    exit_idx, exit_price, reason = _exit_long(bars_b, 0, entry, stop, take, max_bars=10)
    assert reason == "sl", reason
    assert exit_idx == 1
    assert exit_price == stop
    pnl_r = (exit_price - entry) / stop_distance
    assert abs(pnl_r - (-1.0)) < 1e-9
    print("[ok]   long SL scenario: exit_idx=1 reason=sl pnl_r=-1.0")

    # Scenario C: timeout — price drifts but neither barrier hit
    bars_c = [
        {"high": 100.2, "low": 99.8, "close": 100.0},
        {"high": 100.5, "low": 99.6, "close": 100.2},
        {"high": 100.8, "low": 99.9, "close": 100.4},
    ]
    exit_idx, exit_price, reason = _exit_long(bars_c, 0, entry, stop, take, max_bars=2)
    assert reason == "timeout", reason
    assert exit_idx == 2
    print(f"[ok]   long TIMEOUT scenario: exit_idx=2 reason=timeout close={exit_price}")

    # Scenario D: short hits TP (price drops to take)
    entry_s = 3250.0
    stop_s = entry_s + 5.0     # SL at 3255
    take_s = entry_s - 10.0    # TP at 3240 (rr=2)
    bars_s = [
        {"high": 3252, "low": 3248, "close": 3250},
        {"high": 3252, "low": 3245, "close": 3247},
        {"high": 3249, "low": 3239, "close": 3240},   # TP hit
    ]
    exit_idx, exit_price, reason = _exit_short(bars_s, 0, entry_s, stop_s, take_s, max_bars=10)
    assert reason == "tp", reason
    assert exit_idx == 2
    assert exit_price == take_s
    pnl_r_s = (entry_s - exit_price) / 5.0  # stop_distance=5
    assert abs(pnl_r_s - 2.0) < 1e-9
    print("[ok]   short TP scenario: exit_idx=2 reason=tp pnl_r=2.0")

    print("\nALL OFFLINE BACKTEST CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
