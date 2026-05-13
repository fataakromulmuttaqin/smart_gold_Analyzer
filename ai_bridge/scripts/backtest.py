#!/usr/bin/env python3
"""CLI entry-point for the SmartGold backtest harness.

Supports the new PSAR + EMA + Volume strategy (``psar_ema_vol`` engine)
and the legacy SMC strategy (``smartgold`` engine) for A/B comparison.

Usage:
    # New strategy with indicator-based exits (recommended):
    python scripts/backtest.py --yf GC=F --period 1y --interval 1h \\
        --engine psar_ema_vol --exit-mode indicator

    # Compare new vs legacy:
    python scripts/backtest.py --yf GC=F --period 1y --interval 1h \\
        --engine psar_ema_vol --exit-mode indicator \\
        --variants baseline,strong_only,ema_stack

    # With a local CSV (must have datetime, open, high, low, close, volume):
    python scripts/backtest.py --csv data/XAUUSD_60.csv --symbol XAUUSD --tf 60

    # With LLM filter (mock mode to avoid API cost):
    LLM_MOCK_MODE=true python scripts/backtest.py --yf GC=F --period 6mo \\
        --interval 1h --variants baseline,llm

    # Include weak signals (not just strong_*):
    python scripts/backtest.py --yf GC=F --period 1y --interval 1h \\
        --engine psar_ema_vol --emit-weak

The run prints a structured report and optionally writes JSON to disk.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure project root on sys.path when invoked directly
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from app.backtest.prompt_eval import (  # noqa: E402
    baseline_accept_all,
    confidence_threshold_variant,
    run_backtest,
    simple_trend_filter,
    strong_only_filter,
)
from app.config.settings import get_settings  # noqa: E402
from app.utils.logging import configure_logging, logger  # noqa: E402


def load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    # Infer datetime column
    for col in ("datetime", "date", "time", "timestamp"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")
            df = df.dropna(subset=[col]).set_index(col)
            break
    else:
        raise ValueError(
            "CSV missing a datetime column (expected one of: "
            "datetime, date, time, timestamp)"
        )
    df = df[~df.index.duplicated(keep="first")].sort_index()
    return df


def load_yfinance(symbol: str, period: str, interval: str) -> pd.DataFrame:
    try:
        import yfinance as yf  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "yfinance not installed. `pip install yfinance` or use --csv."
        ) from exc
    df = yf.download(symbol, period=period, interval=interval, progress=False)
    if df.empty:
        raise RuntimeError(f"yfinance returned no data for {symbol}")
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df.rename(columns=str.lower)
    return df


def build_variants(names: list[str]):
    mapping = {
        "baseline": baseline_accept_all(),
        "ema_stack": simple_trend_filter(),
        "strong_only": strong_only_filter(),
        "llm": confidence_threshold_variant(min_confidence=0.60, name="llm_min_0.60"),
        "llm_strict": confidence_threshold_variant(
            min_confidence=0.75, name="llm_min_0.75"
        ),
    }
    out = []
    for n in names:
        if n not in mapping:
            raise SystemExit(
                f"unknown variant '{n}'; choose from {sorted(mapping)}"
            )
        out.append(mapping[n])
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="SmartGold PSAR+EMA+Vol Backtest Harness"
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", type=Path, help="CSV file with OHLCV data")
    src.add_argument("--yf", type=str, help="yfinance symbol (e.g. GC=F, XAUUSD=X)")
    parser.add_argument("--period", default="1y", help="yfinance period (default 1y)")
    parser.add_argument("--interval", default="1h", help="yfinance interval (default 1h)")
    parser.add_argument("--symbol", default="XAUUSD", help="Symbol label for signals")
    parser.add_argument("--tf", default="60", help="Timeframe label (e.g. 60, 240, D)")
    parser.add_argument(
        "--engine",
        default="psar_ema_vol",
        choices=["psar_ema_vol", "smartgold"],
        help="Signal engine (default: psar_ema_vol)",
    )
    parser.add_argument(
        "--exit-mode",
        default=None,
        choices=["fixed", "indicator"],
        help="Exit mode (default: indicator for psar_ema_vol, fixed for smartgold)",
    )
    parser.add_argument(
        "--emit-weak",
        action="store_true",
        help="Also emit non-strong entries (long/short in addition to strong_*)",
    )
    parser.add_argument(
        "--variants",
        default="baseline,strong_only,ema_stack",
        help="Comma-separated variant names: baseline, strong_only, ema_stack, llm, llm_strict",
    )
    parser.add_argument("--stop-atr-mult", type=float, default=1.5)
    parser.add_argument("--rr", type=float, default=2.0)
    parser.add_argument("--max-bars", type=int, default=48)
    parser.add_argument("--out-json", type=Path, help="Write report JSON to this path")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    s = get_settings()
    configure_logging(args.log_level)
    logger.info("Backtest starting — engine={} mock_mode={}", args.engine, s.llm_mock_mode)

    # ── Load data ──────────────────────────────────────────────────────
    if args.csv:
        df = load_csv(args.csv)
    else:
        df = load_yfinance(args.yf, args.period, args.interval)
    logger.info("Loaded {} bars from {}", len(df), args.csv or args.yf)

    # ── Run backtest ───────────────────────────────────────────────────
    variant_objs = build_variants(args.variants.split(","))
    engine_kwargs = {}
    if args.engine == "psar_ema_vol":
        engine_kwargs["emit_weak"] = args.emit_weak

    report = run_backtest(
        df,
        symbol=args.symbol,
        timeframe=args.tf,
        variants=variant_objs,
        engine_name=args.engine,
        exit_mode=args.exit_mode,
        stop_atr_mult=args.stop_atr_mult,
        rr=args.rr,
        max_bars=args.max_bars,
        engine_kwargs=engine_kwargs,
        settings=s,
    )

    # ── Print report ───────────────────────────────────────────────────
    print("\n" + "═" * 60)
    print("  SMARTGOLD BACKTEST REPORT")
    print("═" * 60)
    inp = report["input"]
    print(f"  Symbol: {inp['symbol']}  TF: {inp['timeframe']}  Bars: {inp['bars']}")
    print(f"  Engine: {inp['engine']}  Exit mode: {inp['exit_mode']}")
    print(f"  Signals generated: {inp['signals']}")
    print(f"  Stop: {inp['stop_atr_mult']}×ATR  RR: {inp['rr']}  Max bars: {inp['max_bars']}")
    print("─" * 60)

    for name, v in report["variants"].items():
        m = v["metrics"]
        print(f"\n  ┌─ Variant: {name}")
        print(f"  │  Accepted: {v['accepted']}  Rejected: {v['rejected']}")
        print(f"  │  Trades: {m['trades']}  W/L/BE: {m['wins']}/{m['losses']}/{m['breakevens']}")
        print(f"  │  Win Rate:       {m['win_rate']}")
        print(f"  │  Expectancy (R): {m['expectancy_r']}")
        print(f"  │  Profit Factor:  {m['profit_factor']}")
        print(f"  │  Total R:        {m['total_r']}")
        print(f"  │  Max DD (R):     {m['max_drawdown_r']}")
        print(f"  │  Sharpe (R):     {m['sharpe_r']}")
        print(f"  │  Avg Bars Held:  {m.get('avg_bars_held', 'N/A')}")
        if v.get("sample"):
            print(f"  │  Sample (first 3):")
            for t in v["sample"][:3]:
                print(
                    f"  │    {t['entry_ts'][:16]} {t['side']:5} → "
                    f"{t['outcome']:4} ({t['reason']}) "
                    f"R={t['pnl_r']:+.2f} bars={t['bars_held']}"
                )
        print(f"  └{'─' * 58}")

    print("\n" + "═" * 60)

    # ── Save JSON ──────────────────────────────────────────────────────
    if args.out_json:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(json.dumps(report, indent=2, default=str))
        logger.info("Report written to {}", args.out_json)
        print(f"\n  JSON report: {args.out_json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
