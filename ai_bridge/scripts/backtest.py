#!/usr/bin/env python3
"""CLI entry-point for the backtest harness.

Usage:
    # With a local CSV (must have datetime, open, high, low, close, volume):
    python scripts/backtest.py --csv data/XAUUSD_60.csv --symbol XAUUSD --tf 60

    # Via yfinance (needs internet + yfinance installed):
    python scripts/backtest.py --yf GC=F --period 6mo --interval 1h --tf 60

    # Use the PSAR+EMA+Vol engine with indicator-based exits:
    python scripts/backtest.py --yf GC=F --period 6mo --interval 1h \
        --engine psar_ema_vol --exit-mode indicator

    # Compare prompt variants with LLM in mock mode (free):
    LLM_MOCK_MODE=true python scripts/backtest.py --yf GC=F --period 1y \
        --interval 1h --variants baseline,ema_stack,llm

The run prints a JSON report summarising each variant. Use
``--out-json path/to/report.json`` to persist it.
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
)
from app.backtest.signals import get_engine  # noqa: E402
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
    parser = argparse.ArgumentParser(description="SmartGold backtest harness")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", type=Path, help="CSV file with OHLCV data")
    src.add_argument("--yf", type=str, help="yfinance symbol (e.g. GC=F, XAUUSD=X)")
    parser.add_argument("--period", default="6mo", help="yfinance period (default 6mo)")
    parser.add_argument("--interval", default="1h", help="yfinance interval (default 1h)")
    parser.add_argument("--symbol", default="XAUUSD", help="Symbol label for signals")
    parser.add_argument("--tf", default="60", help="Timeframe label (e.g. 60, 240, D)")
    parser.add_argument(
        "--engine",
        default="smartgold",
        help="Signal engine: smartgold (default), psar_ema_vol",
    )
    parser.add_argument(
        "--exit-mode",
        default="fixed_rr",
        choices=["fixed_rr", "indicator"],
        help="Exit mode: fixed_rr (ATR stop + RR TP) or indicator (EMA cross exit)",
    )
    parser.add_argument(
        "--variants",
        default="baseline,ema_stack,llm",
        help="Comma-separated variant names",
    )
    parser.add_argument("--stop-atr-mult", type=float, default=1.5)
    parser.add_argument("--rr", type=float, default=2.0)
    parser.add_argument("--max-bars", type=int, default=48)
    parser.add_argument("--out-json", type=Path, help="Write report to this path")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    s = get_settings()
    configure_logging(args.log_level)
    logger.info("Backtest starting — mock_mode={}", s.llm_mock_mode)

    # Validate engine name early
    try:
        get_engine(args.engine)
    except KeyError as e:
        raise SystemExit(str(e))

    if args.csv:
        df = load_csv(args.csv)
    else:
        df = load_yfinance(args.yf, args.period, args.interval)
    logger.info("Loaded {} bars from {}", len(df), args.csv or args.yf)

    variant_objs = build_variants(args.variants.split(","))
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
        settings=s,
    )

    # Pretty-print the variant comparison
    print("\n═══ BACKTEST REPORT ═══")
    print(json.dumps(report["input"], indent=2))
    print()
    for name, v in report["variants"].items():
        m = v["metrics"]
        print(f"── {name} ──")
        print(f"  accepted={v['accepted']}  rejected={v['rejected']}")
        print(
            f"  trades={m['trades']}  wins={m['wins']}  losses={m['losses']}  "
            f"BE={m['breakevens']}"
        )
        print(
            f"  win_rate={m['win_rate']}  expectancy_R={m['expectancy_r']}  "
            f"profit_factor={m['profit_factor']}"
        )
        print(
            f"  total_R={m['total_r']}  max_dd_R={m['max_drawdown_r']}  "
            f"sharpe={m['sharpe_r']}"
        )
        print()

    if args.out_json:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(json.dumps(report, indent=2, default=str))
        logger.info("Report written to {}", args.out_json)

    return 0


if __name__ == "__main__":
    sys.exit(main())
