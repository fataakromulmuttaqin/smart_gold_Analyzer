"""Pydantic models shared across modules (webhook payload, LLM decision, context)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ══════════════════════════════════════════════════════════════════════════
# Incoming TradingView webhook payload
# ══════════════════════════════════════════════════════════════════════════
class TradingViewAlert(BaseModel):
    """Schema for the JSON body posted by TradingView alerts.

    The SmartGold indicator's ``alertcondition()`` is wired via a custom
    JSON message in TradingView — see pinescript/ALERT_PAYLOAD.md.

    Accepts multiple key naming conventions from TradingView:
      - ema_fast / ema20      → EMA 20
      - ema_mid / ema50       → EMA 50
      - ema_slow / ema100     → EMA 100
      - ema_base / ema200     → EMA 200
      - atr / atr14           → ATR(14)
      - volume_sma / vol_sma  → SMA(volume, 20)
      - psar_below / psar_below_price → SAR position
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    secret: str = Field(..., description="Shared WEBHOOK_SECRET for auth")
    symbol: str = Field(..., description="e.g. XAUUSD, XAUUSDm (broker ticker)")
    timeframe: str = Field(..., description="e.g. 15, 60, 240, D")
    signal: Literal[
        # ── Current strategy (PSAR + EMA + Volume) ─────────────────
        "long",
        "short",
        "strong_long",
        "strong_short",
        "exit_long",
        "exit_short",
        # ── Legacy v1 (SMC — still accepted for backward compat) ───
        "bull_choch",
        "bear_choch",
        "bull_bos",
        "bear_bos",
        "bull_grab",
        "bear_grab",
    ]
    price: float = Field(..., description="Close price at signal bar")
    time: str | None = Field(default=None, description="Bar time (ISO or TV format)")

    # ── PSAR + EMA + Volume strategy fields (v2+) ───────────────────────
    # EMA Ribbon (20 / 50 / 100 / 200)
    # Accepts both internal names (ema_fast) and TradingView names (ema20)
    ema_fast: float | None = Field(
        default=None, alias="ema20", description="EMA 20"
    )
    ema_mid: float | None = Field(
        default=None, alias="ema50", description="EMA 50"
    )
    ema_slow: float | None = Field(
        default=None, alias="ema100", description="EMA 100"
    )
    ema_base: float | None = Field(
        default=None, alias="ema200", description="EMA 200 trend filter"
    )

    # Parabolic SAR
    psar: float | None = Field(default=None, description="Current SAR value")
    psar_below: bool | None = Field(
        default=None,
        alias="psar_below_price",
        description="True = bullish (SAR below price)",
    )

    # Volume
    volume: float | None = Field(default=None, description="Raw volume at signal bar")
    volume_sma: float | None = Field(
        default=None, alias="vol_sma", description="SMA(volume, 20)"
    )
    volume_ratio: float | None = Field(
        default=None, alias="vol_ratio", description="volume / volume_sma (>1 = above avg)"
    )

    # Trend state
    bull_trend: bool | None = Field(
        default=None, alias="bull_trend_aligned", description="Full bull alignment"
    )
    bear_trend: bool | None = Field(
        default=None, alias="bear_trend_aligned", description="Full bear alignment"
    )

    # Risk / position
    atr: float | None = Field(default=None, alias="atr14", description="ATR(14)")
    bars_since_entry: int | None = Field(
        default=None, description="0 for entries; N for exits"
    )
    exit_reason: str | None = Field(
        default=None,
        description='One of: "", psar_flip, trend_break, time_max, volume_fade',
    )

    # ── Legacy v1 (SMC) fields — kept for backward compatibility ────────
    ms_state: str | None = None
    rsi: float | None = None
    money_flow: float | None = None

    def model_post_init(self, __context: Any) -> None:
        """Handle additional field name mappings from TradingView alerts.

        TradingView may send keys like 'ema_fast', 'ema_mid' etc. directly,
        or alternative names. This post-init fills None fields from the
        extra data if the primary alias didn't match.
        """
        extra = self.model_extra or {}

        # EMA fields: accept both underscore and numeric naming
        if self.ema_fast is None:
            self.ema_fast = _coerce_float(extra.get("ema_fast") or extra.get("ema_20"))
        if self.ema_mid is None:
            self.ema_mid = _coerce_float(extra.get("ema_mid") or extra.get("ema_50"))
        if self.ema_slow is None:
            self.ema_slow = _coerce_float(extra.get("ema_slow") or extra.get("ema_100"))
        if self.ema_base is None:
            self.ema_base = _coerce_float(extra.get("ema_base") or extra.get("ema_200"))

        # ATR: accept 'atr', 'atr14', 'atr_14'
        if self.atr is None:
            self.atr = _coerce_float(extra.get("atr") or extra.get("atr_14"))

        # PSAR below: accept various names
        if self.psar_below is None:
            val = extra.get("psar_below") or extra.get("psar_below_price")
            if val is not None:
                self.psar_below = bool(val)

        # Volume fields
        if self.volume_sma is None:
            self.volume_sma = _coerce_float(
                extra.get("volume_sma") or extra.get("volume_sma20") or extra.get("vol_sma20")
            )
        if self.volume_ratio is None:
            self.volume_ratio = _coerce_float(
                extra.get("volume_ratio") or extra.get("vol_ratio")
            )

        # Trend alignment
        if self.bull_trend is None:
            val = extra.get("bull_trend") or extra.get("bull_trend_aligned")
            if val is not None:
                self.bull_trend = bool(val)
        if self.bear_trend is None:
            val = extra.get("bear_trend") or extra.get("bear_trend_aligned")
            if val is not None:
                self.bear_trend = bool(val)


def _coerce_float(val: Any) -> float | None:
    """Safely coerce a value to float, returning None on failure."""
    if val is None:
        return None
    try:
        f = float(val)
        return f if f == f else None  # NaN check
    except (TypeError, ValueError):
        return None


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
