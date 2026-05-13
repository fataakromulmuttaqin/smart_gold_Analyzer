"""Risk management module.

Centralises stop-loss calculation, position sizing, and trade lifecycle
policies (breakeven shift, partial close, trailing). Used by both the
live MT5 executor and the backtest simulator so that backtested
performance matches live behaviour.

Key modules:
    * stop_calculator: SL distance policies (fixed ATR, PSAR,
      hybrid ATR-bounded PSAR)
    * position_sizer:  risk-based lot sizing
    * breakeven:       breakeven trigger logic
"""

from app.risk.stop_calculator import (
    ATRStop,
    HybridATRPsarStop,
    PSARStop,
    StopCalculator,
    StopResult,
    build_default_stop_calculator,
)
from app.risk.trade_plan import TradePlan, build_plan, compute_outcome_pnl

__all__ = [
    "ATRStop",
    "HybridATRPsarStop",
    "PSARStop",
    "StopCalculator",
    "StopResult",
    "build_default_stop_calculator",
    "TradePlan",
    "build_plan",
    "compute_outcome_pnl",
]
