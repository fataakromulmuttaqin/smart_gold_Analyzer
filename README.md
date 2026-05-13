# SmartGold Analyzer Pro

**TradingView Pine Script indicator + FastAPI decision bridge for XAU/USD (Gold) on H1.**
Combines technical analysis (Parabolic SAR + EMA Ribbon 20/50/100/200 + Volume)
with an LLM (MiniMax) that filters signals against live macro context (DXY, US 10Y yields,
gold news) before they reach Telegram or your broker (MT5).

```
TradingView (Pine v6)  ──▶  Your VPS (FastAPI)  ──▶  MiniMax LLM
                                     │
                                     ├──▶  Safety Guards
                                     ├──▶  Telegram (decision + context)
                                     ├──▶  MT5 Broker (optional)
                                     ├──▶  Web Dashboard (/ui)
                                     └──▶  SQLite (audit log)
```

---

## How it works — the full pipeline

Here's what happens from the moment a candle closes on H1 until a trade is (or isn't) placed:

### 1. Signal generation in Pine Script (TradingView)

The current strategy (`pinescript/smart_gold_analyzer_v2_ai.pine`) is **PSAR + EMA Ribbon + Volume** on H1:

| Entry LONG (all must be true) | Entry SHORT (mirror) |
|-------------------------------|----------------------|
| `close > EMA200` (trend filter) | `close < EMA200` |
| `EMA20 > EMA50 > EMA100` (ribbon aligned) | `EMA20 < EMA50 < EMA100` |
| `close crossup EMA20` (pullback-reclaim trigger) | `close crossdown EMA20` |
| `volume > 1.3 × SMA(vol, 20)` (volume confirmation) | same |
| PSAR below price | PSAR above price |

**`strong_long`/`strong_short`** fire when volume is extra strong (`>1.8×`) AND price reclaims both EMA20 and EMA50 on the same bar.

**Exit signals** (priority order, first match wins):

1. `psar_flip` — PSAR flipped to the opposite side (most reliable)
2. `trend_break` — close crossed back through EMA20 against position
3. `time_max` — held ≥ 6 bars, forced exit
4. `volume_fade` — held ≥ 4 bars AND volume declined 2 bars in a row

### 2. Webhook payload to VPS

Pine `alert()` sends this JSON to your FastAPI server (`ai_bridge/`):

```json
{
  "secret": "WEBHOOK_SECRET",
  "symbol": "XAUUSD", "timeframe": "60",
  "signal": "strong_long", "price": 3245.67,
  "ema_fast": 3243.10, "ema_mid": 3234.80,
  "ema_slow": 3220.40, "ema_base": 3175.70,
  "psar": 3237.55, "psar_below": true,
  "volume": 1850, "volume_sma": 980, "volume_ratio": 1.89,
  "bull_trend": true, "atr": 8.40,
  "bars_since_entry": 0, "exit_reason": ""
}
```

Full schema: [`pinescript/ALERT_PAYLOAD.md`](./pinescript/ALERT_PAYLOAD.md).

### 3. AI Bridge processing pipeline

For each incoming signal, the FastAPI server runs:

```
  1. ┌─────────────────────────────────┐
     │ Schema validation + secret auth  │  → 401 / 422 if bad
     └──────────────┬──────────────────┘
                    ▼
  2. ┌─────────────────────────────────┐
     │ Per-(symbol, signal) cooldown    │  → 429 if within cooldown
     └──────────────┬──────────────────┘
                    ▼
  3. ┌─────────────────────────────────┐
     │ Fetch macro context              │  DXY, US10Y yield, news
     │ (yfinance → TwelveData →         │  Multi-provider fallback
     │  AlphaVantage fallback chain)    │  for VPS-blocked Yahoo
     └──────────────┬──────────────────┘
                    ▼
  4. ┌─────────────────────────────────┐
     │ LLM decision (MiniMax)           │  action: execute|skip|reduce
     │ system prompt aware of           │  + confidence (0.0-1.0)
     │ PSAR+EMA strategy rules          │  + reasoning, risk_notes
     └──────────────┬──────────────────┘
                    ▼
  5. ┌─────────────────────────────────┐
     │ Safety guards (chain)            │  BLOCK / REDUCE / PASS
     │ • MaxDailyTrades                 │
     │ • Drawdown (-3R default)         │
     │ • Spread (>50pt abnormal)        │
     │ • MaxATR (>12 = extreme vol)     │
     │ • NewsBlackout (NFP/FOMC/CPI)    │
     └──────────────┬──────────────────┘
                    ▼
  6. ┌─────────────────────────────────┐
     │ Stop-loss + position sizing       │  HybridATRPsarStop (default)
     │ • Stop distance via policy        │  • min 0.8×ATR, max 2.5×ATR
     │ • Lot size from risk_pct          │  • clipped to broker step
     └──────────────┬──────────────────┘
                    ▼
  7. ┌─────────────────────────────────┐
     │ Broker execute (MT5, optional)   │  Skipped on Linux w/o Wine
     └──────────────┬──────────────────┘
                    ▼
  8. ┌─────────────────────────────────┐
     │ Telegram notify + SQLite log     │  Every signal logged for audit
     └─────────────────────────────────┘

  Background (every 10s):
     ┌─────────────────────────────────┐
     │ Breakeven reconciler             │  Shift SL to entry + 0.1×ATR
     │ (MT5 only)                       │  once a position is +1R in profit
     └─────────────────────────────────┘
```

