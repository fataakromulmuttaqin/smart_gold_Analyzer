"""cTrader Open API executor — headless Linux compatible.

Connects to IC Markets (or any cTrader broker) via JSON WebSocket.
No GUI, no Wine, no MT5 terminal needed. Pure Python + asyncio.

Order sizing uses the centralised ``app.risk`` module (same as MT5Executor):
  * ``StopCalculator`` selects SL distance (hybrid ATR-bounded PSAR)
  * ``position_sizer.compute_lot`` translates risk_pct + SL → lot size

ENV knobs (see .env.example for full docs):
    CTRADER_ENABLED / CTRADER_CLIENT_ID / CTRADER_CLIENT_SECRET
    CTRADER_ACCESS_TOKEN / CTRADER_ACCOUNT_ID / CTRADER_SYMBOL
    CTRADER_DEMO_MODE / CTRADER_LABEL
    SL_POLICY / SL_MIN_ATR_MULT / SL_MAX_ATR_MULT / SL_ATR_MULT
    RISK_PER_TRADE_PCT / RISK_PER_TRADE_PCT_REDUCE
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.config.settings import Settings, get_settings
from app.executor.base import ExecutionResult
from app.executor.ctrader_client import (
    CTRADER_DEMO_HOST,
    CTRADER_LIVE_HOST,
    CTraderAPIError,
    CTraderClient,
    CTraderConfig,
    TRADE_SIDE_BUY,
    TRADE_SIDE_SELL,
)
from app.models.schemas import LLMDecision, TradingViewAlert
from app.risk import build_default_stop_calculator
from app.risk.position_sizer import compute_lot
from app.utils.logging import logger


class CTraderExecutor:
    """Async cTrader executor — places orders via Open API JSON WebSocket.

    Lifecycle:
      1. ``_lazy_connect()`` — called on first execute(). Establishes WS,
         authenticates app + account, resolves symbol name → symbolId.
      2. ``execute(alert, decision)`` — computes SL/TP/lot, places market order.
      3. ``reconcile_breakeven()`` — periodic task to shift SL to breakeven.
      4. ``shutdown()`` — closes WebSocket cleanly (called on app shutdown).
    """

    name = "ctrader"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: CTraderClient | None = None
        self._connected = False
        self._init_error: str | None = None
        self._symbol_id: int | None = None
        self._symbol_digits: int = 5  # Default for XAUUSD (5 decimal places)
        self._symbol_lot_size: int = 100  # Volume units per 1 lot (cTrader convention)
        self._symbol_min_volume: int = 100  # Min volume in units (= 0.01 lot)
        self._symbol_max_volume: int = 10000000  # Max volume in units
        self._symbol_step_volume: int = 100  # Step in units
        self._stop_calc = build_default_stop_calculator(self.settings)
        self._connect_lock = asyncio.Lock()

    # ──────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────
    async def _lazy_connect(self) -> bool:
        """Connect and authenticate if not already done. Thread-safe via lock."""
        if self._connected and self._client and self._client.is_connected:
            return True

        async with self._connect_lock:
            # Double-check after acquiring lock
            if self._connected and self._client and self._client.is_connected:
                return True

            s = self.settings
            if not s.ctrader_enabled:
                self._init_error = "CTRADER_ENABLED=false — executor inactive"
                logger.info(self._init_error)
                return False

            if not s.ctrader_is_configured:
                self._init_error = (
                    "cTrader not fully configured. Need: "
                    "CTRADER_CLIENT_ID, CTRADER_CLIENT_SECRET, "
                    "CTRADER_ACCESS_TOKEN, CTRADER_ACCOUNT_ID"
                )
                logger.warning(self._init_error)
                return False

            host = CTRADER_DEMO_HOST if s.ctrader_demo_mode else CTRADER_LIVE_HOST

            config = CTraderConfig(
                client_id=s.ctrader_client_id,
                client_secret=s.ctrader_client_secret,
                access_token=s.ctrader_access_token,
                account_id=int(s.ctrader_account_id),
                host=host,
            )

            self._client = CTraderClient(config)

            try:
                await self._client.connect()
            except CTraderAPIError as exc:
                self._init_error = f"cTrader connection failed: {exc}"
                logger.error(self._init_error)
                self._client = None
                return False
            except Exception as exc:  # noqa: BLE001
                self._init_error = f"cTrader unexpected error: {exc}"
                logger.error(self._init_error)
                self._client = None
                return False

            # Resolve symbol name → symbolId
            try:
                await self._resolve_symbol()
            except CTraderAPIError as exc:
                self._init_error = f"cTrader symbol resolution failed: {exc}"
                logger.error(self._init_error)
                await self._client.disconnect()
                self._client = None
                return False

            self._connected = True
            self._init_error = None
            logger.info(
                "cTrader: ready (host={} account={} symbol={} → id={})",
                host, s.ctrader_account_id, s.ctrader_symbol, self._symbol_id,
            )
            return True

    async def _resolve_symbol(self) -> None:
        """Find the symbolId for the configured symbol name (e.g. 'XAUUSD')."""
        assert self._client is not None
        symbol_name = self.settings.ctrader_symbol.upper()

        # Get light symbol list (name + id)
        symbols = await self._client.get_symbols_list()

        # Find by name match
        matched = None
        for sym in symbols:
            name = str(sym.get("symbolName", "")).upper()
            if name == symbol_name:
                matched = sym
                break

        if matched is None:
            # Try partial match (some brokers prefix, e.g. "XAUUSD.r")
            for sym in symbols:
                name = str(sym.get("symbolName", "")).upper()
                if symbol_name in name:
                    matched = sym
                    break

        if matched is None:
            raise CTraderAPIError(
                error_code="SYMBOL_NOT_FOUND",
                description=(
                    f"Symbol '{symbol_name}' not found in account. "
                    f"Available: {[s.get('symbolName') for s in symbols[:20]]}"
                ),
            )

        self._symbol_id = int(matched["symbolId"])

        # Fetch detailed info for lot sizing constraints
        try:
            details = await self._client.get_symbol_by_id([self._symbol_id])
            if details:
                d = details[0]
                self._symbol_digits = int(d.get("digits", 5))
                # Volume in cTrader is in "cents" — lotSize defines units per lot
                self._symbol_lot_size = int(d.get("lotSize", 100))
                self._symbol_min_volume = int(d.get("minVolume", 100))
                self._symbol_max_volume = int(d.get("maxVolume", 10000000))
                self._symbol_step_volume = int(d.get("stepVolume", 100))
                logger.debug(
                    "cTrader symbol detail: digits={} lotSize={} min={} max={} step={}",
                    self._symbol_digits, self._symbol_lot_size,
                    self._symbol_min_volume, self._symbol_max_volume, self._symbol_step_volume,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("cTrader: couldn't fetch symbol details, using defaults: {}", exc)

    async def shutdown(self) -> None:
        """Clean disconnect. Called on application shutdown."""
        if self._client:
            await self._client.disconnect()
            self._client = None
        self._connected = False

    # ──────────────────────────────────────────────────────────────────
    # Public: execute()
    # ──────────────────────────────────────────────────────────────────
    async def execute(
        self,
        alert: TradingViewAlert,
        decision: LLMDecision,
    ) -> ExecutionResult:
        """Place a market order on cTrader based on signal + LLM decision."""
        if decision.action not in {"execute", "reduce"}:
            return ExecutionResult(
                placed=False,
                note=f"skipping: decision.action='{decision.action}'",
            )

        if not await self._lazy_connect():
            return ExecutionResult(
                placed=False,
                note="ctrader not connected",
                error=self._init_error,
            )

        assert self._client is not None
        assert self._symbol_id is not None

        s = self.settings
        symbol_name = s.ctrader_symbol or alert.symbol

        # Side inference from signal name
        side_is_long = "long" in alert.signal or "bull" in alert.signal
        side = "long" if side_is_long else "short"
        trade_side = TRADE_SIDE_BUY if side_is_long else TRADE_SIDE_SELL

        # Get current price from trader info / we use alert.price as reference
        # (cTrader market order fills at best available, so no need for precise tick)
        entry_price = float(alert.price)

        # ── Stop distance via centralised calculator ─────────────────
        stop_result = self._stop_calc.calculate(
            side=side,
            entry_price=entry_price,
            atr=float(alert.atr) if alert.atr else None,
            psar=float(alert.psar) if alert.psar else None,
        )
        stop_distance = stop_result.distance

        if stop_distance <= 0:
            # Fallback: 200 pips for XAUUSD (= $2.00)
            stop_distance = 2.0

        rr = float(decision.suggested_rr) if decision.suggested_rr else s.sl_default_rr

        if side_is_long:
            sl_price = entry_price - stop_distance
            tp_price = entry_price + stop_distance * rr
        else:
            sl_price = entry_price + stop_distance
            tp_price = entry_price - stop_distance * rr

        # ── Risk-based sizing ────────────────────────────────────────
        risk_pct = (
            s.risk_per_trade_pct_reduce
            if decision.action == "reduce"
            else s.risk_per_trade_pct
        )

        # Try to get account equity from cTrader
        equity = s.plan_equity_hint  # Fallback
        try:
            trader_info = await self._client.get_trader_info()
            # Balance is returned in cents (integer) by some API versions
            raw_balance = trader_info.get("balance", 0)
            if raw_balance > 100000:
                # Likely in cents
                equity = float(raw_balance) / 100.0
            else:
                equity = float(raw_balance) if raw_balance else s.plan_equity_hint
        except Exception as exc:  # noqa: BLE001
            logger.warning("cTrader: couldn't fetch equity, using hint: {}", exc)

        # Build a mock symbol_info-like object for position_sizer
        symbol_info = _SymbolInfo(
            volume_min=self._symbol_min_volume / self._symbol_lot_size,  # Convert to lots
            volume_max=self._symbol_max_volume / self._symbol_lot_size,
            volume_step=self._symbol_step_volume / self._symbol_lot_size,
            trade_tick_value=1.0,  # XAUUSD: $1 per 0.01 move per lot
            trade_tick_size=0.01,
        )

        sizing = compute_lot(
            equity=equity,
            risk_pct=risk_pct,
            stop_distance=stop_distance,
            symbol_info=symbol_info,
            fixed_lot=s.ctrader_fixed_lot,
        )
        lot = sizing.lot

        if lot <= 0:
            return ExecutionResult(
                placed=False,
                error=f"computed lot {lot} ≤ 0 (equity={equity})",
            )

        # Convert lot to cTrader volume (in "units")
        # cTrader volume = lots × lotSize (e.g. 0.01 lot × 100 = 100 units)
        volume_units = int(round(lot * self._symbol_lot_size))
        # Ensure minimum
        volume_units = max(volume_units, self._symbol_min_volume)
        # Snap to step
        if self._symbol_step_volume > 0:
            volume_units = (volume_units // self._symbol_step_volume) * self._symbol_step_volume

        if volume_units <= 0:
            return ExecutionResult(
                placed=False,
                error=f"volume_units={volume_units} invalid after snapping",
            )

        # ── Place order ──────────────────────────────────────────────
        comment = f"sga:{alert.signal}:c{decision.confidence:.2f}"
        label = s.ctrader_label or "SmartGold"

        try:
            result = await self._client.place_market_order(
                symbol_id=self._symbol_id,
                side=trade_side,
                volume=volume_units,
                stop_loss=round(sl_price, self._symbol_digits),
                take_profit=round(tp_price, self._symbol_digits),
                comment=comment,
                label=label,
            )
        except CTraderAPIError as exc:
            return ExecutionResult(
                placed=False,
                error=f"cTrader order failed: {exc.error_code} — {exc.description}",
            )
        except Exception as exc:  # noqa: BLE001
            return ExecutionResult(
                placed=False,
                error=f"cTrader unexpected error: {exc}",
            )

        # Parse execution result
        order_id = result.get("orderId") or result.get("order", {}).get("orderId")
        position_id = result.get("positionId") or result.get("position", {}).get("positionId")
        exec_price = result.get("executionPrice") or entry_price
        # cTrader executionPrice might be in integer format (price × 10^digits)
        if isinstance(exec_price, int) and exec_price > 100000:
            exec_price = exec_price / (10 ** self._symbol_digits)

        actual_volume = result.get("filledVolume", volume_units)
        actual_lot = actual_volume / self._symbol_lot_size if self._symbol_lot_size else lot

        return ExecutionResult(
            placed=True,
            note=f"order filled on {symbol_name} via cTrader",
            order_id=int(order_id) if order_id else None,
            symbol=symbol_name,
            side="buy" if side_is_long else "sell",
            volume=float(actual_lot),
            entry_price=float(exec_price),
            stop_loss=round(sl_price, self._symbol_digits),
            take_profit=round(tp_price, self._symbol_digits),
            extra={
                "position_id": position_id,
                "rr_used": rr,
                "risk_pct_used": risk_pct,
                "sl_policy": stop_result.source,
                "sl_clipped": stop_result.was_clipped,
                "sl_atr_mult_effective": round(stop_result.atr_mult_effective, 3),
                "sizing_effective_risk_usd": round(sizing.effective_risk_usd, 2),
                "sizing_reason": sizing.reason,
                "volume_units": volume_units,
                "equity_used": round(equity, 2),
                "executor": "ctrader",
            },
        )

    # ──────────────────────────────────────────────────────────────────
    # Breakeven reconciler (called periodically by main.py background task)
    # ──────────────────────────────────────────────────────────────────
    async def reconcile_breakeven(self) -> int:
        """Scan open positions and shift SL to breakeven if triggered.

        Returns number of positions modified.
        """
        if not self.settings.sl_breakeven_enabled:
            return 0

        if not await self._lazy_connect():
            return 0

        assert self._client is not None

        from app.risk.breakeven import check_breakeven_long, check_breakeven_short

        try:
            positions = await self._client.get_open_positions()
        except Exception as exc:  # noqa: BLE001
            logger.warning("cTrader: failed to get positions for breakeven: {}", exc)
            return 0

        if not positions:
            return 0

        label_prefix = (self.settings.ctrader_label or "SmartGold").lower()
        modified = 0

        for pos in positions:
            # Only manage positions placed by SmartGold
            pos_label = str(pos.get("label", "")).lower()
            if label_prefix not in pos_label:
                continue

            # Extract position data
            position_id = pos.get("positionId")
            if not position_id:
                continue

            trade_side = pos.get("tradeSide", 0)
            is_long = trade_side == TRADE_SIDE_BUY

            entry_price = float(pos.get("entryPrice", 0))
            current_sl = float(pos.get("stopLoss", 0))
            # If prices are in integer format
            if entry_price > 100000:
                entry_price = entry_price / (10 ** self._symbol_digits)
            if current_sl > 100000:
                current_sl = current_sl / (10 ** self._symbol_digits)

            if entry_price <= 0 or current_sl <= 0:
                continue

            stop_distance = abs(entry_price - current_sl)
            if stop_distance <= 0:
                continue

            # Use a simple ATR estimate from stop_distance / atr_mult
            atr_estimate = stop_distance / self.settings.sl_atr_mult

            # Get current market price from the position's unrealized P&L
            # Or we can use entry + some estimate. Better: use alert price if recent.
            # For now, approximate with margin price field or skip if unavailable.
            current_price_raw = pos.get("currentPrice") or pos.get("moneyDigits")
            if not current_price_raw:
                continue
            current_price = float(current_price_raw)
            if current_price > 100000:
                current_price = current_price / (10 ** self._symbol_digits)

            if is_long:
                result = check_breakeven_long(
                    entry_price=entry_price,
                    current_price=current_price,
                    current_stop=current_sl,
                    atr=atr_estimate,
                    stop_distance=stop_distance,
                    trigger_r=self.settings.sl_breakeven_trigger_r,
                    buffer_atr_mult=self.settings.sl_breakeven_buffer_atr_mult,
                )
            else:
                result = check_breakeven_short(
                    entry_price=entry_price,
                    current_price=current_price,
                    current_stop=current_sl,
                    atr=atr_estimate,
                    stop_distance=stop_distance,
                    trigger_r=self.settings.sl_breakeven_trigger_r,
                    buffer_atr_mult=self.settings.sl_breakeven_buffer_atr_mult,
                )

            if not result.should_shift or result.new_stop is None:
                continue

            try:
                await self._client.amend_position_sl_tp(
                    position_id=int(position_id),
                    stop_loss=round(result.new_stop, self._symbol_digits),
                )
                logger.info(
                    "cTrader breakeven: shifted SL for position #{} to {:.5f} — {}",
                    position_id, result.new_stop, result.reason,
                )
                modified += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("cTrader: breakeven modify failed for #{}: {}", position_id, exc)

        return modified


class _SymbolInfo:
    """Minimal symbol info adapter for position_sizer.compute_lot()."""

    def __init__(
        self,
        volume_min: float,
        volume_max: float,
        volume_step: float,
        trade_tick_value: float,
        trade_tick_size: float,
    ) -> None:
        self.volume_min = volume_min
        self.volume_max = volume_max
        self.volume_step = volume_step
        self.trade_tick_value = trade_tick_value
        self.trade_tick_size = trade_tick_size


__all__ = ["CTraderExecutor"]
