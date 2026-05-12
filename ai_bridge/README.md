# SmartGold AI Bridge

An LLM-augmented decision layer that sits between the **SmartGold Analyzer Pro**
Pine Script indicator on TradingView and your trading workflow.

```
┌──────────────────┐    webhook     ┌──────────────────────┐
│  TradingView     │  ────────────▶ │  AI Bridge (FastAPI) │
│  (Pine Script    │   JSON alert    │  + MiniMax LLM       │
│   alerts)        │                 │  + Macro filters     │
└──────────────────┘                 │  + Telegram alerts   │
                                     └──────────┬───────────┘
                                                │
                                                ▼
                                         SQLite audit log
```

## What it does

1. Receives a TradingView alert (POST webhook) with the signal fired by the
   `SmartGold Analyzer Pro` indicator.
2. Enriches the signal with macro context (DXY, US10Y yield, recent gold news).
3. Calls **MiniMax** LLM with a structured prompt that returns a JSON decision:
   `{action, confidence, reasoning, risk_notes}`.
4. Applies local risk policy (min confidence, cooldown).
5. Forwards the final decision to Telegram (and logs everything to SQLite).

## Quick links

- [Deployment guide (VPS + Docker)](../DEPLOYMENT.md)
- [`.env.example`](./.env.example) — required environment variables
- [`app/`](./app) — FastAPI application source
- [`scripts/smoke_test.py`](./scripts/smoke_test.py) — end-to-end smoke test

## Local development

```bash
cd ai_bridge
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in MINIMAX_API_KEY, WEBHOOK_SECRET
uvicorn app.main:app --reload --port 8080
```

Then in another terminal:

```bash
python scripts/smoke_test.py  # posts a fake TV alert, prints the decision
```

## Production

```bash
docker compose -f docker/docker-compose.yml up -d --build
```

Full VPS setup (Ubuntu + Caddy HTTPS + Telegram) is in
[DEPLOYMENT.md](../DEPLOYMENT.md).
