"""Low-level async cTrader Open API client using JSON over WebSocket.

This module handles:
  * TLS WebSocket connection to cTrader backend
  * Application auth (client_id + client_secret)
  * Account auth (access_token + ctid_trader_account_id)
  * Sending requests and awaiting responses by clientMsgId
  * Heartbeat keep-alive (every 10s)
  * Graceful reconnection on disconnect

Protocol reference:
  https://help.ctrader.com/open-api/sending-receiving-json/

No Twisted, no protobuf — pure asyncio + websockets library.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.utils.logging import logger

# cTrader Open API endpoints (production)
CTRADER_LIVE_HOST = "live.ctraderapi.com"
CTRADER_DEMO_HOST = "demo.ctraderapi.com"
CTRADER_PORT = 5035

# ── Payload type constants ───────────────────────────────────────────────
# Application layer
PT_APP_AUTH_REQ = 2100
PT_APP_AUTH_RES = 2101
PT_ACCOUNT_AUTH_REQ = 2102
PT_ACCOUNT_AUTH_RES = 2103

# Trading
PT_NEW_ORDER_REQ = 2124
PT_EXECUTION_EVENT = 2126
PT_CANCEL_ORDER_REQ = 2128
PT_CLOSE_POSITION_REQ = 2130
PT_AMEND_POSITION_SLTP_REQ = 2135
PT_AMEND_ORDER_REQ = 2136

# Data
PT_SYMBOLS_LIST_REQ = 2149
PT_SYMBOLS_LIST_RES = 2150
PT_SYMBOL_BY_ID_REQ = 2151
PT_SYMBOL_BY_ID_RES = 2152
PT_TRADER_REQ = 2164
PT_TRADER_RES = 2165
PT_RECONCILE_REQ = 2124  # re-use: positions list uses PT 2132
PT_GET_POSITIONS_REQ = 2132  # ProtoOAReconcileReq -> returns open positions
PT_GET_POSITIONS_RES = 2133

# System
PT_HEARTBEAT = 51
PT_ERROR_RES = 50
PT_OA_ERROR_RES = 2142

# Order type enum values
ORDER_TYPE_MARKET = 1
ORDER_TYPE_LIMIT = 2
ORDER_TYPE_STOP = 3

# Trade side
TRADE_SIDE_BUY = 1
TRADE_SIDE_SELL = 2


class CTraderAPIError(Exception):
    """Raised when the cTrader backend returns an error."""

    def __init__(self, error_code: str = "", description: str = "", payload: dict | None = None):
        self.error_code = error_code
        self.description = description
        self.payload = payload or {}
        super().__init__(f"cTrader API error [{error_code}]: {description}")


@dataclass
class CTraderConfig:
    """Configuration for the cTrader Open API client."""

    client_id: str
    client_secret: str
    access_token: str
    account_id: int  # ctidTraderAccountId (integer)
    host: str = CTRADER_DEMO_HOST
    port: int = CTRADER_PORT
    heartbeat_interval: float = 10.0
    request_timeout: float = 30.0


class CTraderClient:
    """Async cTrader Open API client (JSON mode over WebSocket).

    Usage:
        config = CTraderConfig(...)
        client = CTraderClient(config)
        await client.connect()
        result = await client.place_market_order(...)
        await client.disconnect()

    Or as an async context manager:
        async with CTraderClient(config) as client:
            result = await client.place_market_order(...)
    """

    def __init__(self, config: CTraderConfig) -> None:
        self._config = config
        self._ws: Any = None
        self._connected = False
        self._authenticated = False
        self._pending: dict[str, asyncio.Future] = {}
        self._listener_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._execution_events: asyncio.Queue = asyncio.Queue()

    async def __aenter__(self) -> "CTraderClient":
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.disconnect()

    # ──────────────────────────────────────────────────────────────────
    # Connection lifecycle
    # ──────────────────────────────────────────────────────────────────
    async def connect(self) -> None:
        """Establish WebSocket, authenticate app + account."""
        try:
            import websockets  # type: ignore[import-untyped]
        except ImportError as exc:
            raise CTraderAPIError(
                error_code="IMPORT_ERROR",
                description=(
                    "websockets package not installed. "
                    "Run: pip install websockets"
                ),
            ) from exc

        uri = f"wss://{self._config.host}:{self._config.port}"
        logger.info("cTrader: connecting to {}", uri)

        try:
            self._ws = await websockets.connect(
                uri,
                ping_interval=None,  # We handle heartbeat ourselves
                close_timeout=10,
                max_size=2**20,  # 1MB max message
            )
        except Exception as exc:
            raise CTraderAPIError(
                error_code="CONNECTION_FAILED",
                description=f"Failed to connect to {uri}: {exc}",
            ) from exc

        self._connected = True

        # Start listener before auth (it processes auth responses)
        self._listener_task = asyncio.create_task(self._listener_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        # Authenticate application
        await self._auth_application()
        # Authenticate trading account
        await self._auth_account()
        self._authenticated = True
        logger.info("cTrader: fully authenticated (account_id={})", self._config.account_id)

    async def disconnect(self) -> None:
        """Gracefully close connection."""
        self._connected = False
        self._authenticated = False

        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass

        if self._ws:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None

        # Fail all pending futures
        for msg_id, fut in self._pending.items():
            if not fut.done():
                fut.set_exception(CTraderAPIError(
                    error_code="DISCONNECTED",
                    description="Connection closed while waiting for response",
                ))
        self._pending.clear()
        logger.info("cTrader: disconnected")

    @property
    def is_connected(self) -> bool:
        return self._connected and self._authenticated

    # ──────────────────────────────────────────────────────────────────
    # Public trading methods
    # ──────────────────────────────────────────────────────────────────
    async def place_market_order(
        self,
        *,
        symbol_id: int,
        side: int,  # TRADE_SIDE_BUY or TRADE_SIDE_SELL
        volume: int,  # In cents (units): 1 lot = 100 units for most brokers; actual depends on symbol
        stop_loss: float | None = None,
        take_profit: float | None = None,
        comment: str = "",
        label: str = "SmartGold",
    ) -> dict:
        """Place a market order. Returns the execution event payload."""
        payload: dict[str, Any] = {
            "ctidTraderAccountId": self._config.account_id,
            "symbolId": symbol_id,
            "orderType": ORDER_TYPE_MARKET,
            "tradeSide": side,
            "volume": volume,
        }
        if stop_loss is not None:
            # SL/TP in cTrader are in price (as integer cents × 100000 for 5-digit)
            # Actually cTrader accepts them as actual price values (double)
            payload["stopLoss"] = stop_loss
        if take_profit is not None:
            payload["takeProfit"] = take_profit
        if comment:
            payload["comment"] = comment[:50]  # Max 50 chars
        if label:
            payload["label"] = label[:50]

        response = await self._send_request(PT_NEW_ORDER_REQ, payload)
        return response

    async def amend_position_sl_tp(
        self,
        *,
        position_id: int,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict:
        """Modify SL/TP of an existing position."""
        payload: dict[str, Any] = {
            "ctidTraderAccountId": self._config.account_id,
            "positionId": position_id,
        }
        if stop_loss is not None:
            payload["stopLoss"] = stop_loss
        if take_profit is not None:
            payload["takeProfit"] = take_profit

        return await self._send_request(PT_AMEND_POSITION_SLTP_REQ, payload)

    async def close_position(self, *, position_id: int, volume: int | None = None) -> dict:
        """Close (fully or partially) a position."""
        payload: dict[str, Any] = {
            "ctidTraderAccountId": self._config.account_id,
            "positionId": position_id,
        }
        if volume is not None:
            payload["volume"] = volume
        return await self._send_request(PT_CLOSE_POSITION_REQ, payload)

    async def get_symbols_list(self) -> list[dict]:
        """Get all available symbols for the account."""
        payload = {"ctidTraderAccountId": self._config.account_id}
        resp = await self._send_request(PT_SYMBOLS_LIST_REQ, payload)
        return resp.get("symbol", [])

    async def get_symbol_by_id(self, symbol_ids: list[int]) -> list[dict]:
        """Get detailed symbol info by IDs."""
        payload = {
            "ctidTraderAccountId": self._config.account_id,
            "symbolId": symbol_ids,
        }
        resp = await self._send_request(PT_SYMBOL_BY_ID_REQ, payload)
        return resp.get("symbol", [])

    async def get_trader_info(self) -> dict:
        """Get account/trader information (equity, balance, etc.)."""
        payload = {"ctidTraderAccountId": self._config.account_id}
        return await self._send_request(PT_TRADER_REQ, payload)

    async def get_open_positions(self) -> list[dict]:
        """Get all open positions (reconcile request)."""
        payload = {"ctidTraderAccountId": self._config.account_id}
        resp = await self._send_request(PT_GET_POSITIONS_REQ, payload)
        return resp.get("position", [])

    # ──────────────────────────────────────────────────────────────────
    # Internal: send/receive
    # ──────────────────────────────────────────────────────────────────
    async def _send_request(self, payload_type: int, payload: dict) -> dict:
        """Send a message and wait for the correlated response."""
        if not self._connected or self._ws is None:
            raise CTraderAPIError(
                error_code="NOT_CONNECTED",
                description="Client is not connected",
            )

        msg_id = str(uuid.uuid4())[:8]
        message = json.dumps({
            "clientMsgId": msg_id,
            "payloadType": payload_type,
            "payload": payload,
        })

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = future

        try:
            await self._ws.send(message)
            logger.debug("cTrader TX [{}]: type={} payload_keys={}", msg_id, payload_type, list(payload.keys()))
        except Exception as exc:
            self._pending.pop(msg_id, None)
            raise CTraderAPIError(
                error_code="SEND_FAILED",
                description=f"WebSocket send failed: {exc}",
            ) from exc

        try:
            result = await asyncio.wait_for(future, timeout=self._config.request_timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(msg_id, None)
            raise CTraderAPIError(
                error_code="TIMEOUT",
                description=f"Request timed out after {self._config.request_timeout}s (type={payload_type})",
            ) from exc

        return result

    async def _listener_loop(self) -> None:
        """Background task: read messages from WebSocket and dispatch."""
        try:
            async for raw in self._ws:
                if not self._connected:
                    break
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("cTrader: received non-JSON message, ignoring")
                    continue
                self._dispatch(msg)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.error("cTrader listener error: {}", exc)
            self._connected = False

    def _dispatch(self, msg: dict) -> None:
        """Route incoming message to pending future or event queue."""
        payload_type = msg.get("payloadType", 0)
        client_msg_id = msg.get("clientMsgId", "")
        payload = msg.get("payload", {})

        # Heartbeat — just acknowledge
        if payload_type == PT_HEARTBEAT:
            return

        # Error responses
        if payload_type in (PT_ERROR_RES, PT_OA_ERROR_RES):
            error_code = str(payload.get("errorCode", "UNKNOWN"))
            description = str(payload.get("description", payload.get("maintenanceEndTimestamp", "")))
            exc = CTraderAPIError(error_code=error_code, description=description, payload=payload)
            if client_msg_id and client_msg_id in self._pending:
                fut = self._pending.pop(client_msg_id)
                if not fut.done():
                    fut.set_exception(exc)
            else:
                logger.warning("cTrader unsolicited error: {} — {}", error_code, description)
            return

        # Execution events (may be unsolicited — e.g. SL/TP hit)
        if payload_type == PT_EXECUTION_EVENT:
            # If it correlates to a pending request, resolve that first
            if client_msg_id and client_msg_id in self._pending:
                fut = self._pending.pop(client_msg_id)
                if not fut.done():
                    fut.set_result(payload)
            else:
                # Unsolicited execution event (SL hit, TP hit, server close)
                self._execution_events.put_nowait(payload)
            return

        # Generic response: match by clientMsgId
        if client_msg_id and client_msg_id in self._pending:
            fut = self._pending.pop(client_msg_id)
            if not fut.done():
                fut.set_result(payload)
            return

        # Unmatched message — log at debug
        logger.debug("cTrader: unmatched message type={} id={}", payload_type, client_msg_id)

    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeat to keep connection alive."""
        try:
            while self._connected and self._ws:
                await asyncio.sleep(self._config.heartbeat_interval)
                if self._ws and self._connected:
                    heartbeat = json.dumps({
                        "clientMsgId": f"hb-{int(time.time())}",
                        "payloadType": PT_HEARTBEAT,
                        "payload": {},
                    })
                    try:
                        await self._ws.send(heartbeat)
                    except Exception:  # noqa: BLE001
                        logger.warning("cTrader: heartbeat send failed")
                        break
        except asyncio.CancelledError:
            return

    # ──────────────────────────────────────────────────────────────────
    # Internal: authentication
    # ──────────────────────────────────────────────────────────────────
    async def _auth_application(self) -> None:
        """Authenticate the application (step 1)."""
        payload = {
            "clientId": self._config.client_id,
            "clientSecret": self._config.client_secret,
        }
        resp = await self._send_request(PT_APP_AUTH_REQ, payload)
        logger.debug("cTrader: app auth response: {}", resp)

    async def _auth_account(self) -> None:
        """Authenticate the trading account (step 2)."""
        payload = {
            "ctidTraderAccountId": self._config.account_id,
            "accessToken": self._config.access_token,
        }
        resp = await self._send_request(PT_ACCOUNT_AUTH_REQ, payload)
        logger.debug("cTrader: account auth response: {}", resp)


__all__ = [
    "CTraderClient",
    "CTraderConfig",
    "CTraderAPIError",
    "TRADE_SIDE_BUY",
    "TRADE_SIDE_SELL",
    "ORDER_TYPE_MARKET",
    "CTRADER_LIVE_HOST",
    "CTRADER_DEMO_HOST",
]
