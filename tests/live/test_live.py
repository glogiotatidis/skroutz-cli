"""Live tests against the real Skroutz OAuth server and MCP endpoint.

These are the canary for the whole chain: OAuth discovery, the MCP server's tool
set, and the shapes our commands render. They need network access and run in the
scheduled workflow, never in the offline suite (`pytest -m "not live"`).

Read-only: nothing here adds to or removes from a cart, so a failing run never
leaves an account in a strange state.

Account tests need `SKROUTZ_ACCESS_TOKEN` (and optionally `SKROUTZ_REFRESH_TOKEN`
+ `SKROUTZ_CLIENT_ID`/`SKROUTZ_CLIENT_SECRET`); without them they skip rather
than fail, so a fork with no secrets still gets the public checks.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pytest

from skroutz_cli import _http, auth, mcp

pytestmark = pytest.mark.live

QUERY = "ρουλεμάν 6000"


def token() -> str:
    value = os.environ.get("SKROUTZ_ACCESS_TOKEN", "").strip()
    if not value:
        pytest.skip("SKROUTZ_ACCESS_TOKEN not set")
    return value


# ----------------------------------------------------------------- metadata / oauth

def test_oauth_metadata_is_plain_json():
    """No browser TLS fingerprint needed: the discovery document answers urllib."""
    meta = auth.discover("https://www.skroutz.gr")
    assert meta["issuer"].startswith("https://www.skroutz.gr")
    assert meta["token_endpoint"].endswith("/oauth2/token")
    assert "S256" in meta["code_challenge_methods_supported"]


def test_token_endpoint_answers_plain_requests():
    """A rejected grant is still proof the endpoint is reachable without impersonation."""
    r = _http.request("https://www.skroutz.gr/oauth2/token", method="POST",
                      form={"grant_type": "authorization_code", "code": "nope",
                            "redirect_uri": "http://127.0.0.1:8765/callback",
                            "client_id": "nope"})
    assert r.status in (400, 401), f"unexpected status {r.status}: {r.text[:200]}"
    assert r.json().get("error"), "token endpoint returned no OAuth error envelope"


# ------------------------------------------------------------------------ mcp

def test_mcp_endpoint_rejects_anonymous_requests():
    r = _http.request(mcp.ENDPOINT, method="POST",
                      json_body={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": mcp.PROTOCOL_VERSION,
                                            "capabilities": {},
                                            "clientInfo": {"name": "ci", "version": "0"}}},
                      headers={"Accept": "application/json"}, timeout=30)
    assert r.status == 401, f"expected 401 without a token, got {r.status}"


def test_mcp_initialize_and_tools():
    server = mcp.MCP(token=token(), timeout=45)
    info = server.initialize()
    assert info["serverInfo"]["name"], "no serverInfo.name"
    tools = {t["name"] for t in server.tools()}
    for expected in ("search_products", "get_product", "get_cart", "add_to_cart", "list_orders"):
        assert expected in tools, f"{expected} disappeared from the tool set"
    assert len(tools) >= 20, f"only {len(tools)} tools — did the server change shape?"


def test_search_returns_priced_products():
    data = mcp.MCP(token=token(), timeout=45).search(QUERY, limit=5)
    assert data.get("success") is True, f"search_products failed: {data}"
    products = data.get("products") or []
    assert products, "MCP search returned no products"
    assert any(p.get("sku_id") for p in products)
    assert any(p.get("cart_price") or p.get("lowest_price") for p in products), \
        "no product carried a price — the search shape moved"


def test_product_details_for_the_first_search_hit():
    server = mcp.MCP(token=token(), timeout=45)
    first = (server.search(QUERY, limit=1).get("products") or [None])[0]
    assert first, "search returned nothing to look up"
    product = server.product(first["sku_id"])
    assert product.get("name"), "no product name"
    assert product.get("url", "").startswith("https://www.skroutz.gr/"), "no canonical url"


def test_account_reads():
    server = mcp.MCP(token=token(), timeout=45)
    addresses = server.addresses()
    assert addresses.get("success") is True, f"list_addresses failed: {addresses}"
    cart = server.cart()
    assert cart.get("success") is True, f"get_cart failed: {cart}"
    orders = server.orders(limit=3)
    assert orders.get("success") is True, f"list_orders failed: {orders}"


@pytest.mark.refresh
def test_refresh_token_still_works():
    """A daily job must notice a dead grant before the access token expires.

    Marked `refresh` as well as `live`: refreshing **rotates** the token, so a
    scheduled run that exercises this without writing the new refresh token back
    to the secret silently kills the credential it just used. It runs by hand,
    or in CI only once a writeback path exists.
    """
    refresh_token = os.environ.get("SKROUTZ_REFRESH_TOKEN", "").strip()
    client_id = os.environ.get("SKROUTZ_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SKROUTZ_CLIENT_SECRET", "").strip()
    if not (refresh_token and client_id and client_secret):
        pytest.skip("refresh credentials not configured")
    meta = auth.discover("https://www.skroutz.gr")
    fresh = auth._token_request(
        meta,
        {"client_id": client_id, "client_secret": client_secret},
        {"grant_type": "refresh_token", "refresh_token": refresh_token},
    )
    assert fresh.get("access_token"), "refresh returned no access token"
