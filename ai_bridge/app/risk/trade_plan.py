"""Trade plan builder — computes entry/SL/TP/sizing for every signal.

This module is the single source of truth for "what would this trade look
like if we executed it right now". It runs for EVERY signal (execute,
reduce, or skip) so the dashboard can display the planned entry, stop
loss, take profit, and risk — even in signal-only mode where no broker
order is actually placed.

Decoupling plan-building from broker execution means:

  * The UI shows meaningful rows for all signals, not just executed ones.
  * The user can compare the planned trade vs. what the market did.
  * When the user records the outcome, we can auto-compute PnL from the
    stored entry_price + the exit_price they enter.
  * The live MT5 executor can reuse the same plan to place its order —
    no duplicated logic.

Key design choices:

  * ``entry_price`` = the ``price`` field on the incoming alert, i.e. the
    close of the H1 bar that triggered. This is deterministic and exactly
    matches what the Pine script saw. For live MT5 execution the actual
    fill may differ by 1-3 ticks (slippage); that's tracked separately in
    ``ExecutionResult.entry_price``.
  * ``stop_loss`` uses the project's :mod:`app.risk.stop_calculator`
    hybrid ATR-bounded PSAR policy (or whatever ``SL_POLICY`` is set to).
  * ``take_profit`` uses ``decision.suggested_rr`` if present, else
    ``settings.sl_default_rr`` (default 2.0). None if neither is valid.
  * ``lot_estimate`` requires a known equity. We don't query MT5 here —
    if the caller provides ``equity_hint`` we compute it, else leave None.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from app.config.settings import Settings, get_settings
from app.models.schemas import LLMDecision, TradingViewAlert
from app.risk.position_sizer import compute_lot
from app.risk.stop_calculator import build_default_stop_calculator
from app.utils.logging import logger


@dataclass(slots=True)
class TradePlan:
    """Everything the UI + executor need to know about a planned trade."""

    # ── Direction inferred from signal name ───────────────────────────
    side: str                       # "long" | "short"

    # ── Core prices ────────────────────────────────────────────────────
    entry_price: float
    stop_loss: float
    take_profit: float | None       # None if RR not set / invalid

    # ── Distances (always positive) ───────────────────────────────────
    stop_distance: float            # |entry - stop_loss|
    take_distance: float | None     # |take_profit - entry|
    risk_reward: float | None       # take_distance / stop_distance

    # ── Stop-loss metadata ────────────────────────────────────────────
    stop_policy: str                # "hybrid" | "psar" | "atr"
    stop_source: str                # e.g. "hybrid_atr_psar", "psar_no_atr"
    stop_was_clipped: bool          # True if hybrid hit the [min,max] bound
    stop_atr_mult: float            # Effective ATR multiple

    # ── Sizing (optional — only if equity_hint provided) ──────────────
    risk_pct: float                 # Risk % used (or would use)
    risk_usd_estimate: float | None # equity × risk_pct / 100
    lot_estimate: float | None      # Computed lot (broker-valid)

    # ── Notes ─────────────────────────────────────────────────────────
    notes: list[str]                # Free-form warnings (e.g. "ATR missing")

    def to_dict(self) -> dict:
        """JSON-serialisable dict for SQLite/API."""
        return asdict(self)


def _infer_side(signal: str) -> str:
    """Pine signal name → long / short."""
    s = signal.lower()
    if "long" in s or "bull" in s:
        return "long"
    if "short" in s or "bear" in s:
        return "short"
    # Unknown — default to long so we still produce a plan; caller can override.
    return "long"


def build_plan(
    alert: TradingViewAlert,
    decision: LLMDecision,
    settings: Settings | None = None,
    equity_hint: float | None = None,
    default_rr: float = 2.0,
) -> TradePlan:
    """Build a :class:`TradePlan` for the given signal + decision.

    Args:
        alert:        Incoming TradingView alert (provides price, ATR, PSAR).
        decision:     LLM output (provides suggested_rr, suggested_stop_atr_mult).
        settings:     App settings (defaults to get_settings()).
        equity_hint:  Optional equity for lot sizing. If None, lot_estimate
                      stays None — the UI will just omit that column.
        default_rr:   RR used when ``decision.suggested_rr`` is None.

    This function is infallible — any internal failure falls back to a
    minimal plan with diagnostic ``notes``. It never raises.
    """
    s = settings or get_settings()
    notes: list[str] = []

    # ── Direction ─────────────────────────────────────────────────────
    side = _infer_side(alert.signal)

    # ── Entry ─────────────────────────────────────────────────────────
    entry_price = float(alert.price)

    # ── Stop-loss ─────────────────────────────────────────────────────
    stop_calc = build_default_stop_calculator(s)
    atr_val = float(alert.atr) if alert.atr and alert.atr > 0 else None
    psar_val = float(alert.psar) if getattr(alert, "psar", None) else None

    try:
        stop_result = stop_calc.calculate(
            side=side,
            entry_price=entry_price,
            atr=atr_val,
            psar=psar_val,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("TradePlan: stop calculator raised {}, using fallback", exc)
        notes.append(f"stop_calc_error: {exc}")
        from app.risk.stop_calculator import StopResult
        stop_result = StopResult(
            distance=max(atr_val * 1.5 if atr_val else 20.0, 0.01),
            source="fallback",
        )

    stop_distance = max(stop_result.distance, 1e-9)
    if side == "long":
        stop_loss = entry_price - stop_distance
    else:
        stop_loss = entry_price + stop_distance

    # ── Take-profit (RR based) ────────────────────────────────────────
    rr = decision.suggested_rr if decision.suggested_rr else default_rr
    if rr is None or rr <= 0:
        take_profit: float | None = None
        take_distance: float | None = None
        risk_reward: float | None = None
        notes.append("no_take_profit: rr not set")
    else:
        risk_reward = float(rr)
        take_distance = stop_distance * risk_reward
        if side == "long":
            take_profit = entry_price + take_distance
        else:
            take_profit = entry_price - take_distance

    # ── Risk sizing ───────────────────────────────────────────────────
    risk_pct = (
        s.risk_per_trade_pct_reduce
        if decision.action == "reduce"
        else s.risk_per_trade_pct
    )

    risk_usd_estimate: float | None = None
    lot_estimate: float | None = None
    if equity_hint is not None and equity_hint > 0:
        try:
            sz = compute_lot(
                equity=equity_hint,
                risk_pct=risk_pct,
                stop_distance=stop_distance,
                symbol_info=None,          # We don't have MT5 here — use defaults
                fixed_lot=s.mt5_fixed_lot,
            )
            risk_usd_estimate = round(sz.risk_usd, 2)
            lot_estimate = round(sz.lot, 4)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"sizing_error: {exc}")
    else:
        notes.append("no_equity_hint: lot/risk_usd not computed")

    if stop_result.was_clipped:
        notes.append(f"stop_clipped: {stop_result.meta.get('clip_reason', '?')}")
    if atr_val is None:
        notes.append("atr_missing")
    if psar_val is None:
        notes.append("psar_missing")

    return TradePlan(
        side=side,
        entry_price=round(entry_price, 4),
        stop_loss=round(stop_loss, 4),
        take_profit=round(take_profit, 4) if take_profit is not None else None,
        stop_distance=round(stop_distance, 4),
        take_distance=round(take_distance, 4) if take_distance is not None else None,
        risk_reward=round(risk_reward, 2) if risk_reward is not None else None,
        stop_policy=str(s.sl_policy),
        stop_source=str(stop_result.source),
        stop_was_clipped=bool(stop_result.was_clipped),
        stop_atr_mult=round(stop_result.atr_mult_effective, 3),
        risk_pct=round(risk_pct, 3),
        risk_usd_estimate=risk_usd_estimate,
        lot_estimate=lot_estimate,
        notes=notes,
    )


def compute_outcome_pnl(
    *,
    side: str,
    entry_price: float,
    exit_price: float,
    stop_distance: float,
    contract_value_per_point: float = 100.0,  # XAUUSD standard: $100/$1 move/lot
    lot: float | None = None,
) -> tuple[float, float | None]:
    """Compute (pnl_r, pnl_usd) from an entry + exit price.

    Args:
        side:           "long" or "short".
        entry_price:    Filled/planned entry.
        exit_price:     User-reported exit (or broker close).
        stop_distance:  Original stop distance (for R multiple).
        contract_value_per_point: $/price unit/lot. Defaults to 100 (XAUUSD).
        lot:            If provided, compute account-currency PnL. Else None.

    Returns:
        (pnl_r, pnl_usd). ``pnl_usd`` is None if ``lot`` not provided.
    """
    if side == "long":
        pnl_points = exit_price - entry_price
    else:
        pnl_points = entry_price - exit_price

    pnl_r = pnl_points / stop_distance if stop_distance > 0 else 0.0
    pnl_usd = None
    if lot is not None and lot > 0:
        pnl_usd = pnl_points * contract_value_per_point * lot

    return round(pnl_r, 3), (round(pnl_usd, 2) if pnl_usd is not None else None)


__all__ = ["TradePlan", "build_plan", "compute_outcome_pnl"]
