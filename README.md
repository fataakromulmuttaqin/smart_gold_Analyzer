# SmartGold Analyzer Pro

Pine Script indicator + LLM decision bridge for XAU/USD (gold) on
TradingView. Combines classical price-action / SMC concepts (BOS/CHoCH,
Order Blocks, FVG, liquidity sweeps) with a MiniMax LLM that filters
signals against live macro context (DXY, US10Y yields, gold news) before
they reach your Telegram / broker.

```
TradingView (Pine v6)  ──▶  Your VPS (FastAPI)  ──▶  MiniMax LLM
                                     │
                                     ├──▶  Telegram (decision)
                                     ├──▶  Web Dashboard (/ui)
                                     └──▶  SQLite (audit log)
```

## Repository layout

| Path                                              | Purpose                                            |
|---------------------------------------------------|----------------------------------------------------|
| [`smart_gold_analyzer`](./smart_gold_analyzer)    | **v1** indicator — standalone, no webhook         |
| [`pinescript/smart_gold_analyzer_v2_ai.pine`](./pinescript/smart_gold_analyzer_v2_ai.pine) | **v2** — identical logic + JSON webhook alerts |
| [`pinescript/ALERT_PAYLOAD.md`](./pinescript/ALERT_PAYLOAD.md) | Exact payload schema + TradingView setup steps |
| [`ai_bridge/`](./ai_bridge)                       | FastAPI + MiniMax LLM decision server             |
| [`ai_bridge/docker/`](./ai_bridge/docker)         | Dockerfile, compose, Caddyfile, systemd unit      |
| [`DEPLOYMENT.md`](./DEPLOYMENT.md)                | Full VPS deployment walkthrough (Ubuntu 22.04/24.04) |

## Quickstart

### Use the Pine indicator alone (no AI)

Copy `smart_gold_analyzer` into the TradingView Pine Editor → Add to chart.
That's it — you get BOS/CHoCH, Order Blocks, FVG, liquidity sweeps, and
visual long/short signals on XAU/USD.

### Add the AI bridge

1. Rent a VPS (any provider, 1 vCPU / 1 GB RAM enough).
2. Point a domain at it (for HTTPS).
3. Follow **[DEPLOYMENT.md](./DEPLOYMENT.md)** — ~15 minutes end-to-end.
4. Swap the Pine indicator to `pinescript/smart_gold_analyzer_v2_ai.pine`
   and configure the TradingView webhook (instructions in
   [`ALERT_PAYLOAD.md`](./pinescript/ALERT_PAYLOAD.md)).

## What the AI bridge does

For every confirmed strong_long / strong_short (and optional weaker
signals), it:

1. **Enriches** the signal with live macro:
   - DXY price + daily change
   - US 10Y yield + change in basis points
   - Recent gold-related headlines (NewsAPI, optional)
2. **Asks MiniMax** (model `MiniMax-M2` by default) to review the signal
   against the context and return a JSON decision
   `{action, confidence, reasoning, risk_notes, suggested_rr, …}`.
3. **Applies risk policy**: if confidence < `MIN_CONFIDENCE` (default 0.60),
   the decision is downgraded to `skip` automatically. Invalid/malformed
   LLM responses also collapse to `skip`.
4. **Notifies Telegram** only when action is `execute` or `reduce`.
5. **Writes an audit row** to SQLite for every signal, even skipped ones.
6. **Renders a dashboard** at `https://YOUR_DOMAIN/ui/` with rolling
   stats, a filterable signal table, and a per-signal detail view
   showing the LLM's reasoning and the macro snapshot it used.

Mock mode (`LLM_MOCK_MODE=true`) bypasses the real LLM so you can test
plumbing end-to-end without spending tokens.

## License

Mozilla Public License 2.0. Inspired by LuxAlgo price-action concepts and
oscillator matrix ideas; all code original.

## Author

[@fataakromulmuttaqin](https://github.com/fataakromulmuttaqin)
