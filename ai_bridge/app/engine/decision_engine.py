"""Decision engine: feeds signal + macro context into MiniMax, returns validated LLMDecision.

Safety rails:
  * If LLM errors or returns unparseable JSON, default to a safe SKIP
    decision so no bad trade is forwarded to execution.
  * Validate the action enum & confidence range.
  * Enforce ``MIN_CONFIDENCE``: if confidence below threshold, downgrade
    execute/reduce to skip.
"""
from __future__ import annotations

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


def _safe_skip(reason: str) -> LLMDecision:
    """Build a conservative SKIP decision for any error path."""
    return LLMDecision(
        action="skip",
        confidence=0.0,
        reasoning=f"AUTO-SKIP: {reason}",
        risk_notes="Decision engine fallback — no LLM decision was obtained.",
    )


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

    Never raises — all failures collapse to a safe SKIP.
    """
    s = settings or get_settings()

    if not s.llm_is_configured:
        return _safe_skip("LLM not configured (MINIMAX_API_KEY missing)")

    user_prompt = build_user_prompt(alert, context)

    try:
        async with MiniMaxClient(settings=s) as llm:
            resp = await llm.chat(
                [
                    ChatMessage(role="system", content=SYSTEM_PROMPT),
                    ChatMessage(role="user", content=user_prompt),
                ],
                json_mode=True,
            )
            raw = resp.as_json()
    except MiniMaxError as exc:
        logger.error("MiniMax call failed: {}", exc)
        return _safe_skip(f"LLM error: {exc}")
    except Exception as exc:  # noqa: BLE001 — absolute last-resort guard
        logger.exception("Unexpected decision engine error")
        return _safe_skip(f"unexpected error: {exc}")

    decision = _parse_and_validate(raw)
    decision = _apply_policy(decision, s)

    logger.info(
        "Decision: action={} conf={:.2f} signal={}",
        decision.action,
        decision.confidence,
        alert.signal,
    )
    return decision


__all__ = ["evaluate_signal"]
