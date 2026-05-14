"""cTrader MCP client — simple HTTP POST to mcp.ctrader.com.

Uses the standard Model Context Protocol (JSON-RPC 2.0 over HTTP).
No WebSocket, no protobuf, no OAuth2 app approval — just a bearer token
generated from the cTrader platform.

Protocol:
  1. POST initialize → server capabilities
  2. POST tools/list → discover available trading tools
  3. POST tools/call → execute a tool (place order, get positions, etc.)

Token format (base64-encoded JSON):
  {"plant": "ctrader", "environment": "demo|live", "token": "<session>"}

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
    """Stateless HTTP client for cTrader MCP server.

    Each method is a single HTTP POST — no persistent connection needed.
    The client discovers available tools on first use and caches them.

    Usage:
        client = CTraderMCPClient(token="eyJ...")
        await client.connect()  # initialize + discover tools
        result = await client.call_tool("place_market_order", {...})
        await client.close()

    Or as async context manager:
        async with CTraderMCPClient(token="eyJ...") as client:
            result = await client.call_tool("place_market_order", {...})
    """

    def __init__(
        self,
        token: str,
        endpoint: str = CTRADER_MCP_TRADING,
        timeout: float = 30.0,
    ) -> None:
        self._token = token
        self._endpoint = endpoint
        self._timeout = timeout
        self._http: httpx.AsyncClient | None = None
        self._initialized = False
        self._session_id: str | None = None  # Mcp-Session-Id from server
        self._tools: dict[str, dict] = {}  # name → tool schema
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
        """Initialize MCP session and discover tools.

        Flow per MCP Streamable HTTP spec:
          1. POST initialize → get Mcp-Session-Id from response headers
          2. POST notifications/initialized (fire-and-forget notification)
          3. POST tools/list → discover tools (with session header)

        IMPORTANT: cTrader MCP ties the session to the TCP connection.
        We force HTTP/1.1 with a single keep-alive connection so all
        requests in the session go over the same socket.
        """
        if self._http is None:
            # Force HTTP/1.1 with a single persistent connection.
            # cTrader MCP server binds the session to the TCP socket —
            # if we open a new connection, the server sees "No valid session".
            limits = httpx.Limits(
                max_connections=1,
                max_keepalive_connections=1,
                keepalive_expiry=300,  # Keep socket alive 5 min
            )
            self._http = httpx.AsyncClient(
                http1=True,
                http2=False,
                limits=limits,
                timeout=self._timeout,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "Connection": "keep-alive",
                },
            )

        # Step 1: Initialize — capture session ID from response headers
        init_result, init_headers = await self._send_with_headers("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "SmartGold-Bridge", "version": "2.0.0"},
        })

        # Extract Mcp-Session-Id from response headers (try multiple casings)
        self._session_id = (
            init_headers.get("mcp-session-id")
            or init_headers.get("Mcp-Session-Id")
            or init_headers.get("MCP-Session-Id")
        )

        self._server_info = init_result
        logger.info(
            "cTrader MCP: initialized (server={}, session_id={})",
            init_result.get("serverInfo", {}).get("name", "unknown"),
            (self._session_id[:16] + "...") if self._session_id else "none",
        )

        # Step 2: Send notifications/initialized (required by MCP spec)
        # This is a JSON-RPC notification (no "id" field, no response expected)
        await self._send_notification("notifications/initialized", {})

        self._initialized = True

        # Step 3: Discover tools (now with session header)
        tools_result = await self._send("tools/list", {})
        tools_list = tools_result.get("tools", [])
        self._tools = {t["name"]: t for t in tools_list}
        logger.info(
            "cTrader MCP: discovered {} tools: {}",
            len(self._tools),
            list(self._tools.keys()),
        )

    async def close(self) -> None:
        """Close HTTP client."""
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

    def get_tool_schema(self, name: str) -> dict | None:
        """Get the input schema for a tool (for debugging/validation)."""
        tool = self._tools.get(name)
        if tool:
            return tool.get("inputSchema", {})
        return None

    # ──────────────────────────────────────────────────────────────────
    # Public: call any MCP tool
    # ──────────────────────────────────────────────────────────────────
    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        """Call a tool on the cTrader MCP server.

        Args:
            tool_name: Name of the tool (e.g. "place_market_order")
            arguments: Tool arguments as a dict

        Returns:
            Tool result (parsed from JSON-RPC response)

        Raises:
            CTraderMCPError: If the server returns an error
        """
        if not self._initialized:
            await self.connect()

        if tool_name not in self._tools:
            raise CTraderMCPError(
                code=-1,
                message=(
                    f"Tool '{tool_name}' not found. "
                    f"Available: {list(self._tools.keys())}"
                ),
            )

        result = await self._send("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })

        # MCP tools/call returns {"content": [...]} with text/json blocks
        content = result.get("content", [])
        if not content:
            return result

        # Extract the first text/json content block
        for block in content:
            if block.get("type") == "text":
                text = block.get("text", "")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"text": text}

        return result

    # ──────────────────────────────────────────────────────────────────
    # Convenience methods (common trading operations)
    # ──────────────────────────────────────────────────────────────────
    async def place_market_order(
        self,
        *,
        symbol: str,
        side: str,  # "buy" or "sell"
        volume: float,  # in lots
        stop_loss: float | None = None,
        take_profit: float | None = None,
        comment: str = "",
        label: str = "SmartGold",
    ) -> dict:
        """Place a market order. Wraps whichever tool the server exposes."""
        # Try common tool names (cTrader MCP may use different naming)
        tool_name = self._find_tool(
            "place_market_order",
            "placeMarketOrder",
            "create_market_order",
            "createMarketOrder",
            "market_order",
            "place_order",
            "placeOrder",
        )
        if not tool_name:
            raise CTraderMCPError(
                code=-1,
                message=f"No market order tool found. Available: {self.available_tools}",
            )

        args: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "volume": volume,
        }
        if stop_loss is not None:
            args["stopLoss"] = stop_loss
            args["stop_loss"] = stop_loss  # try both conventions
        if take_profit is not None:
            args["takeProfit"] = take_profit
            args["take_profit"] = take_profit
        if comment:
            args["comment"] = comment
        if label:
            args["label"] = label

        return await self.call_tool(tool_name, args)

    async def get_positions(self) -> list[dict]:
        """Get open positions."""
        tool_name = self._find_tool(
            "get_positions",
            "getPositions",
            "list_positions",
            "listPositions",
            "open_positions",
            "positions",
        )
        if not tool_name:
            raise CTraderMCPError(
                code=-1,
                message=f"No positions tool found. Available: {self.available_tools}",
            )

        result = await self.call_tool(tool_name, {})
        if isinstance(result, list):
            return result
        return result.get("positions", result.get("data", [result]))

    async def modify_position(
        self,
        *,
        position_id: str | int,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict:
        """Modify SL/TP of an existing position."""
        tool_name = self._find_tool(
            "modify_position",
            "modifyPosition",
            "amend_position",
            "amendPosition",
            "update_position",
            "updatePosition",
        )
        if not tool_name:
            raise CTraderMCPError(
                code=-1,
                message=f"No modify position tool found. Available: {self.available_tools}",
            )

        args: dict[str, Any] = {"positionId": str(position_id), "position_id": str(position_id)}
        if stop_loss is not None:
            args["stopLoss"] = stop_loss
            args["stop_loss"] = stop_loss
        if take_profit is not None:
            args["takeProfit"] = take_profit
            args["take_profit"] = take_profit

        return await self.call_tool(tool_name, args)

    async def get_account_info(self) -> dict:
        """Get account information (balance, equity, etc.)."""
        tool_name = self._find_tool(
            "get_account",
            "getAccount",
            "get_account_info",
            "getAccountInfo",
            "account_info",
            "accountInfo",
            "account",
        )
        if not tool_name:
            raise CTraderMCPError(
                code=-1,
                message=f"No account info tool found. Available: {self.available_tools}",
            )

        return await self.call_tool(tool_name, {})

    async def get_symbol_info(self, symbol: str) -> dict:
        """Get symbol details (for lot sizing constraints)."""
        tool_name = self._find_tool(
            "get_symbol",
            "getSymbol",
            "get_symbol_info",
            "getSymbolInfo",
            "symbol_info",
            "symbolInfo",
        )
        if not tool_name:
            # Not critical — we can size without it
            return {}

        return await self.call_tool(tool_name, {"symbol": symbol})

    # ──────────────────────────────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────────────────────────────
    def _find_tool(self, *candidates: str) -> str | None:
        """Find the first matching tool name from candidates."""
        for name in candidates:
            if name in self._tools:
                return name
        # Also try case-insensitive match
        lower_tools = {k.lower(): k for k in self._tools}
        for name in candidates:
            if name.lower() in lower_tools:
                return lower_tools[name.lower()]
        return None

    async def _send(self, method: str, params: dict) -> dict:
        """Send a JSON-RPC 2.0 request to the MCP server (with session header)."""
        result, _ = await self._send_with_headers(method, params)
        return result

    async def _send_with_headers(self, method: str, params: dict) -> tuple[dict, httpx.Headers]:
        """Send a JSON-RPC 2.0 request and return (result, response_headers)."""
        if self._http is None:
            raise CTraderMCPError(code=-1, message="Client not connected")

        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }

        # Include Mcp-Session-Id header if we have one
        extra_headers: dict[str, str] = {}
        if self._session_id:
            extra_headers["Mcp-Session-Id"] = self._session_id

        # ── SSE-aware response parsing ──────────────────────────────────
        def _parse_response(resp: httpx.Response) -> dict:
            """Parse response, handling both plain JSON and SSE formats.

            cTrader MCP may return:
              - application/json: {"jsonrpc":"2.0","id":1,"result":{...}}
              - text/event-stream: event: message\\ndata: {"jsonrpc":"2.0",...}\\n\\n
            """
            ct = resp.headers.get("content-type", "").lower()

            if "text/event-stream" in ct or "event-stream" in ct:
                # SSE format: "event: message\\ndata: {...}\\n\\n"
                text = resp.text
                # Find "data: " lines and parse JSON after them
                for line in text.split("\n"):
                    line = line.strip()
                    if line.startswith("data:"):
                        json_str = line[5:].strip()  # Remove "data:" prefix
                        try:
                            return json.loads(json_str)
                        except json.JSONDecodeError:
                            pass
                raise CTraderMCPError(
                    code=-4,
                    message=f"SSE but no parseable JSON in data: {resp.text[:200]}",
                )

            # Plain JSON
            try:
                return resp.json()
            except json.JSONDecodeError as exc:
                raise CTraderMCPError(
                    code=-4,
                    message=f"Invalid JSON response: {resp.text[:200]}",
                ) from exc

        try:
            response = await self._http.post(
                self._endpoint, json=payload, headers=extra_headers,
            )
        except httpx.TimeoutException as exc:
            raise CTraderMCPError(
                code=-2,
                message=f"Request timed out after {self._timeout}s: {method}",
            ) from exc
        except httpx.HTTPError as exc:
            raise CTraderMCPError(
                code=-3,
                message=f"HTTP error: {exc}",
            ) from exc

        if response.status_code != 200:
            raise CTraderMCPError(
                code=response.status_code,
                message=f"HTTP {response.status_code}: {response.text[:500]}",
            )

        data = _parse_response(response)

        # JSON-RPC error handling
        if "error" in data:
            err = data["error"]
            raise CTraderMCPError(
                code=err.get("code", -1),
                message=err.get("message", "Unknown error"),
                data=err.get("data"),
            )

        return data.get("result", data), response.headers

    async def _send_notification(self, method: str, params: dict) -> None:
        """Send a JSON-RPC 2.0 notification (no id, no response expected).

        Per MCP spec, notifications have no "id" field. Server may return
        200/202/204 — all acceptable.
        """
        if self._http is None:
            raise CTraderMCPError(code=-1, message="Client not connected")

        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }

        extra_headers: dict[str, str] = {}
        if self._session_id:
            extra_headers["Mcp-Session-Id"] = self._session_id

        try:
            response = await self._http.post(
                self._endpoint, json=payload, headers=extra_headers,
            )
            if response.status_code >= 400:
                logger.warning(
                    "cTrader MCP: notification {} returned HTTP {}",
                    method, response.status_code,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("cTrader MCP: notification {} failed: {}", method, exc)


__all__ = ["CTraderMCPClient", "CTraderMCPError", "CTRADER_MCP_TRADING"]
