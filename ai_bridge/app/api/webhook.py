"""FastAPI webhook endpoints.

POST /webhook/tradingview
    Main entry from TradingView alerts. Validates shared secret, runs
    the decision pipeline, writes audit log, optionally notifies Telegram.

GET /health
    Liveness + configuration summary (without leaking secrets).
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict

from fastapi import APIRouter, HTTPException, Request, status

from app.config.settings import get_settings
from app.context.market_context import fetch_macro_context
from app.engine.decision_engine import evaluate_signal
from app.models.schemas import BridgeResponse, TradingViewAlert
from app.notifier.telegram import TelegramNotifier
from app.storage.signal_log import SignalLog
from app.utils.logging import logger


router = APIRouter()

# In-memory cooldown tracker: {(symbol, signal): last_unix_ts}
_cooldown: dict[tuple[str, str], float] = defaultdict(float)
_cooldown_lock = asyncio.Lock()

_signal_log = SignalLog()
_notifier = TelegramNotifier()


def _check_cooldown(alert: TradingViewAlert, cooldown_s: int) -> bool:
    """Returns True if within cooldown (should drop this signal)."""
    if cooldown_s <= 0:
        return False
    key = (alert.symbol, alert.signal)
    last = _cooldown[key]
    now = time.time()
    if now - last < cooldown_s:
        return True
    _cooldown[key] = now
    return False


@router.post("/webhook/tradingview", response_model=BridgeResponse)
async def tradingview_webhook(request: Request) -> BridgeResponse:
    """Process an incoming TradingView alert."""
    settings = get_settings()

    # Accept both JSON and form-encoded bodies (TradingView wraps in JSON).
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Body must be valid JSON",
        )

    try:
        alert = TradingViewAlert.model_validate(body)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Invalid webhook payload: {}", exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Payload validation failed: {exc}",
        )

    # ── Auth: shared secret ─────────────────────────────────────────────
    if not settings.webhook_secret:
        logger.error("WEBHOOK_SECRET is empty — refusing all requests")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Bridge is not configured with a webhook secret",
        )
    if alert.secret != settings.webhook_secret:
        logger.warning("Webhook auth failed for symbol={}", alert.symbol)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook secret",
        )

    # ── Cooldown ────────────────────────────────────────────────────────
    async with _cooldown_lock:
        if _check_cooldown(alert, settings.signal_cooldown_seconds):
            logger.info(
                "Cooldown active for {} {} — dropping",
                alert.symbol,
                alert.signal,
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Signal within cooldown window",
            )

    logger.info(
        "Received alert: symbol={} tf={} signal={} price={}",
        alert.symbol,
        alert.timeframe,
        alert.signal,
        alert.price,
    )

    # ── Macro context ──────────────────────────────────────────────────
    context = await fetch_macro_context(settings=settings)

    # ── LLM decision ───────────────────────────────────────────────────
    decision = await evaluate_signal(alert, context, settings=settings)

    # ── Notify & log ───────────────────────────────────────────────────
    notified = False
    if decision.action in {"execute", "reduce"}:
        notified = await _notifier.send(alert, context, decision)
    # Log every signal (even skips) for audit
    try:
        signal_id = await _signal_log.record(alert, context, decision, notified=notified)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to write signal log: {}", exc)
        signal_id = None

    return BridgeResponse(
        accepted=True,
        alert=alert,
        context=context,
        decision=decision,
        notifier_sent=notified,
        signal_id=signal_id,
    )


@router.get("/health")
async def health() -> dict:
    """Liveness + non-sensitive config summary."""
    s = get_settings()
    return {
        "status": "ok",
        "app_env": s.app_env,
        "llm_mock_mode": s.llm_mock_mode,
        "llm_configured": s.llm_is_configured,
        "telegram_configured": s.telegram_is_configured,
        "newsapi_configured": s.newsapi_is_configured,
        "macro_context_enabled": s.enable_macro_context,
        "min_confidence": s.min_confidence,
        "cooldown_seconds": s.signal_cooldown_seconds,
        "model": s.minimax_model,
    }
