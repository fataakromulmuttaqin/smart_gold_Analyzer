"""Decision engine: feeds signal + macro context into MiniMax, returns validated LLMDecision.

Safety rails:
  * Retry with exponential backoff on empty/non-JSON responses (up to 3 attempts).
  * If all retries fail, use a SMART FALLBACK based on signal type and
    indicator data — NOT a blind skip (the indicator already validated the
    setup, so a conservative execute is safer than losing every trade).
  * Validate the action enum & confidence range.
  * Enforce ``MIN_CONFIDENCE``: if confidence below threshold, downgrade
    execute/reduce to skip.
"""
from __future__ import annotations

import asyncio

from app.config.settings import Settings, get_settings
from app.engine.prompts import SYSTEM_PROMPT, build_user_prompt
from app.llm.minimax_client import (
    ChatMessage,
    MiniMaxClient,
    MiniMaxError,
)
from app.models.schemas import LLMDecision, MacroContext, TradingViewAlert
from app.utils.logging import logger

VALID_ACTIONS = {"execute", "skip", "reduce"}

# Retry configuration for transient LLM failures (empty response, timeout)
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 1.5  # seconds: 1.5, 3.0, 4.5


def _safe_skip(reason: str) -> LLMDecision:
    """Build a conservative SKIP decision for any error path."""
    return LLMDecision(
        action="skip",
        confidence=0.0,
        reasoning=f"AUTO-SKIP: {reason}",
        risk_notes="Decision engine fallback — no LLM decision was obtained.",
    )


def _smart_fallback(alert: TradingViewAlert, reason: str) -> LLMDecision:
    """Build a smart fallback decision based on indicator signal type.

    When the LLM is unavailable but the indicator has already validated
    all 5 entry conditions (trend + EMA ribbon + volume + PSAR + crossover),
    a conservative execute with reduced confidence is more profitable than
    blindly skipping every signal.

    For EXIT signals, we ALWAYS execute — the indicator's exit logic is
    mechanical and reliable (PSAR flip, trend break, etc.).
    """
    signal = alert.signal

    # EXIT signals: always honour the indicator's exit decision
    if signal in ("exit_long", "exit_short"):
        return LLMDecision(
            action="execute",
            confidence=0.75,
            reasoning=(
                f"LLM FALLBACK: {reason}. Exit signal '{signal}' "
                f"(reason: {alert.exit_reason or 'unknown'}) executed without "
                f"LLM review — indicator exit logic is mechanical and reliable."
            ),
            risk_notes="LLM unavailable — exit executed based on indicator logic only.",
            suggested_rr=None,
            suggested_stop_atr_mult=None,
        )

    # STRONG entry signals: the indicator already requires 5+ confluent
    # conditions. Execute with moderate confidence + tighter risk.
    if signal in ("strong_long", "strong_short"):
        return LLMDecision(
            action="execute",
            confidence=0.70,
            reasoning=(
                f"LLM FALLBACK: {reason}. Strong signal '{signal}' has full "
                f"indicator confluence (EMA ribbon + PSAR + volume >1.8x). "
                f"Executing with reduced confidence due to missing macro review."
            ),
            risk_notes=(
                "LLM unavailable — no macro filter applied. "
                "Using conservative R:R and standard ATR stop."
            ),
            suggested_rr=2.0,
            suggested_stop_atr_mult=1.5,
        )

    # Regular entry signals (long/short): less confluence, more cautious
    if signal in ("long", "short"):
        return LLMDecision(
            action="reduce",
            confidence=0.55,
            reasoning=(
                f"LLM FALLBACK: {reason}. Regular signal '{signal}' has basic "
                f"indicator confluence but no macro confirmation. "
                f"Executing with REDUCED size as precaution."
            ),
            risk_notes=(
                "LLM unavailable — reduced lot size applied. "
                "No macro context was evaluated."
            ),
            suggested_rr=2.0,
            suggested_stop_atr_mult=1.5,
        )

    # Unknown / legacy signals: conservative skip
    return _safe_skip(f"LLM fallback: unknown signal type '{signal}' — skipping safely")


