"""Client for Skroutz's remote MCP server (`shopping_mcp`).

The same server the official ChatGPT and Claude connectors use, reached with our
own OAuth token (scope `shopping_mcp`) over JSON-RPC 2.0 / streamable HTTP:

    https://api.skroutz.gr/api/agents/mcp/shopping

Its 27 tools cover both halves of this CLI: the catalogue (search, product,
deals) and the account (cart, orders, favourites, addresses, returns, invoices,
delivery changes, support tickets).
"""
from __future__ import annotations

import json
from typing import Any, Optional

from . import _http, auth
from . import __version__

ENDPOINT = "https://api.skroutz.gr/api/agents/mcp/shopping"
PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "Skroutz Shopping MCP Server"


class MCPError(RuntimeError):
    pass


class NotLoggedIn(MCPError):
    pass


def _parse_sse(text: str) -> list[dict[str, Any]]:
    """Return the JSON-RPC messages carried by an SSE response body."""
    messages: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        payload = []
        for line in block.splitlines():
            if line.startswith("data:"):
                payload.append(line[5:].lstrip())
        if not payload:
            continue
        try:
            messages.append(json.loads("\n".join(payload)))
        except ValueError:
            continue
    return messages


class MCP:
    """Minimal JSON-RPC 2.0 client for the Skroutz Shopping MCP server."""

    def __init__(self, token: Optional[str] = None, endpoint: str = ENDPOINT,
                 timeout: int = 60) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self.token = token or auth.valid_token()
        if not self.token:
            raise NotLoggedIn("not logged in — run `skroutz auth login`")
        self._id = 0
        self.session_id: Optional[str] = None
        self.server_info: Optional[dict[str, Any]] = None

    # ------------------------------------------------------------------ transport
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }

    def _post(self, payload: dict[str, Any], retry_auth: bool = True) -> list[dict[str, Any]]:
        headers = self._headers()
        try:
            response = _http.request(self.endpoint, method="POST", json_body=payload,
                                     headers=headers, timeout=self.timeout)
        except _http.TransportError as exc:
            raise MCPError(str(exc)) from exc
        if response.status == 401 and retry_auth:
            fresh = auth.valid_token()
            if fresh and fresh != self.token:
                self.token = fresh
                return self._post(payload, retry_auth=False)
            raise NotLoggedIn("Skroutz rejected the token — run `skroutz auth login`")
        if response.status >= 400:
            raise MCPError(f"HTTP {response.status}: {response.text[:300]}")
        # the server is stateless today, but honour a session id if one ever appears
        if "text/event-stream" in response.content_type.lower():
            messages = _parse_sse(response.text)
        else:
            try:
                body = response.json()
            except ValueError:
                raise MCPError(f"unparsable response: {response.text[:200]}")
            messages = body if isinstance(body, list) else [body]
        return messages

    def _rpc(self, method: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        self._id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        for message in self._post(payload):
            if message.get("id") != self._id:
                continue
            if "error" in message:
                err = message["error"]
                raise MCPError(f"{err.get('message', 'MCP error')} (code {err.get('code')})")
            return message.get("result") or {}
        raise MCPError(f"no response for {method}")

    # ---------------------------------------------------------------------- API
    def initialize(self) -> dict[str, Any]:
        result = self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "skroutz-cli", "version": __version__},
        })
        self.server_info = result.get("serverInfo") or {}
        return result

    def tools(self) -> list[dict[str, Any]]:
        return self._rpc("tools/list", {}).get("tools", [])

    def call(self, name: str, arguments: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Call a tool; returns {is_error, text, data} where data is parsed JSON."""
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        chunks = []
        for part in result.get("content") or []:
            if part.get("type") == "text":
                chunks.append(part.get("text") or "")
            else:
                chunks.append(json.dumps(part, ensure_ascii=False))
        text = "\n".join(chunks).strip()
        data: Any = result.get("structuredContent")
        if data is None and text:
            try:
                data = json.loads(text)
            except ValueError:
                data = None
        return {"is_error": bool(result.get("isError")), "text": text, "data": data}

    def call_data(self, name: str, arguments: Optional[dict[str, Any]] = None) -> Any:
        """Call a tool and return its structured payload, raising on tool errors."""
        out = self.call(name, arguments)
        if out["is_error"]:
            raise MCPError(out["text"] or f"{name} failed")
        return out["data"] if out["data"] is not None else out["text"]

    # ---------------------------------------------------------------- catalogue
    def search(self, query: str, *, sort: str = "popular", limit: int = 10,
               price_min: Optional[float] = None, price_max: Optional[float] = None,
               **kw: Any) -> Any:
        """`search_products`: the server caps a single call at 10 results."""
        args: dict[str, Any] = {"query": query, "sort": sort, "limit": max(1, min(int(limit), 10))}
        if price_min is not None:
            args["price_min"] = price_min
        if price_max is not None:
            args["price_max"] = price_max
        args.update({k: v for k, v in kw.items() if v is not None})
        return self.call_data("search_products", args)

    def product(self, sku_id: Any, *, size: Optional[str] = None,
                delivery: bool = False) -> Any:
        args: dict[str, Any] = {"sku_id": int(sku_id)}
        if size:
            args["size"] = size
        if delivery:
            args["include_delivery"] = True
        return self.call_data("get_product", args)

    def deals(self, **kw: Any) -> Any:
        return self.call_data("search_deals", {k: v for k, v in kw.items() if v is not None})

    # -------------------------------------------------------------- convenience
    def cart(self, **kw: Any) -> Any:
        return self.call_data("get_cart", {k: v for k, v in kw.items() if v is not None})

    def add(self, sku_id: Any, quantity: int = 1, **kw: Any) -> Any:
        try:
            sku: Any = int(sku_id)
        except (TypeError, ValueError):
            sku = sku_id
        args: dict[str, Any] = {"items": [{"sku_id": sku, "quantity": int(quantity)}]}
        args.update({k: v for k, v in kw.items() if v is not None})
        return self.call_data("add_to_cart", args)

    def remove(self, line_item_id: Any, **kw: Any) -> Any:
        try:
            line_item_id = int(line_item_id)
        except (TypeError, ValueError):
            pass
        args = {"line_item_id": line_item_id}
        args.update({k: v for k, v in kw.items() if v is not None})
        return self.call_data("remove_cart_item", args)

    def addresses(self) -> Any:
        return self.call_data("list_addresses")

    def orders(self, **kw: Any) -> Any:
        return self.call_data("list_orders", {k: v for k, v in kw.items() if v is not None})

    def order(self, order_code: str) -> Any:
        return self.call_data("get_order_details", {"order_code": order_code})

    def favorites(self, **kw: Any) -> Any:
        return self.call_data("search_user_favorites", {k: v for k, v in kw.items() if v is not None})

    def reorder(self, order_code: str, line_item_ids: Optional[list] = None) -> Any:
        args: dict[str, Any] = {"order_code": order_code}
        if line_item_ids:
            args["line_item_ids"] = line_item_ids
        return self.call_data("reorder", args)