### 4. Stop-loss policy (RECOMMENDED: `hybrid`)

The default is **HybridATRPsarStop** — uses PSAR as structural reference but clips to `[0.8, 2.5] × ATR`:

```
psar_distance  = entry - psar              (for long)
atr_min_bound  = 0.8 × ATR(14)             (prevents shakeout)
atr_max_bound  = 2.5 × ATR(14)             (caps worst-case risk)

stop_distance  = clamp(psar_distance, atr_min_bound, atr_max_bound)
SL             = entry - stop_distance
```

**Why hybrid?** Pure PSAR can be too tight (nempel-harga at breakout = shaken out by retest) or too wide (PSAR far below = absurd worst case). Hybrid gets the best of both:

| Policy | When PSAR close to price | When PSAR far from price |
|--------|--------------------------|--------------------------|
| `atr`  | Ignores PSAR, fixed mult | Ignores PSAR, fixed mult |
| `psar` | Stop too tight | Stop too wide |
| `hybrid` | **Widens to 0.8×ATR min** | **Clips to 2.5×ATR max** |

Policy is selectable via `SL_POLICY=hybrid|psar|atr`. See [`ai_bridge/.env.example`](./ai_bridge/.env.example) for all knobs.

### 5. Position sizing

Lot size is derived from risk budget (not hard-coded):

```
risk_usd    = equity × RISK_PER_TRADE_PCT / 100
$/point/lot = tick_value / tick_size        (XAUUSD ≈ $100/$1 move/lot)
raw_lot     = risk_usd / (stop_distance × $/point/lot)
final_lot   = round_down_to_step(clamp(raw_lot, vol_min, vol_max))
```

**Example**: equity $10k, 1% risk, hybrid stop=$8.00 → lot = 0.12. Final loss if hit = exactly $100.

When LLM returns `action=reduce`, risk drops to `RISK_PER_TRADE_PCT_REDUCE` (default 0.5%).

### 6. Breakeven management

Once a position is +1R in profit, a background task (every 10s) shifts SL to `entry ± 0.1×ATR` via MT5 modify. This converts potential losers into scratch trades and typically adds +10-20% to Sharpe on XAUUSD H1.

Configurable: `SL_BREAKEVEN_ENABLED`, `SL_BREAKEVEN_TRIGGER_R`, `SL_BREAKEVEN_BUFFER_ATR_MULT`.

---

## Repository layout

| Path | Purpose |
|------|---------|
| [`pinescript/smart_gold_analyzer_v2_ai.pine`](./pinescript/smart_gold_analyzer_v2_ai.pine) | **Current v2 strategy** — PSAR + EMA + Volume, webhook-enabled |
| [`pinescript/ALERT_PAYLOAD.md`](./pinescript/ALERT_PAYLOAD.md) | Exact payload schema + TradingView setup steps |
| [`smart_gold_analyzer`](./smart_gold_analyzer) | **v1 legacy** — standalone SMC indicator, no webhook |
| [`ai_bridge/app/`](./ai_bridge/app) | FastAPI application root |
| `ai_bridge/app/risk/` | **Stop calculator + position sizer + breakeven** |
| `ai_bridge/app/guards/` | **Safety guards** (daily trade cap, drawdown, spread, ATR, news) |
| `ai_bridge/app/backtest/` | Backtest harness with multiple engines |
| `ai_bridge/app/engine/` | LLM decision engine + prompts |
| `ai_bridge/app/executor/` | MT5 executor + NoopExecutor |
| `ai_bridge/docker/` | Dockerfile, compose, Caddyfile, systemd unit |
| `ai_bridge/scripts/backtest.py` | CLI backtest runner |
| `ai_bridge/scripts/smoke_test.py` | Offline smoke test (no server needed) |
| [`DEPLOYMENT.md`](./DEPLOYMENT.md) | Full VPS deployment walkthrough |

