"""cTrader MCP executor — headless Linux, pure HTTP, no approval wait.

Connects to IC Markets (or any cTrader broker) via the official cTrader
MCP server at mcp.ctrader.com. Uses standard Model Context Protocol
(JSON-RPC 2.0 over HTTP POST). No WebSocket, no Wine, no GUI.

All you need is a bearer token generated from cTrader platform settings.

Order sizing uses the centralised ``app.risk`` module (same as MT5Executor):
  * ``StopCalculator`` selects SL distance (hybrid ATR-bounded PSAR)
  * ``position_sizer.compute_lot`` translates risk_pct + SL → lot size

ENV knobs (see .env.example):
    CTRADER_ENABLED / CTRADER_TOKEN / CTRADER_SYMBOL
    CTRADER_FIXED_LOT / CTRADER_LABEL
    SL_POLICY / SL_MIN_ATR_MULT / SL_MAX_ATR_MULT / SL_ATR_MULT
    RISK_PER_TRADE_PCT / RISK_PER_TRADE_PCT_REDUCE
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.config.settings import Settings, get_settings
from app.executor.base import ExecutionResult
from app.executor.ctrader_client import CTraderMCPClient, CTraderMCPError
from app.models.schemas import LLMDecision, TradingViewAlert
from app.risk import build_default_stop_calculator
from app.risk.position_sizer import compute_lot
from app.utils.logging import logger


class CTraderExecutor:
    """Async cTrader executor via MCP (HTTP POST).

    Lifecycle:
      1. ``_lazy_connect()`` — called on first execute(). Initializes MCP
         session and discovers available tools.
      2. ``execute(alert, decision)`` — computes SL/TP/lot, places market order.
      3. ``reconcile_breakeven()`` — periodic task to shift SL to breakeven.
      4. ``shutdown()`` — closes HTTP client (called on app shutdown).
    """

    name = "ctrader"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: CTraderMCPClient | None = None
        self._connected = False
        self._init_error: str | None = None
        self._stop_calc = build_default_stop_calculator(self.settings)
        self._connect_lock = asyncio.Lock()

    # ──────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────
    async def _lazy_connect(self) -> bool:
        """Connect and discover tools if not already done."""
        if self._connected and self._client and self._client.is_connected:
            return True

        async with self._connect_lock:
            # Double-check after lock
            if self._connected and self._client and self._client.is_connected:
                return True

            s = self.settings
            if not s.ctrader_enabled:
                self._init_error = "CTRADER_ENABLED=false — executor inactive"
                logger.info(self._init_error)
                return False

            if not s.ctrader_is_configured:
                self._init_error = (
                    "cTrader not configured. Need CTRADER_TOKEN in .env. "
                    "Generate from cTrader platform → Settings → API/MCP access."
                )
                logger.warning(self._init_error)
                return False

            self._client = CTraderMCPClient(
                token=s.ctrader_token,
                timeout=30.0,
            )

            try:
                await self._client.connect()
            except CTraderMCPError as exc:
                self._init_error = f"cTrader MCP connection failed: {exc}"
                logger.error(self._init_error)
                await self._client.close()
                self._client = None
                return False
            except Exception as exc:  # noqa: BLE001
                self._init_error = f"cTrader MCP unexpected error: {exc}"
                logger.error(self._init_error)
                if self._client:
                    await self._client.close()
                self._client = None
                return False

            self._connected = True
            self._init_error = None
            logger.info(
                "cTrader MCP: ready (symbol={} tools={})",
                s.ctrader_symbol,
                self._client.available_tools,
            )
            return True

    async def shutdown(self) -> None:
        """Clean shutdown. Called on application shutdown."""
        if self._client:
            await self._client.close()
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
        """Place a market order via cTrader MCP."""
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

        s = self.settings
        symbol = s.ctrader_symbol or alert.symbol

        # Side inference from signal name
        side_is_long = "long" in alert.signal or "bull" in alert.signal
        side = "buy" if side_is_long else "sell"

        # Use alert price as reference (MCP market order fills at best available)
        entry_price = float(alert.price)

        # ── Stop distance via centralised calculator ─────────────────
        stop_result = self._stop_calc.calculate(
            side="long" if side_is_long else "short",
            entry_price=entry_price,
            atr=float(alert.atr) if alert.atr else None,
            psar=float(alert.psar) if alert.psar else None,
        )
        stop_distance = stop_result.distance

        if stop_distance <= 0:
            # Fallback: $2.00 for XAUUSD (~200 pips)
            stop_distance = 2.0

        rr = float(decision.suggested_rr) if decision.suggested_rr else s.sl_default_rr

        if side_is_long:
            sl_price = round(entry_price - stop_distance, 2)
            tp_price = round(entry_price + stop_distance * rr, 2)
        else:
            sl_price = round(entry_price + stop_distance, 2)
            tp_price = round(entry_price - stop_distance * rr, 2)

        # ── Risk-based sizing ────────────────────────────────────────
        risk_pct = (
            s.risk_per_trade_pct_reduce
            if decision.action == "reduce"
            else s.risk_per_trade_pct
        )

        # Try to get account equity from cTrader
        equity = s.plan_equity_hint  # Fallback
        try:
            account = await self._client.get_account_info()
            raw_equity = account.get("equity") or account.get("balance") or 0
            if isinstance(raw_equity, (int, float)) and raw_equity > 0:
                equity = float(raw_equity)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cTrader: couldn't fetch equity, using hint: {}", exc)

        # Compute lot (using simplified symbol info — no broker constraints available via MCP)
        sizing = compute_lot(
            equity=equity,
            risk_pct=risk_pct,
            stop_distance=stop_distance,
            symbol_info=None,  # Use defaults (0.01 min, 100 max, 0.01 step)
            fixed_lot=s.ctrader_fixed_lot,
        )
        volume = sizing.lot

        if volume <= 0:
            return ExecutionResult(
                placed=False,
                error=f"computed volume {volume} <= 0 (equity={equity})",
            )

        # ── Place order via MCP ──────────────────────────────────────
        comment = f"sga:{alert.signal}:c{decision.confidence:.2f}"
        label = s.ctrader_label or "SmartGold"

        try:
            result = await self._client.place_market_order(
                symbol=symbol,
                side=side,
                volume=volume,
                stop_loss=sl_price,
                take_profit=tp_price,
                comment=comment,
                label=label,
            )
        except CTraderMCPError as exc:
            return ExecutionResult(
                placed=False,
                error=f"cTrader MCP order failed: [{exc.code}] {exc.message}",
            )
        except Exception as exc:  # noqa: BLE001
            return ExecutionResult(
                placed=False,
                error=f"cTrader unexpected error: {exc}",
            )

        # Parse result — structure depends on server implementation
        order_id = (
            result.get("orderId")
            or result.get("order_id")
            or result.get("id")
            or result.get("positionId")
            or result.get("position_id")
        )
        position_id = (
            result.get("positionId")
            or result.get("position_id")
            or order_id
        )
        exec_price = (
            result.get("executionPrice")
            or result.get("execution_price")
            or result.get("price")
            or entry_price
        )
        filled_volume = result.get("filledVolume") or result.get("volume") or volume

        return ExecutionResult(
            placed=True,
            note=f"order filled on {symbol} via cTrader MCP",
            order_id=int(order_id) if order_id else None,
            symbol=symbol,
            side=side,
            volume=float(filled_volume),
            entry_price=float(exec_price),
            stop_loss=sl_price,
            take_profit=tp_price,
            extra={
                "position_id": position_id,
                "rr_used": rr,
                "risk_pct_used": risk_pct,
                "sl_policy": stop_result.source,
                "sl_clipped": stop_result.was_clipped,
                "sl_atr_mult_effective": round(stop_result.atr_mult_effective, 3),
                "sizing_effective_risk_usd": round(sizing.effective_risk_usd, 2),
                "sizing_reason": sizing.reason,
                "equity_used": round(equity, 2),
                "executor": "ctrader_mcp",
                "raw_result": result,
            },
        )

    # ──────────────────────────────────────────────────────────────────
    # Breakeven reconciler
    # ──────────────────────────────────────────────────────────────────
    async def reconcile_breakeven(self) -> int:
        """Scan open positions and shift SL to breakeven if triggered.

        Returns number of positions modified.

        Session recovery:
          If cTrader returns a session-expired error, we reset the connection
          state so the next call will re-initialize. We DON'T retry immediately
          here — the caller (main.py breakeven loop) will retry on the next
          cycle. This prevents hammering the server every 10s when the session
          is persistently dead.
        """
        if not self.settings.sl_breakeven_enabled:
            return 0

        if not await self._lazy_connect():
            return 0

        assert self._client is not None

        from app.risk.breakeven import check_breakeven_long, check_breakeven_short

        try:
            positions = await self._client.get_positions()
        except CTraderMCPError as exc:
            # If session expired, the client's call_tool already tried to
            # reconnect once. If it still fails, reset our state so the
            # NEXT cycle starts fresh instead of looping on a dead session.
            if self._client._is_session_expired(exc):
                logger.warning(
                    "cTrader breakeven: session expired after reconnect attempt — "
                    "resetting state (will retry next cycle): {}", exc,
                )
                self._connected = False
            else:
                logger.warning("cTrader: failed to get positions for breakeven: {}", exc)
            return 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("cTrader: breakeven positions error: {}", exc)
            return 0

        if not positions:
            return 0

        label_prefix = (self.settings.ctrader_label or "SmartGold").lower()
        modified = 0

        for pos in positions:
            # Only manage positions placed by SmartGold
            pos_label = str(pos.get("label", "") or pos.get("comment", "")).lower()
            if label_prefix not in pos_label and "sga:" not in pos_label:
                continue

            # Extract position data (flexible key names)
            position_id = pos.get("positionId") or pos.get("position_id") or pos.get("id")
            if not position_id:
                continue

            # Determine side
            pos_side = str(pos.get("side", "") or pos.get("tradeSide", "")).lower()
            is_long = pos_side in ("buy", "long", "1")

            entry_price = float(
                pos.get("entryPrice") or pos.get("entry_price")
                or pos.get("openPrice") or pos.get("open_price") or 0
            )
            current_sl = float(
                pos.get("stopLoss") or pos.get("stop_loss") or pos.get("sl") or 0
            )
            current_price = float(
                pos.get("currentPrice") or pos.get("current_price")
                or pos.get("lastPrice") or pos.get("bid") or pos.get("ask") or 0
            )

            if entry_price <= 0 or current_sl <= 0 or current_price <= 0:
                continue

            stop_distance = abs(entry_price - current_sl)
            if stop_distance <= 0:
                continue

            # Estimate ATR from stop distance
            atr_estimate = stop_distance / self.settings.sl_atr_mult

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
                await self._client.modify_position(
                    position_id=position_id,
                    stop_loss=round(result.new_stop, 2),
                )
                logger.info(
                    "cTrader breakeven: shifted SL for #{} to {:.2f} — {}",
                    position_id, result.new_stop, result.reason,
                )
                modified += 1
            except CTraderMCPError as exc:
                if self._client._is_session_expired(exc):
                    logger.warning(
                        "cTrader breakeven: session died during modify — "
                        "resetting (will retry next cycle)",
                    )
                    self._connected = False
                    break
                logger.warning("cTrader: breakeven modify failed for #{}: {}", position_id, exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("cTrader: breakeven modify failed for #{}: {}", position_id, exc)

        return modified


__all__ = ["CTraderExecutor"]
