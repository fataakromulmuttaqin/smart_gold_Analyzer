# TradingView → AI Bridge Alert Payload

The `smart_gold_analyzer_v2_ai.pine` indicator (PSAR + EMA Ribbon +
Volume strategy) emits this exact JSON body to your webhook URL on every
confirmed signal bar. The AI bridge (`ai_bridge/`) parses it via
`TradingViewAlert` (`app/models/schemas.py`).

## Strategy summary

**Timeframe:** H1 (recommended — strategy tuned for this)
**Entry Long:** `close > EMA200` AND `EMA20 > EMA50 > EMA100` AND
`close crossup EMA20` AND `volume > SMA(vol,20) × 1.3` AND `PSAR below price`
**Entry Short:** mirror of above
**Exit (priority order, first match wins):**
1. PSAR flip (`psar_flip`)
2. Close crosses back through EMA20 (`trend_break`)
3. Max bars reached = 6 (`time_max`)
4. After min 4 bars + 2 consecutive declining volume bars (`volume_fade`)

## Payload schema

```jsonc
{
  "secret":      "string",   // shared secret, must match server WEBHOOK_SECRET
  "symbol":      "string",   // e.g. "XAUUSD" (from syminfo.ticker)
  "timeframe":   "string",   // "60" for H1 (tuned value) — others untested
  "signal":      "string",   // strong_long | strong_short | long | short |
                             // exit_long | exit_short
  "price":       number,     // close price at signal bar
  "time":        "string",   // ISO-8601 UTC, e.g. "2026-05-12T18:00:00Z"

  // ── EMA Ribbon ────────────────────────────────────────────────────
  "ema_fast":    number,     // EMA 20
  "ema_mid":     number,     // EMA 50
  "ema_slow":    number,     // EMA 100
  "ema_base":    number,     // EMA 200 (trend filter)

  // ── Parabolic SAR ─────────────────────────────────────────────────
  "psar":        number,     // current SAR value
  "psar_below":  boolean,    // true = bullish (SAR below price)

  // ── Volume ────────────────────────────────────────────────────────
  "volume":       number,    // raw volume for this bar
  "volume_sma":   number,    // SMA(volume, 20)
  "volume_ratio": number,    // volume / volume_sma (e.g. 1.35 = 35% above avg)

  // ── Trend State ───────────────────────────────────────────────────
  "bull_trend":  boolean,    // close>EMA200 AND EMA20>EMA50>EMA100
  "bear_trend":  boolean,    // close<EMA200 AND EMA20<EMA50<EMA100

  // ── Risk / Position ───────────────────────────────────────────────
  "atr":             number, // ATR(14) for position sizing
  "bars_since_entry": integer, // 0 for entries; N for exit signals
  "exit_reason":      "string" // "" for entries; psar_flip | trend_break |
                               // time_max | volume_fade for exits
}
```

### Example — entry signal

```json
{
  "secret":"abc123xyz",
  "symbol":"XAUUSD",
  "timeframe":"60",
  "signal":"strong_long",
  "price":2345.6700,
  "time":"2026-05-12T18:00:00Z",
  "ema_fast":2344.10,
  "ema_mid":2338.80,
  "ema_slow":2330.40,
  "ema_base":2290.70,
  "psar":2339.55,
  "psar_below":true,
  "atr":3.21,
  "volume":1850,
  "volume_sma":980,
  "volume_ratio":1.888,
  "bull_trend":true,
  "bear_trend":false,
  "bars_since_entry":0,
  "exit_reason":""
}
```

### Example — exit signal

```json
{
  "secret":"abc123xyz",
  "symbol":"XAUUSD",
  "timeframe":"60",
  "signal":"exit_long",
  "price":2349.10,
  "time":"2026-05-12T22:00:00Z",
  "ema_fast":2347.50,
  "ema_mid":2340.20,
  "ema_slow":2331.10,
  "ema_base":2291.00,
  "psar":2350.80,
  "psar_below":false,
  "atr":3.45,
  "volume":720,
  "volume_sma":960,
  "volume_ratio":0.75,
  "bull_trend":true,
  "bear_trend":false,
  "bars_since_entry":5,
  "exit_reason":"psar_flip"
}
```

## TradingView alert setup (5 steps)

1. Apply `smart_gold_analyzer_v2_ai.pine` to a **XAU/USD H1** chart.
2. In the indicator **Settings → 🤖 AI Bridge (Webhook)**:
   - **Enable AI bridge webhook alerts** = on
   - **Webhook Secret** = paste your `WEBHOOK_SECRET` value
     (generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`)
   - **Send exit signals to bridge** = on (recommended — lets the bridge
     record position lifecycle and the LLM reason about exit quality)
3. Click the **Alarm bell** → **Create alert**.
4. **Condition**: select this indicator → **Any alert() function call**.
   (Do NOT pick a named `alertcondition` — those are plain-text only.)
5. **Notifications**:
   - Enable **Webhook URL**
   - Paste: `https://YOUR_DOMAIN/webhook/tradingview`
   - Leave **Message** field empty (`alert()` provides the JSON).

One alert covers ALL signal types (entries + exits) — the `signal` field
inside the payload tells the server what happened.

## Signal reference

| `signal` value   | Fires when                                                   | Default on? |
|------------------|--------------------------------------------------------------|-------------|
| `strong_long`    | Full alignment + vol×1.8 + simultaneous EMA20/50 reclaim     | ✅          |
| `strong_short`   | Mirror                                                       | ✅          |
| `long`           | Entry conditions met but not "strong" confluence             | ❌ (opt-in) |
| `short`          | Mirror                                                       | ❌ (opt-in) |
| `exit_long`      | Any of the 4 exit rules triggers while in long position      | ✅          |
| `exit_short`     | Mirror                                                       | ✅          |

## Exit reason codes

- `psar_flip`    — PSAR flipped to the opposite side (most reliable exit)
- `trend_break`  — Close crossed back through EMA20 against position
- `time_max`     — Held 6+ bars, forced exit
- `volume_fade`  — Held 4+ bars and volume declined 2 bars in a row

## Server-side compatibility

The server validates the payload against `app/models/schemas.py::TradingViewAlert`.
- Unknown fields are **allowed** (pydantic `extra="allow"`) — you can add
  new keys in Pine without touching the server.
- Missing required fields (e.g. `secret`, `symbol`, `signal`, `price`)
  return HTTP 422.
- Legacy v1 signals (`bull_choch`, `bear_bos`, etc.) are still accepted
  by the server enum but are no longer emitted by this indicator — safe
  to ignore.

## Troubleshooting

- **401 Unauthorized** → `secret` mismatch. Copy the exact
  `WEBHOOK_SECRET` from the server `.env` into the indicator settings.
- **422 Unprocessable Entity** → inspect TradingView alert log (Alerts
  tab → History); usually caused by a broken JSON edit in Pine.
- **429 Too Many Requests** → within `SIGNAL_COOLDOWN_SECONDS` of the
  previous signal for the same `(symbol, signal)` tuple. Expected.
- **No alert fires at all** → ensure alert condition = **Any alert()
  function call**, not a named `alertcondition`. TradingView silently
  never fires if the wrong condition is selected.
- **No exit signals appear** → ensure **Send exit signals to bridge** is
  enabled in the indicator inputs.
