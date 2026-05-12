"""Async Telegram Bot API notifier.

Sends formatted signal + decision messages to a configured chat. All
errors are swallowed and logged — a failed notification must not break
the webhook response.
"""
from __future__ import annotations

import httpx

from app.config.settings import Settings, get_settings
from app.models.schemas import LLMDecision, MacroContext, TradingViewAlert
from app.utils.logging import logger

# Action → emoji for at-a-glance Telegram UX
_ACTION_EMOJI = {
    "execute": "✅",
    "reduce": "⚠️",
    "skip": "⏸️",
}


def _format_message(
    alert: TradingViewAlert,
    context: MacroContext,
    decision: LLMDecision,
) -> str:
    emoji = _ACTION_EMOJI.get(decision.action, "ℹ️")
    rr = (
        f"1:{decision.suggested_rr:g}"
        if decision.suggested_rr is not None
        else "—"
    )
    stop = (
        f"{decision.suggested_stop_atr_mult:g}×ATR"
        if decision.suggested_stop_atr_mult is not None
        else "—"
    )

    macro_line = ""
    if context.dxy_price is not None:
        macro_line += f"DXY: {context.dxy_price} ({context.dxy_change_pct:+.2f}%)  "
    if context.us10y_yield is not None:
        macro_line += f"US10Y: {context.us10y_yield:.2f}% ({context.us10y_change_bp:+.1f}bp)"
    if not macro_line:
        macro_line = "_(macro context unavailable)_"

    news_line = ""
    if context.news_headlines:
        # take top 2, trim each to ~100 chars
        top = [h[:100] for h in context.news_headlines[:2]]
        news_line = "\n*News:* " + " | ".join(top)

    return (
        f"{emoji} *SmartGold Decision: {decision.action.upper()}*\n"
        f"_{alert.symbol} / {alert.timeframe}m — {alert.signal}_\n"
        f"Price: `{alert.price}`  Confidence: `{decision.confidence:.2f}`\n"
        f"\n*Macro:* {macro_line}"
        f"{news_line}\n"
        f"\n*Reasoning:* {decision.reasoning}\n"
        f"*Risk:* {decision.risk_notes or '—'}\n"
        f"*Suggested R:R:* {rr}  *Stop:* {stop}"
    )


class TelegramNotifier:
    """Fire-and-forget Telegram Bot API client."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def send(
        self,
        alert: TradingViewAlert,
        context: MacroContext,
        decision: LLMDecision,
    ) -> bool:
        if not self.settings.telegram_is_configured:
            logger.debug("Telegram not configured — skipping notify")
            return False

        url = (
            f"https://api.telegram.org/bot{self.settings.telegram_bot_token}"
            f"/sendMessage"
        )
        text = _format_message(alert, context, decision)
        payload = {
            "chat_id": self.settings.telegram_chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                logger.warning(
                    "Telegram API returned HTTP {}: {}",
                    resp.status_code,
                    resp.text[:200],
                )
                return False
            return True
        except httpx.HTTPError as exc:
            logger.warning("Telegram send failed: {}", exc)
            return False


__all__ = ["TelegramNotifier", "_format_message"]
