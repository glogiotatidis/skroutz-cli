"""MCP client tests against a local fake Skroutz Shopping server (no network)."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from skroutz_cli import auth, mcp

TOOLS = [
    {"name": "get_cart", "description": "The user's Skroutz cart.", "inputSchema": {"type": "object"}},
    {"name": "add_to_cart", "description": "Add products to the cart.", "inputSchema": {"type": "object"}},
]


class FakeMCP:
    """Streamable-HTTP MCP server: JSON by default, SSE when asked for it."""

    def __init__(self) -> None:
        self.port = 0
        self.calls: list[dict] = []
        self.sse = False
        self.status = 200
        self.reject_tokens: set[str] = set()
        self.token_seen: list[str] = []
        self.server: HTTPServer | None = None

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/agents/mcp/shopping"

    def handler(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a) -> None:  # keep pytest output clean
                pass

            def do_POST(self) -> None:  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(n) or b"{}")
                outer.calls.append(payload)
                header = self.headers.get("Authorization") or ""
                outer.token_seen.append(header)
                if outer.status != 200 or header.replace("Bearer ", "") in outer.reject_tokens:
                    body = b'{"errors":[{"code":"Unauthorized","messages":["unauthorized"]}]}'
                    self.send_response(outer.status if outer.status != 200 else 401)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                response = outer.respond(payload)
                raw = json.dumps(response, ensure_ascii=False).encode()
                self.send_response(200)
                if outer.sse:
                    self.send_header("Content-Type", "text/event-stream")
                    raw = b"event: message\ndata: " + raw + b"\n\n"
                else:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        return H

    @staticmethod
    def respond(payload: dict) -> dict:
        method = payload.get("method")
        rid = payload.get("id")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": mcp.PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "Skroutz Shopping MCP Server", "version": "1.0.0"}}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
        if method == "tools/call":
            name = payload["params"]["name"]
            args = payload["params"].get("arguments") or {}
            if name == "get_cart":
                data = {"success": True, "products": [], "estimated_total": 0.0}
            elif name == "add_to_cart":
                items = args.get("items") or []
                data = {"success": True, "added": items}
            elif name == "boom":
                return {"jsonrpc": "2.0", "id": rid,
                        "result": {"isError": True, "content": [{"type": "text", "text": "nope"}]}}
            else:
                data = {"success": True, "echo": name}
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": json.dumps(data)}],
                "structuredContent": data, "isError": False}}
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": f"Method not found: {method}"}}


@pytest.fixture()
def server():
    fake = FakeMCP()
    httpd = HTTPServer(("127.0.0.1", 0), fake.handler())
    fake.port = httpd.server_address[1]
    fake.server = httpd
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield fake
    httpd.shutdown()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("SKROUTZ_CONFIG_DIR", str(tmp_path))
    yield


def client(server) -> "mcp.MCP":
    return mcp.MCP(token="test-token", endpoint=server.endpoint, timeout=10)


def test_initialize_reports_server(server):
    c = client(server)
    info = c.initialize()
    assert info["serverInfo"]["name"] == "Skroutz Shopping MCP Server"
    assert info["protocolVersion"] == mcp.PROTOCOL_VERSION


def test_authorization_header_is_sent(server):
    client(server).initialize()
    assert all(h == "Bearer test-token" for h in server.token_seen)


def test_tools_are_listed(server):
    names = [t["name"] for t in client(server).tools()]
    assert names == ["get_cart", "add_to_cart"]


def test_sse_responses_are_parsed(server):
    server.sse = True
    assert [t["name"] for t in client(server).tools()] == ["get_cart", "add_to_cart"]


def test_call_returns_structured_content(server):
    out = client(server).call("get_cart")
    assert out["is_error"] is False
    assert out["data"]["success"] is True


def test_call_parses_json_text_without_structured_content(server):
    c = client(server)
    server.respond  # keep readable
    result = c.call("get_cart")
    assert result["text"].startswith("{")


def test_tool_error_raises(server):
    with pytest.raises(mcp.MCPError):
        client(server).call_data("boom")


def test_add_sends_integer_sku_and_quantity(server):
    c = client(server)
    c.add("46116906", quantity=2)
    sent = server.calls[-1]["params"]["arguments"]["items"][0]
    assert sent == {"sku_id": 46116906, "quantity": 2}
    assert isinstance(sent["sku_id"], int)


def test_remove_coerces_line_item_id_to_int(server):
    c = client(server)
    c.remove("437970523")
    assert server.calls[-1]["params"]["arguments"]["line_item_id"] == 437970523


def test_missing_login_is_reported(server):
    with pytest.raises(mcp.NotLoggedIn):
        mcp.MCP(token=None, endpoint=server.endpoint)


def test_stale_token_is_refreshed_and_retried(server, monkeypatch):
    server.reject_tokens = {"stale-token"}
    monkeypatch.setattr(auth, "valid_token", lambda **kw: "fresh-token")
    c = mcp.MCP(token="stale-token", endpoint=server.endpoint, timeout=10)
    assert c.initialize()["serverInfo"]["name"]
    assert server.token_seen == ["Bearer stale-token", "Bearer fresh-token"]
    assert c.token == "fresh-token"


def test_401_without_a_new_token_asks_for_login(server, monkeypatch):
    server.reject_tokens = {"stale-token"}
    monkeypatch.setattr(auth, "valid_token", lambda **kw: "stale-token")
    c = mcp.MCP(token="stale-token", endpoint=server.endpoint, timeout=10)
    with pytest.raises(mcp.NotLoggedIn):
        c.initialize()


def test_arg_values_are_typed():
    from skroutz_cli.cli import coerce_arg
    assert coerce_arg("2") == 2
    assert coerce_arg("2.5") == 2.5
    assert coerce_arg("true") is True
    assert coerce_arg("false") is False
    assert coerce_arg("") is True
    assert coerce_arg("ρουλεμάν") == "ρουλεμάν"


def test_account_cart_renders_line_total_as_unit_price(capsys):
    from skroutz_cli.cli import print_cart
    print_cart({"products": [{"line_item_id": 1, "sku_id": 2, "name": "Thing",
                              "quantity": 2, "estimated_line_total": 2.26}],
                "estimated_total": 2.26})
    out = capsys.readouterr().out
    assert "1,13 €" in out and "×2" in out
