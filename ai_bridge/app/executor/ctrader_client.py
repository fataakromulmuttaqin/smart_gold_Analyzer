"""cTrader MCP client — HTTP POST to mcp.ctrader.com.

Uses the standard Model Context Protocol (JSON-RPC 2.0 over HTTP).
No WebSocket, no protobuf, no OAuth2 app approval — just a bearer token.

Actual cTrader MCP tools (discovered from server):
  - create_order: symbolId, orderType, tradeSide, volume (all int/string)
  - amend_position: positionId, stopLoss, takeProfit (SL/TP set AFTER fill)
  - close_position: positionId, volume
  - get_positions, get_balance, get_symbols, get_spot_prices, etc.

Key notes:
  - SL/TP CANNOT be set in create_order — must call amend_position after
  - volume is in "cents": 1 lot = 100 cents (for forex/gold)
  - tradeSide: "BUY" or "SELL" (uppercase strings)
  - orderType: "MARKET", "LIMIT", "STOP", etc.
  - symbolId is an integer, resolved via get_symbols

Reference: https://modelcontextprotocol.io/specification/
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from app.utils.logging import logger

# ── Endpoints ────────────────────────────────────────────────────────────
CTRADER_MCP_BASE = "https://mcp.ctrader.com"
CTRADER_MCP_TRADING = f"{CTRADER_MCP_BASE}/trading/mcp"

# ── MCP Protocol version ─────────────────────────────────────────────────
MCP_PROTOCOL_VERSION = "2024-11-05"


class CTraderMCPError(Exception):
    """Raised when the cTrader MCP server returns an error."""

    def __init__(self, code: int = -1, message: str = "", data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"cTrader MCP error [{code}]: {message}")


class CTraderMCPClient:
    """HTTP client for cTrader MCP server.

    Session is tied to TCP connection — we force HTTP/1.1 keep-alive
    with a single connection pool so all requests use the same socket.

    Usage:
        async with CTraderMCPClient(token="eyJ...") as client:
            order = await client.create_market_order(symbol_id=1, side="BUY", volume=100)
            await client.amend_position(position_id=123, stop_loss=3200.0, take_profit=3300.0)
    """

    def __init__(self, token: str, endpoint: str = CTRADER_MCP_TRADING, timeout: float = 30.0) -> None:
        self._token = token
        self._endpoint = endpoint
        self._timeout = timeout
        self._http: httpx.AsyncClient | None = None
        self._initialized = False
        self._session_id: str | None = None
        self._tools: dict[str, dict] = {}
        self._request_id = 0
        self._server_info: dict = {}

    async def __aenter__(self) -> "CTraderMCPClient":
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    # ──────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────
    async def connect(self) -> None:
        """Initialize MCP session and discover tools."""
        if self._http is None:
            limits = httpx.Limits(max_connections=1, max_keepalive_connections=1, keepalive_expiry=300)
            self._http = httpx.AsyncClient(
                http1=True, http2=False, limits=limits, timeout=self._timeout,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "Connection": "keep-alive",
                },
            )

        init_result, init_headers = await self._send_with_headers("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "SmartGold-Bridge", "version": "2.1.0"},
        })

        self._session_id = (
            init_headers.get("mcp-session-id")
            or init_headers.get("Mcp-Session-Id")
            or init_headers.get("MCP-Session-Id")
        )
        self._server_info = init_result
        logger.info("cTrader MCP: initialized (session={})",
                    (self._session_id[:16] + "...") if self._session_id else "none")

        await self._send_notification("notifications/initialized", {})
        self._initialized = True

        tools_result = await self._send("tools/list", {})
        tools_list = tools_result.get("tools", [])
        self._tools = {t["name"]: t for t in tools_list}
        logger.info("cTrader MCP: {} tools: {}", len(self._tools), list(self._tools.keys()))

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None
        self._initialized = False
        self._session_id = None

    @property
    def is_connected(self) -> bool:
        return self._initialized and self._http is not None

    @property
    def available_tools(self) -> list[str]:
        return list(self._tools.keys())

    # ──────────────────────────────────────────────────────────────────
    # Trading tools (exact cTrader MCP schema)
    # ──────────────────────────────────────────────────────────────────
    async def create_market_order(self, *, symbol_id: int, side: str, volume: int) -> dict:
        """Place a market order. SL/TP must be set via amend_position after."""
        return await self.call_tool("create_order", {
            "symbolId": symbol_id, "orderType": "MARKET",
            "tradeSide": side.upper(), "volume": volume,
        })

    async def amend_position(
        self, *, position_id: int, stop_loss: float | None = None, take_profit: float | None = None,
    ) -> dict:
        """Set/modify SL and TP on an existing position."""
        args: dict[str, Any] = {"positionId": position_id}
        if stop_loss is not None:
            args["stopLoss"] = stop_loss
        if take_profit is not None:
            args["takeProfit"] = take_profit
        return await self.call_tool("amend_position", args)

    async def close_position(self, *, position_id: int, volume: int) -> dict:
        return await self.call_tool("close_position", {"positionId": position_id, "volume": volume})

    async def get_symbols(self) -> list[dict]:
        result = await self.call_tool("get_symbols", {})
        if isinstance(result, list):
            return result
        return result.get("symbols", result.get("data", []))

    async def get_balance(self) -> dict:
        return await self.call_tool("get_balance", {})

    async def get_positions(self) -> list[dict]:
        result = await self.call_tool("get_positions", {})
        if isinstance(result, list):
            return result
        return result.get("positions", result.get("data", []))

    async def get_spot_prices(self, symbol_ids: list[int]) -> dict:
        return await self.call_tool("get_spot_prices", {"symbolId": symbol_ids})

    # ──────────────────────────────────────────────────────────────────
    # Generic tool call
    # ──────────────────────────────────────────────────────────────────
    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        if not self._initialized:
            await self.connect()
        if tool_name not in self._tools:
            raise CTraderMCPError(code=-1, message=f"Tool '{tool_name}' not found. Available: {list(self._tools.keys())}")

        try:
            result = await self._send("tools/call", {"name": tool_name, "arguments": arguments})
        except CTraderMCPError as exc:
            # Detect session expiry and auto-reconnect (one retry)
            if self._is_session_expired(exc):
                logger.warning(
                    "cTrader MCP session expired — reconnecting and retrying '{}'",
                    tool_name,
                )
                await self._reconnect()
                result = await self._send("tools/call", {"name": tool_name, "arguments": arguments})
            else:
                raise

        content = result.get("content", [])
        if not content:
            return result
        for block in content:
            if block.get("type") == "text":
                text = block.get("text", "")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"text": text}
        return result

    def _is_session_expired(self, exc: CTraderMCPError) -> bool:
        """Detect if an MCP error indicates the session has expired."""
        msg = (exc.message or "").lower()
        # Common session expiry patterns from cTrader MCP
        return any(phrase in msg for phrase in (
            "session not found",
            "session expired",
            "session invalid",
            "not initialized",
            "unauthorized",
        )) or exc.code == 401

    async def _reconnect(self) -> None:
        """Close the current session and re-initialize from scratch."""
        logger.info("cTrader MCP: reconnecting...")
        # Close existing HTTP client entirely (session is dead)
        if self._http:
            await self._http.aclose()
            self._http = None
        self._initialized = False
        self._session_id = None
        self._tools = {}
        self._request_id = 0
        # Re-establish connection
        await self.connect()

    # ──────────────────────────────────────────────────────────────────
    # Internal transport
    # ──────────────────────────────────────────────────────────────────
    async def _send(self, method: str, params: dict) -> dict:
        result, _ = await self._send_with_headers(method, params)
        return result

    async def _send_with_headers(self, method: str, params: dict) -> tuple[dict, httpx.Headers]:
        if self._http is None:
            raise CTraderMCPError(code=-1, message="Client not connected")

        self._request_id += 1
        payload = {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params}
        extra_headers: dict[str, str] = {}
        if self._session_id:
            extra_headers["Mcp-Session-Id"] = self._session_id

        try:
            response = await self._http.post(self._endpoint, json=payload, headers=extra_headers)
        except httpx.TimeoutException as exc:
            raise CTraderMCPError(code=-2, message=f"Timeout: {method}") from exc
        except httpx.HTTPError as exc:
            raise CTraderMCPError(code=-3, message=f"HTTP error: {exc}") from exc

        if response.status_code != 200:
            raise CTraderMCPError(code=response.status_code, message=f"HTTP {response.status_code}: {response.text[:500]}")

        # Handle SSE format (text/event-stream) — extract JSON from data: lines
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            data = self._parse_sse(response.text)
        else:
            try:
                data = response.json()
            except json.JSONDecodeError as exc:
                raise CTraderMCPError(code=-4, message=f"Invalid JSON: {response.text[:200]}") from exc

        if "error" in data:
            err = data["error"]
            raise CTraderMCPError(code=err.get("code", -1), message=err.get("message", "Unknown"), data=err.get("data"))

        return data.get("result", data), response.headers

    @staticmethod
    def _parse_sse(text: str) -> dict:
        """Parse Server-Sent Events response to extract JSON-RPC result."""
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("data:"):
                json_str = line[5:].strip()
                if json_str:
                    try:
                        return json.loads(json_str)
                    except json.JSONDecodeError:
                        continue
        return {}

    async def _send_notification(self, method: str, params: dict) -> None:
        if self._http is None:
            return
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        extra_headers: dict[str, str] = {}
        if self._session_id:
            extra_headers["Mcp-Session-Id"] = self._session_id
        try:
            await self._http.post(self._endpoint, json=payload, headers=extra_headers)
        except Exception:  # noqa: BLE001
            pass


__all__ = ["CTraderMCPClient", "CTraderMCPError", "CTRADER_MCP_TRADING"]
