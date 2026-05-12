"""Pydantic models shared across modules (webhook payload, LLM decision, context)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ══════════════════════════════════════════════════════════════════════════
# Incoming TradingView webhook payload
# ══════════════════════════════════════════════════════════════════════════
class TradingViewAlert(BaseModel):
    """Schema for the JSON body posted by TradingView alerts.

    The SmartGold indicator's ``alertcondition()`` is wired via a custom
    JSON message in TradingView — see pinescript/ALERT_PAYLOAD.md.
    """

    model_config = ConfigDict(extra="allow")

    secret: str = Field(..., description="Shared WEBHOOK_SECRET for auth")
    symbol: str = Field(..., description="e.g. OANDA:XAUUSD")
    timeframe: str = Field(..., description="e.g. 15, 60, 240, D")
    signal: Literal[
        "long",
        "short",
        "strong_long",
        "strong_short",
        "bull_choch",
        "bear_choch",
        "bull_bos",
        "bear_bos",
        "bull_grab",
        "bear_grab",
    ]
    price: float = Field(..., description="Close price at signal bar")
    time: str | None = Field(default=None, description="Bar time (ISO or TV format)")

    # Optional indicator context (filled if available from Pine script)
    ms_state: str | None = None
    rsi: float | None = None
    atr: float | None = None
    money_flow: float | None = None
    ema_fast: float | None = None
    ema_slow: float | None = None
    ema_base: float | None = None


# ══════════════════════════════════════════════════════════════════════════
# Macro context fetched by the context provider
# ══════════════════════════════════════════════════════════════════════════
class MacroContext(BaseModel):
    """Snapshot of macro variables relevant to XAU/USD."""

    dxy_price: float | None = None
    dxy_change_pct: float | None = None
    us10y_yield: float | None = None
    us10y_change_bp: float | None = None
    news_headlines: list[str] = Field(default_factory=list)
    fetched_at: datetime = Field(default_factory=datetime.utcnow)
    partial: bool = Field(
        default=False,
        description="True if some data sources failed (graceful degradation)",
    )
    notes: list[str] = Field(default_factory=list)


# ══════════════════════════════════════════════════════════════════════════
# LLM structured decision output
# ══════════════════════════════════════════════════════════════════════════
class LLMDecision(BaseModel):
    """Validated structure returned by the decision engine."""

    action: Literal["execute", "skip", "reduce"]
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    risk_notes: str = ""
    suggested_rr: float | None = Field(
        default=None,
        description="Suggested reward:risk ratio (e.g. 2.0 means 1:2)",
    )
    suggested_stop_atr_mult: float | None = Field(
        default=None,
        description="Stop loss distance in ATR multiples",
    )


# ══════════════════════════════════════════════════════════════════════════
# Final aggregated response (webhook -> client)
# ══════════════════════════════════════════════════════════════════════════
class BridgeResponse(BaseModel):
    accepted: bool
    alert: TradingViewAlert
    context: MacroContext
    decision: LLMDecision
    notifier_sent: bool = False
    signal_id: int | None = None
    execution: dict | None = None