def _parse_and_validate(raw: dict) -> LLMDecision:
    """Coerce dict to LLMDecision with defensive defaults."""
    action = str(raw.get("action", "skip")).lower().strip()
    if action not in VALID_ACTIONS:
        logger.warning("LLM returned invalid action '{}' → coerce to skip", action)
        action = "skip"

    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    reasoning = str(raw.get("reasoning", "")).strip() or "(no reasoning returned)"
    risk_notes = str(raw.get("risk_notes", "")).strip()

    suggested_rr = raw.get("suggested_rr")
    if suggested_rr is not None:
        try:
            suggested_rr = float(suggested_rr)
        except (TypeError, ValueError):
            suggested_rr = None

    suggested_stop = raw.get("suggested_stop_atr_mult")
    if suggested_stop is not None:
        try:
            suggested_stop = float(suggested_stop)
        except (TypeError, ValueError):
            suggested_stop = None

    return LLMDecision(
        action=action,
        confidence=confidence,
        reasoning=reasoning,
        risk_notes=risk_notes,
        suggested_rr=suggested_rr,
        suggested_stop_atr_mult=suggested_stop,
    )


def _apply_policy(decision: LLMDecision, settings: Settings) -> LLMDecision:
    """Apply local risk policy on top of LLM output (e.g. min confidence)."""
    if decision.action in {"execute", "reduce"} and decision.confidence < settings.min_confidence:
        logger.info(
            "Policy downgrade: confidence {:.2f} < min {:.2f} → skip",
            decision.confidence,
            settings.min_confidence,
        )
        return LLMDecision(
            action="skip",
            confidence=decision.confidence,
            reasoning=decision.reasoning,
            risk_notes=(
                f"Policy override: confidence below MIN_CONFIDENCE "
                f"({settings.min_confidence:.2f}). Original action was "
                f"'{decision.action}'."
            ),
            suggested_rr=decision.suggested_rr,
            suggested_stop_atr_mult=decision.suggested_stop_atr_mult,
        )
    return decision


async def evaluate_signal(
    alert: TradingViewAlert,
    context: MacroContext,
    settings: Settings | None = None,
) -> LLMDecision:
    """Main entry point: given an alert + macro context, return a validated decision.

    Retry logic:
      * Up to 3 attempts with exponential backoff on transient failures
        (empty response, non-JSON, timeout).
      * If all retries fail, use smart fallback based on signal type
        (NOT a blind skip — the indicator already validated the setup).

    Never raises — all failures collapse to a decision.
    """
    s = settings or get_settings()

    if not s.llm_is_configured:
        logger.warning("LLM not configured — using smart fallback")
        return _smart_fallback(alert, "LLM not configured (MINIMAX_API_KEY missing)")

    user_prompt = build_user_prompt(alert, context)
    messages = [
        ChatMessage(role="system", content=SYSTEM_PROMPT),
        ChatMessage(role="user", content=user_prompt),
    ]

    last_error: str = ""

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            async with MiniMaxClient(settings=s) as llm:
                resp = await llm.chat(messages, json_mode=True)

                # Guard against empty content (the root cause of the bug)
                if not resp.content or not resp.content.strip():
                    last_error = "LLM returned empty content"
                    logger.warning(
                        "MiniMax returned empty response (attempt {}/{})",
                        attempt, _MAX_RETRIES,
                    )
                    if attempt < _MAX_RETRIES:
                        await asyncio.sleep(_RETRY_BACKOFF_BASE * attempt)
                        continue
                    break

                raw = resp.as_json()

        except MiniMaxError as exc:
            last_error = str(exc)
            logger.warning(
                "MiniMax call failed (attempt {}/{}): {}",
                attempt, _MAX_RETRIES, exc,
            )
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(_RETRY_BACKOFF_BASE * attempt)
                continue
            break
        except Exception as exc:  # noqa: BLE001 — absolute last-resort guard
            last_error = str(exc)
            logger.exception(
                "Unexpected decision engine error (attempt {}/{})",
                attempt, _MAX_RETRIES,
            )
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(_RETRY_BACKOFF_BASE * attempt)
                continue
            break

        # Success — parse and apply policy
        decision = _parse_and_validate(raw)
        decision = _apply_policy(decision, s)

        logger.info(
            "Decision: action={} conf={:.2f} signal={} (attempt {})",
            decision.action,
            decision.confidence,
            alert.signal,
            attempt,
        )
        return decision

    # All retries exhausted — use smart fallback instead of blind skip
    logger.error(
        "All {} LLM attempts failed for {} {} — using smart fallback. Last error: {}",
        _MAX_RETRIES, alert.symbol, alert.signal, last_error,
    )
    decision = _smart_fallback(alert, last_error)
    decision = _apply_policy(decision, s)

    logger.info(
        "Fallback decision: action={} conf={:.2f} signal={}",
        decision.action,
        decision.confidence,
        alert.signal,
    )
    return decision


__all__ = ["evaluate_signal"]
