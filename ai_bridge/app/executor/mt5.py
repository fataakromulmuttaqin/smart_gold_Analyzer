"""MetaTrader5 executor — opt-in, optional dependency.

The ``MetaTrader5`` package only runs on Windows (or Wine). On Linux VPS
it will fail to import. We handle that by:

  * lazy-importing inside methods,
  * catching ImportError + any runtime error during login,
  * returning an ExecutionResult with ``placed=False, error=...`` instead
    of raising.

Order sizing uses the centralised ``app.risk`` module:
  * ``StopCalculator`` selects SL distance (default: hybrid ATR-bounded PSAR)
  * ``position_sizer.compute_lot`` translates risk_pct + SL → lot size

ENV knobs (see .env.example for full list):
    MT5_ENABLED / MT5_LOGIN / MT5_PASSWORD / MT5_SERVER / MT5_SYMBOL
    MT5_FIXED_LOT / MT5_DEVIATION / MT5_MAGIC / MT5_FALLBACK_STOP_POINTS
    SL_POLICY / SL_MIN_ATR_MULT / SL_MAX_ATR_MULT / SL_ATR_MULT
    RISK_PER_TRADE_PCT / RISK_PER_TRADE_PCT_REDUCE
"""
from __future__ import annotations

from typing import Any

from app.config.settings import Settings, get_settings
from app.executor.base import ExecutionResult
from app.models.schemas import LLMDecision, TradingViewAlert
from app.risk import build_default_stop_calculator
from app.risk.position_sizer import compute_lot
from app.utils.logging import logger


