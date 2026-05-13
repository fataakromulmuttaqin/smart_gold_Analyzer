"""FastAPI webhook endpoints.

POST /webhook/tradingview
    Main entry from TradingView alerts. Validates shared secret, runs
    the decision pipeline, applies safety guards, writes audit log,
    optionally notifies Telegram, optionally executes on broker.

GET /health
    Liveness + configuration summary (without leaking secrets).
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict

from fastapi import APIRouter, HTTPException, Request, status

from app.config.settings import Settings, get_settings
from app.context.market_context import fetch_macro_context
from app.engine.decision_engine import evaluate_signal
from app.executor.factory import build_executor
from app.guards import (
    ConsecutiveLossGuard,
    DrawdownGuard,
    GuardChain,
    MaxDailyTradesGuard,
    NewsBlackoutGuard,
    WeekendGuard,
)
from app.models.schemas import BridgeResponse, LLMDecision, TradingViewAlert
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
_guard_chain: GuardChain | None = None


def _get_executor():
    global _executor
    if _executor is None:
        _executor = build_executor()
    return _executor


def _get_guard_chain(settings: Settings) -> GuardChain:
    """Build and cache the safety guard chain from settings."""
    global _guard_chain
    if _guard_chain is not None:
        return _guard_chain

    guards = []

    if settings.guard_weekend_enabled:
        guards.append(
            WeekendGuard(friday_cutoff_hour=settings.guard_friday_cutoff_hour)
        )

    if settings.guard_news_blackout_enabled:
        guards.append(NewsBlackoutGuard())

    if settings.guard_consecutive_loss_enabled:
        guards.append(
            ConsecutiveLossGuard(
                max_consecutive=settings.guard_max_consecutive_losses,
                db_path=settings.sqlite_path,
                cooldown_minutes=settings.guard_loss_cooldown_minutes,
            )
        )

    if settings.guard_max_daily_trades_enabled:
        guards.append(
            MaxDailyTradesGuard(
                max_trades=settings.guard_max_daily_trades,
                db_path=settings.sqlite_path,
            )
        )

    if settings.guard_drawdown_enabled:
        guards.append(
            DrawdownGuard(
                max_daily_loss_usd=settings.guard_max_daily_loss_usd,
                db_path=settings.sqlite_path,
            )
        )

    _guard_chain = GuardChain(guards)
    logger.info(
        "Guard chain initialised with {} guards: {}",
        len(guards),
        [g.name for g in guards],
    )
    return _guard_chain


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

    # ── Safety guards ──────────────────────────────────────────────────
    # Guards run AFTER the LLM decision (so they can inspect `decision.action`)
    # but BEFORE execution. If a guard trips, the decision is downgraded to
    # "skip" — the signal is still logged for audit but no order is placed.
    guard_chain = _get_guard_chain(settings)
    guard_result = await guard_chain.check(alert, decision)

    if not guard_result.allowed:
        logger.warning(
            "Guard BLOCKED signal: guard={} reason={}",
            guard_result.guard_name,
            guard_result.reason,
        )
        # Downgrade to skip — preserve original reasoning in risk_notes
        decision = LLMDecision(
            action="skip",
            confidence=decision.confidence,
            reasoning=decision.reasoning,
            risk_notes=(
                f"[GUARD: {guard_result.guard_name}] {guard_result.reason} "
                f"| Original action was '{decision.action}'."
            ),
            suggested_rr=decision.suggested_rr,
            suggested_stop_atr_mult=decision.suggested_stop_atr_mult,
        )

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
    chain = _get_guard_chain(s)
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
        "guards_active": [g.name for g in chain.guards],
    }
