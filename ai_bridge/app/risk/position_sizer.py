"""Risk-based position sizing.

Translates (equity, risk_pct, stop_distance) into a lot size that risks
exactly ``risk_pct`` of equity if the stop is hit. Handles broker lot
constraints (min/max/step) and graceful fallbacks for missing data.

Formula:
    risk_usd        = equity × risk_pct / 100
    money_per_pt_per_lot = tick_value / tick_size    (e.g. XAUUSD ≈ $1/point/lot)
    raw_lot         = risk_usd / (stop_distance × money_per_pt_per_lot)
    final_lot       = clamp(round_to_step(raw_lot), volume_min, volume_max)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.utils.logging import logger


@dataclass(slots=True)
class SizingResult:
    lot: float
    risk_usd: float
    effective_risk_usd: float
    was_clipped: bool
    reason: str = ""


def compute_lot(
    *,
    equity: float,
    risk_pct: float,
    stop_distance: float,
    symbol_info: Any = None,
    fixed_lot: float = 0.0,
    default_money_per_unit: float = 100.0,  # For XAUUSD standard: $1/$0.01 move/lot
) -> SizingResult:
    """Compute a broker-valid lot size.

    Args:
        equity: Account equity in account currency (USD typically).
        risk_pct: Percent of equity to risk per trade (e.g. 1.0 = 1%).
        stop_distance: Stop loss distance in price units (positive).
        symbol_info: MT5 symbol_info object with volume_min/max/step/
                     trade_tick_value/trade_tick_size attributes.
                     If None, uses permissive defaults (0.01 / 100 / 0.01).
        fixed_lot: If > 0, override sizing and return this lot size.
        default_money_per_unit: Dollars per 1 price unit of movement per
                                1 standard lot. For XAUUSD typically 100.

    Returns:
        SizingResult with final lot + diagnostics.
    """
    # Read symbol constraints (with sensible defaults if no info provided)
    if symbol_info is not None:
        vol_min = float(getattr(symbol_info, "volume_min", 0.01) or 0.01)
        vol_max = float(getattr(symbol_info, "volume_max", 100.0) or 100.0)
        vol_step = float(getattr(symbol_info, "volume_step", 0.01) or 0.01)
        tick_value = float(getattr(symbol_info, "trade_tick_value", 1.0) or 1.0)
        tick_size = float(getattr(symbol_info, "trade_tick_size", 0.01) or 0.01)
        money_per_unit = (tick_value / tick_size) if tick_size > 0 else default_money_per_unit
    else:
        vol_min, vol_max, vol_step = 0.01, 100.0, 0.01
        money_per_unit = default_money_per_unit

    # Fixed lot override
    if fixed_lot > 0:
        final = max(vol_min, min(fixed_lot, vol_max))
        effective = final * stop_distance * money_per_unit
        return SizingResult(
            lot=final,
            risk_usd=effective,
            effective_risk_usd=effective,
            was_clipped=(final != fixed_lot),
            reason="fixed_lot_override",
        )

    if stop_distance <= 0:
        logger.warning(
            "PositionSizer: stop_distance={} invalid, using volume_min", stop_distance,
        )
        return SizingResult(
            lot=vol_min,
            risk_usd=0.0,
            effective_risk_usd=0.0,
            was_clipped=True,
            reason="invalid_stop_distance",
        )

    risk_usd = equity * (risk_pct / 100.0)
    raw_lot = risk_usd / (stop_distance * money_per_unit)

    # Round DOWN to step (conservative — better undersized than overrisk)
    if vol_step > 0:
        raw_lot = int(raw_lot / vol_step) * vol_step

    final_lot = max(vol_min, min(raw_lot, vol_max))
    was_clipped = abs(final_lot - raw_lot) > 1e-9
    effective_risk = final_lot * stop_distance * money_per_unit

    reason = "ok"
    if raw_lot < vol_min:
        reason = "below_min_clamped_up"
    elif raw_lot > vol_max:
        reason = "above_max_clamped_down"
    elif was_clipped:
        reason = "step_rounding"

    return SizingResult(
        lot=final_lot,
        risk_usd=risk_usd,
        effective_risk_usd=effective_risk,
        was_clipped=was_clipped,
        reason=reason,
    )


__all__ = ["SizingResult", "compute_lot"]