class MT5Executor:
    """Thin wrapper over the MetaTrader5 sync SDK, run in a thread pool."""

    name = "mt5"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._mt5 = None
        self._initialised = False
        self._init_error: str | None = None
        self._stop_calc = build_default_stop_calculator(self.settings)

    # ──────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────
    def _lazy_init(self) -> bool:
        """Import MT5 and log in. Returns True if ready, False on any error.

        Never raises — safe to call repeatedly (idempotent).
        """
        if self._initialised:
            return self._init_error is None
        self._initialised = True

        s = self.settings
        if not s.mt5_enabled:
            self._init_error = "MT5_ENABLED=false — executor inactive"
            logger.info(self._init_error)
            return False

        try:
            import MetaTrader5 as mt5  # type: ignore[import-not-found]
        except ImportError as exc:
            self._init_error = (
                "MetaTrader5 Python package not available "
                f"(ImportError: {exc}). MT5 only runs on Windows / Wine."
            )
            logger.warning(self._init_error)
            return False

        self._mt5 = mt5

        try:
            ok = mt5.initialize(
                login=int(s.mt5_login) if s.mt5_login else None,
                password=s.mt5_password or None,
                server=s.mt5_server or None,
            )
        except Exception as exc:  # noqa: BLE001 — SDK raises various types
            self._init_error = f"mt5.initialize raised: {exc}"
            logger.error(self._init_error)
            return False

        if not ok:
            err = mt5.last_error() if hasattr(mt5, "last_error") else ("?", "?")
            self._init_error = f"mt5.initialize returned False: {err}"
            logger.error(self._init_error)
            return False

        logger.info(
            "MT5 initialised (server={} login={} sl_policy={})",
            s.mt5_server, s.mt5_login, s.sl_policy,
        )
        return True

    # ──────────────────────────────────────────────────────────────────
    # Symbol / tick helpers
    # ──────────────────────────────────────────────────────────────────
    def _symbol_info(self, symbol: str) -> Any | None:
        info = self._mt5.symbol_info(symbol)  # type: ignore[union-attr]
        if info is None:
            # Try to make it visible in Market Watch, then re-query.
            self._mt5.symbol_select(symbol, True)  # type: ignore[union-attr]
            info = self._mt5.symbol_info(symbol)  # type: ignore[union-attr]
        return info

    # ──────────────────────────────────────────────────────────────────
    # Public: execute()
    # ──────────────────────────────────────────────────────────────────
    async def execute(
        self,
        alert: TradingViewAlert,
        decision: LLMDecision,
    ) -> ExecutionResult:
        import asyncio
        return await asyncio.to_thread(self._execute_blocking, alert, decision)

    def _execute_blocking(
        self,
        alert: TradingViewAlert,
        decision: LLMDecision,
    ) -> ExecutionResult:
        if decision.action not in {"execute", "reduce"}:
            return ExecutionResult(
                placed=False,
                note=f"skipping: decision.action='{decision.action}'",
            )

        if not self._lazy_init():
            return ExecutionResult(
                placed=False,
                note="mt5 not initialised",
                error=self._init_error,
            )

        mt5 = self._mt5
        s = self.settings
        symbol = s.mt5_symbol or alert.symbol

        info = self._symbol_info(symbol)
        if info is None:
            return ExecutionResult(
                placed=False,
                error=f"symbol {symbol} not available in Market Watch",
            )

        tick = mt5.symbol_info_tick(symbol)  # type: ignore[union-attr]
        if tick is None:
            return ExecutionResult(
                placed=False,
                error=f"no tick for {symbol}",
            )

        # Side inference from the Pine signal name
        side_is_long = "long" in alert.signal or "bull" in alert.signal
        side = "long" if side_is_long else "short"
        price = tick.ask if side_is_long else tick.bid
        order_type = mt5.ORDER_TYPE_BUY if side_is_long else mt5.ORDER_TYPE_SELL

        # ── Stop distance via centralised calculator ──────────────────
        stop_result = self._stop_calc.calculate(
            side=side,
            entry_price=float(price),
            atr=float(alert.atr) if alert.atr else None,
            psar=float(alert.psar) if alert.psar else None,
        )
        stop_distance = stop_result.distance

        if stop_distance <= 0:
            # Absolute fallback to env-configured fixed points
            stop_distance = s.mt5_fallback_stop_points * (info.point or 0.01)

        rr = float(decision.suggested_rr) if decision.suggested_rr else 2.0

        if side_is_long:
            sl = price - stop_distance
            tp = price + stop_distance * rr
        else:
            sl = price + stop_distance
            tp = price - stop_distance * rr

        # ── Risk-based sizing ────────────────────────────────────────
        risk_pct = (
            s.risk_per_trade_pct_reduce
            if decision.action == "reduce"
            else s.risk_per_trade_pct
        )

        acct = mt5.account_info()  # type: ignore[union-attr]
        equity = float(acct.equity) if acct else 0.0

        sizing = compute_lot(
            equity=equity,
            risk_pct=risk_pct,
            stop_distance=stop_distance,
            symbol_info=info,
            fixed_lot=s.mt5_fixed_lot,
        )
        volume = sizing.lot

        if volume <= 0:
            return ExecutionResult(
                placed=False,
                error=f"computed volume {volume} ≤ 0 (equity={equity})",
            )

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": order_type,
            "price": float(price),
            "sl": float(sl),
            "tp": float(tp),
            "deviation": int(s.mt5_deviation),
            "magic": int(s.mt5_magic),
            "comment": f"sga:{alert.signal}:c{decision.confidence:.2f}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_FOK,
        }

        try:
            result = mt5.order_send(req)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            return ExecutionResult(placed=False, error=f"order_send raised: {exc}")

        if result is None:
            err = mt5.last_error() if hasattr(mt5, "last_error") else ("?", "?")
            return ExecutionResult(placed=False, error=f"order_send None: {err}")

        retcode = int(getattr(result, "retcode", -1))
        if retcode != mt5.TRADE_RETCODE_DONE:
            return ExecutionResult(
                placed=False,
                error=f"order_send retcode={retcode} comment={getattr(result, 'comment', '')}",
            )

        return ExecutionResult(
            placed=True,
            note=f"order filled on {symbol}",
            order_id=int(getattr(result, "order", 0)) or None,
            symbol=symbol,
            side="buy" if side_is_long else "sell",
            volume=float(volume),
            entry_price=float(price),
            stop_loss=float(sl),
            take_profit=float(tp),
            extra={
                "retcode": retcode,
                "deal": int(getattr(result, "deal", 0)),
                "comment": getattr(result, "comment", ""),
                "rr_used": rr,
                "risk_pct_used": risk_pct,
                "sl_policy": stop_result.source,
                "sl_clipped": stop_result.was_clipped,
                "sl_atr_mult_effective": round(stop_result.atr_mult_effective, 3),
                "sizing_effective_risk_usd": round(sizing.effective_risk_usd, 2),
                "sizing_reason": sizing.reason,
            },
        )

    # ──────────────────────────────────────────────────────────────────
    # Breakeven reconciler (called periodically by main.py background task)
    # ──────────────────────────────────────────────────────────────────
    async def reconcile_breakeven(self) -> int:
        """Scan open positions and shift SL to breakeven if triggered.

        Returns number of positions modified. Returns 0 if breakeven is
        disabled, MT5 not ready, or no positions open.
        """
        import asyncio

        if not self.settings.sl_breakeven_enabled:
            return 0

        return await asyncio.to_thread(self._reconcile_breakeven_blocking)

    def _reconcile_breakeven_blocking(self) -> int:
        if not self._lazy_init():
            return 0

        from app.risk.breakeven import (
            check_breakeven_long,
            check_breakeven_short,
        )

        mt5 = self._mt5
        s = self.settings
        positions = mt5.positions_get()  # type: ignore[union-attr]
        if not positions:
            return 0

        modified = 0
        for pos in positions:
            # Only manage positions with our magic number
            if int(getattr(pos, "magic", 0)) != int(s.mt5_magic):
                continue

            symbol = str(pos.symbol)
            tick = mt5.symbol_info_tick(symbol)  # type: ignore[union-attr]
            if tick is None:
                continue

            entry = float(pos.price_open)
            current_sl = float(pos.sl) if pos.sl else 0.0
            current = float(tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask)
            # Infer original stop distance from current SL
            if current_sl == 0:
                continue  # No SL set — skip
            stop_distance = abs(entry - current_sl)

            # We don't have live ATR — re-derive from a recent rates query
            rates = mt5.copy_rates_from_pos(  # type: ignore[union-attr]
                symbol, mt5.TIMEFRAME_H1, 0, 20
            )
            if rates is None or len(rates) < 14:
                continue

            # Simple ATR(14)
            import statistics
            trs = []
            for i in range(1, len(rates)):
                hi = float(rates[i]["high"])
                lo = float(rates[i]["low"])
                pc = float(rates[i - 1]["close"])
                trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
            atr = statistics.mean(trs[-14:])

            if pos.type == mt5.ORDER_TYPE_BUY:
                result = check_breakeven_long(
                    entry_price=entry,
                    current_price=current,
                    current_stop=current_sl,
                    atr=atr,
                    stop_distance=stop_distance,
                    trigger_r=s.sl_breakeven_trigger_r,
                    buffer_atr_mult=s.sl_breakeven_buffer_atr_mult,
                )
            else:
                result = check_breakeven_short(
                    entry_price=entry,
                    current_price=current,
                    current_stop=current_sl,
                    atr=atr,
                    stop_distance=stop_distance,
                    trigger_r=s.sl_breakeven_trigger_r,
                    buffer_atr_mult=s.sl_breakeven_buffer_atr_mult,
                )

            if not result.should_shift or result.new_stop is None:
                continue

            # Send modify request
            modify_req = {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": int(pos.ticket),
                "symbol": symbol,
                "sl": float(result.new_stop),
                "tp": float(pos.tp) if pos.tp else 0.0,
            }
            try:
                modify_res = mt5.order_send(modify_req)  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001
                logger.warning("Breakeven modify raised for #{}: {}", pos.ticket, exc)
                continue

            if modify_res is None:
                continue
            if int(getattr(modify_res, "retcode", -1)) == mt5.TRADE_RETCODE_DONE:
                logger.info(
                    "Breakeven: shifted SL for #{} ({}) to {:.4f} — {}",
                    pos.ticket, symbol, result.new_stop, result.reason,
                )
                modified += 1

        return modified


__all__ = ["MT5Executor"]
