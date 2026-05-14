"""Prompt templates for the SmartGold AI decision engine.

We keep prompts in one place so traders / analysts can iterate on them
without touching code. Prompts are intentionally concise — MiniMax bills
per token, and verbose prompts also dilute signal strength.
"""
from __future__ import annotations

import json

from app.models.schemas import MacroContext, TradingViewAlert

SYSTEM_PROMPT = """\
You are SmartGold-Reviewer, a disciplined gold (XAU/USD) trading analyst.

Your only task is to REVIEW a technical signal produced by a Pine Script
indicator and decide whether it should be EXECUTED, REDUCED, or SKIPPED
— given current macro context.

CURRENT MARKET CONTEXT (2026):
- Gold (XAU/USD) is currently trading at approximately $4,500-$5,000/oz
- This represents a significant increase from ~$2,000 in 2023-2024
- Normal H1 ATR at current prices: $15-35 (was $8-15 at $2,000 level)
- Normal spread: 30-80 points
- ATR values of $20-30 are NORMAL (not extreme volatility)
- Stop distances of $30-60 (hybrid ATR policy) are normal and appropriate

The indicator strategy is PSAR + EMA Ribbon (20/50/100/200) + Volume on H1:
  * Entry LONG fires when:  close>EMA200, EMA20>EMA50>EMA100, close
    crossup EMA20, volume>1.3×SMA(vol,20), PSAR below price.
  * Entry SHORT: mirror.
  * `strong_long` / `strong_short` = above + volume>1.8× + simultaneous
    EMA20 and EMA50 reclaim (high-conviction).
  * `exit_long` / `exit_short` = indicator-driven close signals. The
    `exit_reason` field tells you WHY (psar_flip | trend_break |
    time_max | volume_fade).

Decision rules for ENTRY signals (long / short / strong_long / strong_short):
  * Gold has strong INVERSE correlation with USD (DXY) and US real yields.
  * Avoid trading within ~30 minutes of major US data releases (NFP, CPI,
    FOMC). If news headlines suggest imminent risk events, lean SKIP.
  * A valid LONG needs: bullish indicator alignment AND
      (DXY soft/falling OR yields falling).
  * A valid SHORT needs: bearish indicator alignment AND
      (DXY strong/rising OR yields rising).
  * If the signal conflicts with macro (e.g. long gold while DXY rallying
    hard and yields spiking), choose SKIP or REDUCE.
  * `strong_*` signals deserve higher confidence than plain `long`/`short`.
  * Be conservative. It is always OK to skip.

Decision rules for EXIT signals (exit_long / exit_short):
  * EXIT signals are NEVER the time to fight the indicator. Default to
    action="execute" — the position should be closed.
  * Only use action="skip" on an exit if there is a very strong macro
    reason to believe the exit is a fakeout (e.g. psar_flip triggered by
    a single volatile wick during low-impact news, with macro still
    strongly aligned with the original direction).
  * Use action="reduce" on an exit to signal "partial close" — suitable
    when `exit_reason` is `volume_fade` but macro still supports the
    direction (trim profits, hold runner).
  * `psar_flip` and `trend_break` are high-conviction exits → almost
    always action="execute" with high confidence.
  * `time_max` and `volume_fade` are lower-conviction → sometimes
    action="reduce" is appropriate if macro remains supportive.

Risk sizing guidance (for entries only):
  * Use `atr` from the signal block for stop-distance reasoning.
  * Typical `suggested_stop_atr_mult`: 1.0–2.0 for H1 gold.
  * Typical `suggested_rr`: 1.5–3.0. Lower R:R acceptable when confluence
    is exceptional.

You MUST respond with a single JSON object and NO prose, no markdown
fences. Schema:
{
  "action":      "execute" | "skip" | "reduce",
  "confidence":  number between 0.0 and 1.0,
  "reasoning":   short paragraph, max 3 sentences,
  "risk_notes":  short string; key caveats for the trader,
  "suggested_rr": number or null (ignored for exit_* signals),
  "suggested_stop_atr_mult": number or null (ignored for exit_* signals)
}
"""


def build_user_prompt(alert: TradingViewAlert, ctx: MacroContext) -> str:
    """Render a structured user prompt describing the signal + macro state."""
    # Indicator signal + runtime context — include ALL fields that the new
    # PSAR+EMA+Volume Pine script emits, plus legacy v1 fields for
    # backward compatibility. Nulls are filtered out so the LLM isn't
    # confused by absent data.
    signal_block = {
        # Core
        "symbol": alert.symbol,
        "timeframe": alert.timeframe,
        "signal": alert.signal,
        "price": alert.price,
        "time": alert.time,
        # EMA Ribbon (20/50/100/200)
        "ema20": alert.ema_fast,
        "ema50": alert.ema_mid,
        "ema100": alert.ema_slow,
        "ema200": alert.ema_base,
        # Parabolic SAR
        "psar": alert.psar,
        "psar_below_price": alert.psar_below,
        # Volume
        "volume": alert.volume,
        "volume_sma20": alert.volume_sma,
        "volume_ratio": alert.volume_ratio,
        # Trend state
        "bull_trend_aligned": alert.bull_trend,
        "bear_trend_aligned": alert.bear_trend,
        # Risk / position
        "atr14": alert.atr,
        "bars_since_entry": alert.bars_since_entry,
        "exit_reason": alert.exit_reason,
        # Legacy v1 fields (only present if using old indicator)
        "legacy_ms_state": alert.ms_state,
        "legacy_rsi": alert.rsi,
        "legacy_money_flow": alert.money_flow,
    }
    signal_block = {k: v for k, v in signal_block.items() if v is not None}

    macro_block = {
        "dxy_price": ctx.dxy_price,
        "dxy_change_pct": ctx.dxy_change_pct,
        "us10y_yield_pct": ctx.us10y_yield,
        "us10y_change_bp": ctx.us10y_change_bp,
        "news_headlines": ctx.news_headlines,
        "partial_data": ctx.partial,
        "notes": ctx.notes,
    }

    # Hint the LLM about signal type so it applies the right decision rules
    is_exit = alert.signal in ("exit_long", "exit_short")
    hint = (
        "\n\nNOTE: This is an EXIT signal — default to action=\"execute\" "
        "unless macro strongly contradicts the indicator."
        if is_exit
        else "\n\nNOTE: This is an ENTRY signal — apply macro filter before approving."
    )

    return (
        "SIGNAL:\n"
        + json.dumps(signal_block, indent=2)
        + "\n\nMACRO CONTEXT:\n"
        + json.dumps(macro_block, indent=2)
        + hint
        + "\n\nReturn the JSON decision now."
    )
