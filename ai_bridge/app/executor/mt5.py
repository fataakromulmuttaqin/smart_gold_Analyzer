"""MetaTrader5 executor — opt-in, optional dependency.

The ``MetaTrader5`` package only runs on Windows (or Wine). On Linux VPS
it will fail to import. We handle that by:

  * lazy-importing inside methods,
  * catching ImportError + any runtime error during login,
  * returning an ExecutionResult with ``placed=False, error=...`` instead
    of raising.

Order sizing uses the decision's ``suggested_stop_atr_mult`` and
``suggested_rr`` together with the alert's ATR. If ATR is missing we
fall back to a fixed-pip stop (configurable via env).

ENV:
    MT5_ENABLED=true|false   (master switch; default false)
    MT5_LOGIN=<int>          (broker account login)
    MT5_PASSWORD=<str>
    MT5_SERVER=<str>         (e.g. "Exness-MT5Trial8")
    MT5_SYMBOL=<str>         (default: "XAUUSD" — override per broker)
    MT5_RISK_PCT=1.0         (percent of equity to risk per trade)
    MT5_FIXED_LOT=0.0        (if >0, override sizing with this lot)
    MT5_DEVIATION=20         (max slippage, points)
    MT5_MAGIC=260512         (order magic number for later identification)
    MT5_FALLBACK_STOP_POINTS=2000   (if ATR missing; gold 1pt ≈ $0.01)
"""
from __future__ import annotations

from typing import Any

from app.config.settings import Settings, get_settings
from app.executor.base import ExecutionResult
from app.models.schemas import LLMDecision, TradingViewAlert
from app.utils.logging import logger


class MT5Executor:
    """Thin wrapper over the MetaTrader5 sync SDK, run in a thread pool."""

    name = "mt5"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._mt5 = None
        self._initialised = False
        self._init_error: str | None = None

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

        logger.info("MT5 initialised (server={} login={})", s.mt5_server, s.mt5_login)
        return True

    # ──────────────────────────────────────────────────────────────────
    # Sizing / price helpers
    # ──────────────────────────────────────────────────────────────────
    def _symbol_info(self, symbol: str) -> Any | None:
        info = self._mt5.symbol_info(symbol)  # type: ignore[union-attr]
        if info is None:
            # Try to make it visible in Market Watch, then re-query.
            self._mt5.symbol_select(symbol, True)  # type: ignore[union-attr]
            info = self._mt5.symbol_info(symbol)  # type: ignore[union-attr]
        return info

    def _compute_volume(
        self,
        *,
        info: Any,
        equity: float,
        risk_pct: float,
        stop_distance: float,
    ) -> float:
        """Return a broker-valid lot size that risks ≤ risk_pct of equity.

        Clamped to [volume_min, volume_max] and rounded to volume_step.
        """
        # Fixed override wins
        if self.settings.mt5_fixed_lot > 0:
            return max(
                info.volume_min,
                min(self.settings.mt5_fixed_lot, info.volume_max),
            )

        if stop_distance <= 0:
            return info.volume_min

        # Risk in account currency
        risk_amount = equity * (risk_pct / 100.0)
        # Money per 1 lot for 1 price unit of movement.
        # For XAUUSD: tick_value per tick_size. Approximate dollars-per-1.0-move.
        tick_value = float(info.trade_tick_value) if info.trade_tick_value else 1.0
        tick_size = float(info.trade_tick_size) if info.trade_tick_size else 0.01
        dollars_per_unit = (tick_value / tick_size) if tick_size > 0 else 1.0

        raw = risk_amount / (stop_distance * dollars_per_unit)
        step = float(info.volume_step or 0.01)
        if step > 0:
            raw = (int(raw / step)) * step
        return max(info.volume_min, min(raw, info.volume_max))

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
        price = tick.ask if side_is_long else tick.bid
        order_type = mt5.ORDER_TYPE_BUY if side_is_long else mt5.ORDER_TYPE_SELL

        # Stop distance: prefer decision.suggested_stop_atr_mult × alert.atr.
        # Fall back to env points if ATR missing.
        stop_distance: float
        if decision.suggested_stop_atr_mult and alert.atr:
            stop_distance = float(decision.suggested_stop_atr_mult) * float(alert.atr)
        elif alert.atr:
            stop_distance = 1.5 * float(alert.atr)  # sensible default
        else:
            # Each "point" on XAUUSD ≈ 0.01 for most brokers.
            stop_distance = s.mt5_fallback_stop_points * (info.point or 0.01)

        rr = float(decision.suggested_rr) if decision.suggested_rr else 2.0

        if side_is_long:
            sl = price - stop_distance
            tp = price + stop_distance * rr
        else:
            sl = price + stop_distance
            tp = price - stop_distance * rr

        # Risk-based sizing
        risk_pct = s.mt5_risk_pct
        if decision.action == "reduce":
            risk_pct = risk_pct / 2.0  # halve size for reduce

        acct = mt5.account_info()  # type: ignore[union-attr]
        equity = float(acct.equity) if acct else 0.0
        volume = self._compute_volume(
            info=info,
            equity=equity,
            risk_pct=risk_pct,
            stop_distance=stop_distance,
        )

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
            },
        )


__all__ = ["MT5Executor"]
