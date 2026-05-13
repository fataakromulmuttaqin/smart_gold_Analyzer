#!/usr/bin/env bash
# Simple curl cheatsheet for manual testing of the AI Bridge endpoints.
# Source your .env first:   set -a && source .env && set +a
set -euo pipefail

BASE_URL="${SMOKE_URL:-http://127.0.0.1:8080}"
SECRET="${WEBHOOK_SECRET:?WEBHOOK_SECRET not set}"

echo "=== GET /health ==="
curl -sS "${BASE_URL}/health" | python3 -m json.tool
echo

echo "=== POST /webhook/tradingview (strong_long) ==="
curl -sS -X POST "${BASE_URL}/webhook/tradingview" \
  -H "Content-Type: application/json" \
  -d @- <<JSON | python3 -m json.tool
{
  "secret":     "${SECRET}",
  "symbol":     "XAUUSD",
  "timeframe":  "60",
  "signal":     "strong_long",
  "price":      3245.67,
  "time":       "2026-05-12T18:00:00Z",
  "ms_state":   "bullish",
  "rsi":        52.3,
  "atr":        8.40,
  "money_flow": 67.5,
  "ema_fast":   3243.10,
  "ema_slow":   3220.40,
  "ema_base":   3175.70
}
JSON
echo

echo "=== POST with bad secret (expect 401) ==="
curl -sS -o /dev/null -w "HTTP %{http_code}\n" -X POST \
  "${BASE_URL}/webhook/tradingview" \
  -H "Content-Type: application/json" \
  -d '{"secret":"wrong","symbol":"XAUUSD","timeframe":"60","signal":"strong_long","price":1}'
