# Backtest Harness

Historical evaluation of SmartGold signals with different LLM prompt
variants, so you can answer "does this prompt actually help?" with a
measurable P&L — before deploying it live.

## What it does

1. Load OHLCV for a symbol (CSV or yfinance).
2. Replay the SmartGold signal engine (`app.backtest.signals`) — a pandas
   port of the Pine v2 `strong_long` / `strong_short` conditions.
3. For each signal, ask each named **prompt variant** whether to accept.
4. Simulate the trade forward bar-by-bar until TP, SL, or a timeout.
5. Aggregate win-rate, expectancy (in R), profit factor, Sharpe, and
   max drawdown per variant.

## Built-in variants

| Name | Behaviour |
|---|---|
| `baseline` | Accept every signal — reference benchmark |
| `ema_stack` | Accept only if EMA stack agrees with direction — deterministic comparator |
| `llm` | Real LLM decision via `app.engine.decision_engine` with `MIN_CONFIDENCE=0.60` |
| `llm_strict` | Same as `llm` but `min_confidence=0.75` |

You can add your own variants by calling `register_engine()` /
`PromptVariant(...)` — see `prompt_eval.py`.

## CLI

```bash
# CSV source (datetime, open, high, low, close, volume)
python scripts/backtest.py --csv data/XAUUSD_60.csv --tf 60

# yfinance source (needs internet)
python scripts/backtest.py --yf GC=F --period 6mo --interval 1h --tf 60

# Mock mode: no real LLM calls (free)
LLM_MOCK_MODE=true python scripts/backtest.py --yf GC=F --period 1y \
    --interval 1h --variants baseline,ema_stack,llm

# Persist report
python scripts/backtest.py --yf GC=F --period 6mo --interval 1h \
    --out-json reports/backtest_2026-05.json
```

## Output

```text
═══ BACKTEST REPORT ═══
{
  "symbol": "XAUUSD",
  "timeframe": "60",
  "bars": 4320,
  "signals": 142
}

── baseline ──
  accepted=142  rejected=0
  trades=142  wins=63  losses=71  BE=8
  win_rate=0.4701  expectancy_R=0.08  profit_factor=1.12
  total_R=11.4  max_dd_R=9.8  sharpe=0.34

── ema_stack ──
  accepted=118  rejected=24
  ...

── llm_min_0.60 ──
  accepted=77  rejected=65
  ...
```

## Reading the results

- **Win rate** alone is misleading — a 40 % win rate with 1:3 R:R can be
  very profitable. Prioritise **expectancy in R** and **profit factor**.
- **Max drawdown** matters more than raw total R. A variant with higher
  total R but 3× deeper drawdown is not obviously better.
- **Sharpe (trade-level)** compares smoothness. Negative = worse than
  random.
- If LLM variants reject more signals than they keep, that's often fine
  — fewer, higher-quality trades usually have better expectancy.

## Optional: vectorbt cross-check

We ship a `metrics_via_vectorbt()` helper that cross-validates the
trade-level win rate + total R using vectorbt's vectorised math. It's
disabled by default to keep the core dependency tree small — enable with:

```bash
pip install vectorbt
```

…then the CLI will print both native and vectorbt numbers at the bottom
of each variant block. They should agree to 4+ decimal places — any
mismatch is a bug in the simulator.