## Quickstart

### A. Pine indicator only (no AI)

Copy `pinescript/smart_gold_analyzer_v2_ai.pine` into the TradingView Pine Editor, apply to an **XAU/USD H1** chart. You get:

- EMA ribbon (20/50/100/200) + PSAR dots
- Entry/exit shape markers
- Live dashboard panel with trend state, volume ratio, position bar count
- Plain-text `alertcondition()` alerts for manual setups

### B. Full AI bridge (recommended)

1. Rent a VPS (any provider; 1 vCPU / 1 GB RAM sufficient).
2. Point a domain at it (for HTTPS via Caddy).
3. Follow **[DEPLOYMENT.md](./DEPLOYMENT.md)** — about 15 minutes end-to-end.
4. In the indicator Settings → **🤖 AI Bridge (Webhook)**:
   - Enable webhook alerts
   - Paste `WEBHOOK_SECRET`
   - Enable **Send exit signals to bridge** (recommended)
5. Create ONE TradingView alert:
   - Condition: this indicator → **"Any alert() function call"**
   - Webhook URL: `https://your-domain/webhook/tradingview`
   - Message: leave empty

That's it — both entries and exits flow through the same alert.

## Backtesting

The `ai_bridge/app/backtest/` harness replays historical data through the same signal engine, LLM, stop policy, and breakeven logic used in live trading. This means backtest metrics ARE the live metrics.

```bash
cd ai_bridge

# Recommended — new strategy with hybrid stops + breakeven
python scripts/backtest.py --yf GC=F --period 1y --interval 1h \
    --engine psar_ema_vol --exit-mode indicator \
    --stop-policy hybrid --breakeven \
    --out-json reports/sl_hybrid_be.json

# Compare without breakeven
python scripts/backtest.py --yf GC=F --period 1y --interval 1h \
    --engine psar_ema_vol --exit-mode indicator \
    --stop-policy hybrid

# Legacy SMC engine (v1 Pine script) — for A/B comparison
python scripts/backtest.py --yf GC=F --period 1y --interval 1h \
    --engine smartgold --exit-mode fixed_rr --stop-policy atr
```

Output metrics: `win_rate`, `expectancy_r`, `profit_factor`, `max_drawdown_r`, `sharpe_r`, `breakeven_shift_pct`. Reports saved as JSON for diffing.

## Smoke test (offline)

Verify the whole pipeline without a server / LLM / broker:

```bash
python scripts/smoke_test.py
```

Tests: settings loading, both signal engines, both exit modes, all guards, schema validation, backtest integration.

## Safety guards

Guards run **after** the LLM decision but **before** broker execution. Each can PASS, REDUCE lot size, or BLOCK the signal entirely:

| Guard | Default | Purpose |
|-------|---------|---------|
| `MaxDailyTradesGuard` | 5 trades/day | Spam protection |
| `DrawdownGuard` | -3R cap | Circuit breaker on bad days |
| `SpreadGuard` | >50 pts = block | Skip when broker spread abnormal |
| `MaxATRGuard` | >12 = block | Skip flash-news/extreme vol |
| `NewsBlackoutGuard` | reduce 25% | Downgrade during NFP/CPI/FOMC windows |

All tunable via `.env`. Disable individual guards by setting `GUARD_*=false`.

## LLM Decision Layer

For every signal, MiniMax receives:
- **The raw signal** (price, PSAR, EMAs, volume ratio, trend state)
- **Live macro** (DXY change, US10Y change in bp, recent headlines)
- **System prompt** aware of the PSAR+EMA+Volume strategy, with different decision rules for ENTRY vs EXIT signals

Returns structured JSON:
```json
{
  "action": "execute",
  "confidence": 0.78,
  "reasoning": "Strong long: DXY declining, yields flat, EMA ribbon aligned, volume 1.8x confirms breakout.",
  "risk_notes": "Avoid pyramiding — recent signals already executed.",
  "suggested_rr": 2.5,
  "suggested_stop_atr_mult": 1.2
}
```

Policy override: if `confidence < MIN_CONFIDENCE` (default 0.60), action is downgraded to `skip`.

## Dashboard

`https://your-domain/ui/` renders:
- Rolling signal stats (last 24h / 7d / 30d)
- Filterable signal table (by action, symbol, outcome)
- **Win-rate-by-signal chart** (once outcomes recorded)
- Per-signal detail view with:
  - Full LLM reasoning
  - Macro snapshot used
  - Inline form to record win/loss/breakeven

## License

Mozilla Public License 2.0.

## Author

[@fataakromulmuttaqin](https://github.com/fataakromulmuttaqin)
