# TradingView → AI Bridge Alert Payload

The `smart_gold_analyzer_v2_ai.pine` indicator emits this exact JSON body to
your webhook URL on every confirmed signal bar. The AI bridge
(`ai_bridge/`) parses it via `TradingViewAlert` (`app/models/schemas.py`).

## Payload schema

```jsonc
{
  "secret":      "string",   // shared secret, must match server WEBHOOK_SECRET
  "symbol":      "string",   // e.g. "XAUUSD" (from syminfo.ticker)
  "timeframe":   "string",   // e.g. "60" for 1h, "240" for 4h, "D" for daily
  "signal":      "string",   // one of: strong_long | strong_short | long | short
                             //         bull_choch | bear_choch
  "price":       number,     // close price at signal bar (4 decimal places)
  "time":        "string",   // ISO-8601 UTC, e.g. "2026-05-12T18:00:00Z"

  // Indicator runtime context — used by the LLM to reason about the signal
  "ms_state":    "string",   // "bullish" | "bearish" | "neutral"
  "rsi":         number,     // current RSI (14)
  "atr":         number,     // current ATR (14)
  "money_flow":  number,     // 0..100 smart money flow oscillator
  "ema_fast":    number,     // EMA 21
  "ema_slow":    number,     // EMA 50
  "ema_base":    number      // EMA 200
}
```

### Example (what TradingView actually POSTs)

```json
{
  "secret":"abc123xyz",
  "symbol":"XAUUSD",
  "timeframe":"60",
  "signal":"strong_long",
  "price":2345.6700,
  "time":"2026-05-12T18:00:00Z",
  "ms_state":"bullish",
  "rsi":52.34,
  "atr":3.2100,
  "money_flow":67.50,
  "ema_fast":2340.5000,
  "ema_slow":2332.1000,
  "ema_base":2290.7000
}
```

## TradingView alert setup (5 steps)

1. Apply `smart_gold_analyzer_v2_ai.pine` to a gold chart (XAU/USD).
2. In the indicator **Settings → 🤖 AI Bridge (Webhook)**:
   - Toggle **Enable AI bridge webhook alerts** = on
   - Paste your `WEBHOOK_SECRET` into **Webhook Secret**
     (generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`)
3. Click the **Alarm bell** icon → **Create alert**.
4. **Condition**: pick this indicator → **Any alert() function call**.
   (Do *not* pick the per-signal alertconditions for this — those are the
   legacy v1 alerts that only send plain text.)
5. **Notifications** tab:
   - Enable **Webhook URL**
   - Paste: `https://YOUR_DOMAIN/webhook/tradingview`
   - Leave **Message** field empty — `alert()` supplies the full JSON body.

That's it. TradingView will fire exactly once per confirmed bar when the
underlying condition triggers.

## Signals summary

| `signal` value     | Fires when                                          | Default on? |
|--------------------|-----------------------------------------------------|-------------|
| `strong_long`      | Full confluence long (structure + MF + RSI + grab)  | ✅          |
| `strong_short`     | Full confluence short                               | ✅          |
| `long`             | Basic long (weaker confluence)                      | ❌ (opt-in) |
| `short`            | Basic short                                         | ❌ (opt-in) |
| `bull_choch`       | Bullish Change of Character                         | ❌ (opt-in) |
| `bear_choch`       | Bearish Change of Character                         | ❌ (opt-in) |

Toggle the two "Also send …" inputs in the indicator to enable the weaker
signals / structure events.

## Server-side validation

The server validates the payload against `app/models/schemas.py::TradingViewAlert`.
Unknown fields are **allowed** (pydantic `extra="allow"`) so you can add
extra keys in the Pine script without breaking the server. Missing
required fields (e.g. `secret`, `symbol`) return HTTP 422.

## Troubleshooting

- **401 Unauthorized** → `secret` in payload ≠ `WEBHOOK_SECRET` on server.
- **422 Unprocessable Entity** → usually indicates a Pine Script edit
  broke JSON quoting. Check TradingView's alert log (Alerts tab → History).
- **429 Too Many Requests** → within `SIGNAL_COOLDOWN_SECONDS` of the
  previous signal for the same `(symbol, signal)` tuple. Normal behavior.
- **No fire at all** → ensure the alert condition is set to **Any
  alert() function call**, not a named alertcondition. TradingView will
  silently never fire if the wrong condition is selected.
