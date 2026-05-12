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
indicator and decide whether it should be EXECUTED, REDUCED in size, or
SKIPPED — given current macro context.

Principles:
  * Gold has strong INVERSE correlation with USD (DXY) and US real yields.
  * Avoid trading within ~30 minutes of major US data releases (NFP, CPI,
    FOMC). If news headlines suggest imminent risk events, lean SKIP.
  * A valid LONG needs: bullish structure AND (DXY soft OR yields falling).
  * A valid SHORT needs: bearish structure AND (DXY strong OR yields rising).
  * If the signal conflicts with macro (e.g. long gold while DXY rallying
    hard and yields spiking), choose SKIP or REDUCE.
  * Be conservative. It is always OK to skip.

You MUST respond with a single JSON object and NO prose, no markdown
fences. Schema:
{
  "action":      "execute" | "skip" | "reduce",
  "confidence":  number between 0.0 and 1.0,
  "reasoning":   short paragraph, max 3 sentences,
  "risk_notes":  short string; key caveats for the trader,
  "suggested_rr": number or null (e.g. 2.0 for 1:2 reward:risk),
  "suggested_stop_atr_mult": number or null (e.g. 1.5 = 1.5 x ATR)
}
"""


def build_user_prompt(alert: TradingViewAlert, ctx: MacroContext) -> str:
    """Render a structured user prompt describing the signal + macro state."""
    signal_block = {
        "symbol": alert.symbol,
        "timeframe": alert.timeframe,
        "signal": alert.signal,
        "price": alert.price,
        "time": alert.time,
        "ms_state": alert.ms_state,
        "rsi": alert.rsi,
        "atr": alert.atr,
        "money_flow": alert.money_flow,
        "ema_fast": alert.ema_fast,
        "ema_slow": alert.ema_slow,
        "ema_base": alert.ema_base,
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

    return (
        "SIGNAL:\n"
        + json.dumps(signal_block, indent=2)
        + "\n\nMACRO CONTEXT:\n"
        + json.dumps(macro_block, indent=2)
        + "\n\nReturn the JSON decision now."
    )
