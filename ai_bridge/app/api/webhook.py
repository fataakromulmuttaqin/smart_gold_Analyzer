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
from app.executor.factory import build_executor
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
# Built lazily on first request so tests / offline validators that change
# env vars after import still see the right settings.
_executor = None


def _get_executor():
    global _executor
    if _executor is None:
        _executor = build_executor()
    return _executor


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
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Body must be valid JSON",
        ) from exc

    try:
        alert = TradingViewAlert.model_validate(body)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Invalid webhook payload: {}", exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Payload validation failed: {exc}",
        ) from exc

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

    # ── Broker execution (optional) ────────────────────────────────────
    execution = await _get_executor().execute(alert, decision)
    if execution.placed:
        logger.info(
            "Executor placed order #{} vol={} {} @ {}",
            execution.order_id,
            execution.volume,
            execution.side,
            execution.entry_price,
        )
    elif execution.error:
        logger.warning("Executor error: {}", execution.error)

    # Log every signal (even skips) for audit
    try:
        signal_id = await _signal_log.record(
            alert, context, decision, notified=notified, execution=execution
        )
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
        execution=execution.to_dict(),
    )


@router.get("/health")
async def health() -> dict:
    """Liveness + non-sensitive config summary."""
    s = get_settings()
    ex = _get_executor()
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
        "executor": getattr(ex, "name", "unknown"),
        "mt5_enabled": s.mt5_enabled,
    }
