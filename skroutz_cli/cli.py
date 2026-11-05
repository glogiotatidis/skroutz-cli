"""Command line interface: `skroutz search`, `skroutz product`, `skroutz cheapest`.

Every command, browsing included, is a JSON-RPC call to Skroutz's own shopping MCP server with the
user's OAuth token. There is no scraping path and no anonymous mode: `skroutz auth login` first.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from typing import Any, Optional

from . import __version__, auth
from . import mcp as mcp_mod

# CLI sort name -> the MCP server's enum
SORTS = {
    "relevance": "popular",
    "cheapest": "price_asc",
    "expensive": "price_desc",
    "rating": "rating",
    "newest": "newest",
}

# `search_products` accepts at most 10 results in one call.
MAX_RESULTS = 10

SKU_RE = re.compile(r"/s/(\d+)(?:/|$)")


class UsageError(RuntimeError):
    pass


def money(value: Optional[float]) -> str:
    if value is None:
        return "—"
    s = f"{value:,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".")
    return f"{s} €"


def resolve_sku(target: str) -> int:
    m = SKU_RE.search(target)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{5,})", target)
    if m:
        return int(m.group(1))
    raise UsageError(f"cannot find a Skroutz product id in {target!r}")


def build_client(args: argparse.Namespace) -> mcp_mod.MCP:
    return mcp_mod.MCP(timeout=args.timeout)


def product_price(item: dict[str, Any]) -> Optional[float]:
    for key in ("cart_price", "lowest_price", "price"):
        value = item.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


# --------------------------------------------------------------------------- search

def cmd_search(args: argparse.Namespace) -> int:
    client = build_client(args)
    limit = args.limit
    if limit > MAX_RESULTS:
        print(f"note: the MCP search returns at most {MAX_RESULTS} results, showing {MAX_RESULTS}",
              file=sys.stderr)
        limit = MAX_RESULTS
    data = client.search(args.query, sort=SORTS[args.sort], limit=limit,
                         price_min=args.price_min, price_max=args.price_max)
    products = (data or {}).get("products") or []

    if args.json:
        json.dump({"query": args.query, "sort": args.sort, "count": len(products),
                   "results": products}, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0

    if not products:
        print(f"no results for {args.query!r}")
        return 1
    category = ((data or {}).get("category") or {}).get("name")
    header = f"{len(products)} results for {args.query!r} ({args.sort})"
    print(f"{header}  [{category}]" if category else header)
    for i, item in enumerate(products, 1):
        rating = f"★{item['rating']}" if item.get("rating") else "—"
        reviews = f" ({item['reviews_count']})" if item.get("reviews_count") else ""
        print(f"{i:>2}. {money(product_price(item)):>12}  {rating}{reviews}  {item.get('name')}")
        if item.get("spec_summary"):
            print(f"    {item['spec_summary']}")
        print(f"    sku {item.get('sku_id')} · {item.get('url')}")
    return 0


# -------------------------------------------------------------------------- product

def cmd_product(args: argparse.Namespace) -> int:
    sku = resolve_sku(args.target)
    data = build_client(args).product(sku, size=args.size, delivery=args.delivery) or {}

    if args.json:
        json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0

    print(data.get("name") or f"sku {sku}")
    line = f"  price: {money(product_price(data))}"
    if data.get("rating"):
        line += f"  ★{data['rating']}"
        if data.get("reviews_count"):
            line += f" ({data['reviews_count']} reviews)"
    print(line)
    if data.get("lowest_price") and data.get("lowest_price") != data.get("cart_price"):
        print(f"  lowest seen: {money(data['lowest_price'])}")
    if data.get("manufacturer"):
        print(f"  brand: {data['manufacturer']}")
    print(f"  {data.get('url')}")
    specs = data.get("key_specs") or []
    if specs:
        print("\n  specs")
        for spec in specs[:args.specs]:
            print(f"    {spec.get('name')}: {spec.get('value')}")
    delivery = data.get("delivery") or {}
    for location in (delivery.get("locations") or [])[:3]:
        when = location.get("delivery_date") or "?"
        ship = location.get("shipping_cost")
        ship_txt = money(ship) if isinstance(ship, (int, float)) else "?"
        print(f"  {location.get('label') or location.get('city')}: {when}, shipping {ship_txt}")
    options = data.get("purchase_options") or {}
    if options.get("requires_size"):
        sizes = ", ".join(str(s) for s in (data.get("available_sizes") or []))
        print(f"  sizes: {sizes or 'none available'}")
    return 0


# ------------------------------------------------------------------------- cheapest

def cmd_cheapest(args: argparse.Namespace) -> int:
    data = build_client(args).search(args.query, sort="price_asc", limit=MAX_RESULTS)
    products = [p for p in ((data or {}).get("products") or []) if product_price(p) is not None]
    if not products:
        print(f"no priced results for {args.query!r}", file=sys.stderr)
        return 1
    best = min(products, key=lambda p: product_price(p) or 0)
    if args.json:
        json.dump(best, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        print(f"{money(product_price(best))}  {best.get('name')}")
        if best.get("spec_summary"):
            print(f"  {best['spec_summary']}")
        print(f"  sku {best.get('sku_id')} · {best.get('url')}")
    return 0


# ------------------------------------------------------------------------------ auth

def cmd_auth(args: argparse.Namespace) -> int:
    action = args.auth_command
    if action == "login":
        tokens = auth.login(port=args.port, open_browser=not args.no_browser,
                            timeout=args.wait, scope=args.scope, verbose=args.verbose)
        if args.json:
            out = {k: v for k, v in tokens.items() if k != "access_token"}
            out["access_token"] = "***"
            json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
            print()
            return 0
        exp = tokens.get("expires_at")
        print("logged in to Skroutz")
        if exp:
            mins = max(0, int((exp - time.time()) // 60))
            print(f"  access token expires in ~{mins} min (auto-refreshed on use)")
        if tokens.get("refresh_token"):
            print("  refresh token stored — `skroutz auth logout` revokes it locally")
        print(f"  tokens: {auth.token_path()}")
        return 0

    if action == "url":
        started = auth.begin(port=args.port, scope=args.scope)
        if args.json:
            json.dump(started, sys.stdout, ensure_ascii=False, indent=2)
            print()
            return 0
        print("Open this URL, log in to Skroutz, then approve the app:\n")
        print(f"  {started['url']}\n")
        print("Your browser will land on an unreachable 127.0.0.1 page — that is expected.")
        print("Copy the whole address from the address bar and paste it back with:")
        print("  skroutz auth exchange '<pasted url>'")
        return 0

    if action == "exchange":
        tokens = auth.finish(args.redirect)
        if args.json:
            out = {k: v for k, v in tokens.items() if k != "access_token"}
            out["access_token"] = "***"
            json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
            print()
            return 0
        exp = tokens.get("expires_at")
        print("logged in to Skroutz")
        if exp:
            print(f"  access token expires in ~{max(0, int((exp - time.time()) // 60))} min")
        print(f"  refresh token: {'yes' if tokens.get('refresh_token') else 'no'}")
        print(f"  tokens: {auth.token_path()}")
        return 0

    if action == "logout":
        removed = auth.clear_tokens()
        print("logged out" if removed else "not logged in")
        return 0

    info = auth.status()
    if args.json:
        out = dict(info)
        if out.get("access_token"):
            out["access_token"] = out["access_token"][:12] + "…"
        json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0
    if not info["logged_in"]:
        print("not logged in — run `skroutz auth login`")
        print(f"  config: {info['config_dir']}")
        return 1
    print("logged in to Skroutz (oauth2 + pkce)")
    print(f"  client_id: {info.get('client_id')}")
    if info.get("expires_in") is not None:
        state = "expired, will refresh on next call" if info["expires_in"] < 60 \
            else f"{info['expires_in'] // 60} min left"
        print(f"  token: {state}")
    print(f"  refresh token: {'yes' if info.get('has_refresh_token') else 'no'}")
    print(f"  scope: {info.get('scope') or 'shopping_mcp'}")
    print(f"  tokens: {info['token_file']}")
    return 0


# --------------------------------------------------------------------------- account

def account_client(args: argparse.Namespace) -> mcp_mod.MCP:
    return mcp_mod.MCP(timeout=args.timeout)


def _addr_opts(args: argparse.Namespace) -> dict[str, Any]:
    return {"address_id": getattr(args, "address_id", None),
            "shop_id": getattr(args, "shop_id", None)}


def coerce_arg(value: str) -> Any:
    """`--arg limit=2` must send the integer 2, not the string "2" — the MCP
    server validates types strictly and rejects strings with a schema error."""
    if value == "":
        return True
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none"):
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def print_cart(data: dict[str, Any]) -> None:
    items = data.get("products") or []
    if not items:
        print("your Skroutz cart is empty")
        return
    for i, item in enumerate(items, 1):
        name = item.get("name") or item.get("title") or f"sku {item.get('sku_id')}"
        price = item.get("estimated_line_total")
        if isinstance(price, (int, float)) and (item.get("quantity") or 1) > 1:
            price = price / (item.get("quantity") or 1)
        qty = item.get("quantity") or 1
        price_txt = money(float(price)) if isinstance(price, (int, float)) else "—"
        print(f"{i:>2}. {price_txt:>11} ×{qty:<2} {name}")
        if item.get("size"):
            print(f"    size: {item['size']}")
        print(f"    sku {item.get('sku_id')} · line_item {item.get('line_item_id')}")
    total = data.get("estimated_total")
    if isinstance(total, (int, float)) and total:
        print(f"\n  estimated total: {money(float(total))}")


def print_orders(data: dict[str, Any]) -> None:
    orders = data.get("orders") or []
    if not orders:
        print("no orders")
        return
    for o in orders:
        when = (o.get("paid_at") or "")[:10]
        total = o.get("total")
        total_txt = money(float(total)) if isinstance(total, (int, float)) else "—"
        print(f"{o.get('order_code')}  {when}  {total_txt:>10}  {o.get('state', '')}")
        if o.get("shops"):
            print(f"    shop: {', '.join(o['shops'])}")
        for preview in (o.get("items_preview") or [])[:4]:
            print(f"    · {preview}")
        print(f"    {o.get('items_count', '?')} item(s)")


def cmd_account(args: argparse.Namespace) -> int:
    action = args.account_command
    client = account_client(args)

    if action == "tools":
        tools = client.tools()
        if args.json:
            json.dump(tools, sys.stdout, ensure_ascii=False, indent=2)
            print()
            return 0
        for t in tools:
            print(f"{t['name']}")
            desc = (t.get("description") or "").strip().split("\n")[0]
            if desc:
                print(f"    {desc[:110]}")
        print(f"\n{len(tools)} tools on {mcp_mod.SERVER_NAME}")
        return 0

    if action == "call":
        raw = args.tool_args
        if raw is None:
            arguments: dict[str, Any] = {}
        elif raw.startswith("@"):
            with open(raw[1:], encoding="utf-8") as fh:
                arguments = json.load(fh)
        else:
            arguments = json.loads(raw)
        for pair in args.arg or []:
            key, _, value = pair.partition("=")
            if not key:
                raise UsageError(f"bad --arg {pair!r}, expected key=value")
            arguments[key] = coerce_arg(value)
        out = client.call(args.tool, arguments)
        if args.json:
            json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
            print()
        else:
            print(out["text"] or "(no content)")
        return 1 if out["is_error"] else 0

    if action == "cart":
        data = client.cart(**_addr_opts(args), limit=args.limit)
        if args.json:
            json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
            print()
        else:
            print_cart(data if isinstance(data, dict) else {})
        return 0

    if action == "add":
        sku = resolve_sku(args.target)
        data = client.add(sku, quantity=args.quantity, **_addr_opts(args))
        if args.json:
            json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
            print()
            return 0
        if isinstance(data, dict) and data.get("success") is False:
            raise mcp_mod.MCPError(json.dumps(data, ensure_ascii=False))
        print(f"added sku {sku} ×{args.quantity} to your cart")
        after = client.cart(**_addr_opts(args), limit=20)
        print_cart(after if isinstance(after, dict) else {})
        return 0

    if action == "remove":
        data = client.remove(args.line_item_id, **_addr_opts(args))
        if args.json:
            json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
            print()
            return 0
        print(f"removed line item {args.line_item_id}")
        after = client.cart(**_addr_opts(args), limit=20)
        print_cart(after if isinstance(after, dict) else {})
        return 0

    if action == "orders":
        data = client.orders(search_term=args.search, state=args.state,
                             from_date=args.from_date, to_date=args.to_date,
                             limit=args.limit, page=args.page)
        if args.json:
            json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
            print()
        else:
            print_orders(data if isinstance(data, dict) else {})
        return 0

    if action == "order":
        data = client.order(args.order_code)
        if args.json:
            json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
            print()
            return 0
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    if action == "addresses":
        data = client.addresses()
        if args.json:
            json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
            print()
            return 0
        for a in (data or {}).get("addresses", []):
            print(f"{a.get('address_id')}  {a.get('label', '').strip()} — "
                  f"{a.get('street', '').strip()} {a.get('city', '')} {a.get('zip', '')}".rstrip())
        return 0

    if action == "favorites":
        data = client.favorites(query=args.query, limit=args.limit, sort=args.sort,
                                after_id=args.after)
        if args.json:
            json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
            print()
            return 0
        for f in (data or {}).get("favorites", []):
            print(f"{f.get('favorite_id')}  sku {f.get('sku_id')}  {(f.get('name') or '')[:80]}")
        return 0

    if action == "reorder":
        data = client.reorder(args.order_code)
        if args.json:
            json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
            print()
            return 0
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    if action == "deals":
        data = client.deals(query=args.query, limit=args.limit)
        if args.json:
            json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
            print()
            return 0
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    raise UsageError(f"unknown account command {action!r}")


def cmd_mcp(args: argparse.Namespace) -> int:
    if args.mcp_command == "info":
        client = account_client(args)
        info = client.initialize()
        print(f"endpoint: {client.endpoint}")
        print(f"server:   {(info.get('serverInfo') or {}).get('name', '?')}")
        caps = ",".join(sorted((info.get("capabilities") or {}).keys()))
        print(f"protocol: {info.get('protocolVersion')}  capabilities: {caps}")
        tools = client.tools()
        print(f"tools:    {len(tools)}")
        return 0
    raise UsageError(f"unknown mcp command {args.mcp_command!r}")


# ------------------------------------------------------------------------------ cli

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="skroutz",
        description="Search Skroutz.gr, compare prices and use your Skroutz account, "
                    "over Skroutz's official MCP server.")
    p.add_argument("--version", action="version", version=f"skroutz-cli {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--timeout", type=int, default=40, help="per-request timeout in seconds")
    common.add_argument("--json", action="store_true", help="machine readable output")
    common.add_argument("-v", "--verbose", action="store_true", help="extra logging on stderr")

    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", parents=[common], help="search products")
    s.add_argument("query")
    s.add_argument("-n", "--limit", type=int, default=10,
                   help=f"max results, {MAX_RESULTS} is the server ceiling (default: 10)")
    s.add_argument("-s", "--sort", default="relevance", choices=sorted(SORTS))
    s.add_argument("--price-min", dest="price_min", type=float, default=None)
    s.add_argument("--price-max", dest="price_max", type=float, default=None)
    s.set_defaults(func=cmd_search)

    d = sub.add_parser("product", parents=[common], help="one product: price, specs, delivery")
    d.add_argument("target", help="product URL, sku id, or any string containing the id")
    d.add_argument("--size", default=None, help="check availability for a size")
    d.add_argument("--delivery", action="store_true",
                   help="also quote shipping cost and delivery date to your addresses")
    d.add_argument("-n", "--specs", type=int, default=12, help="key specs to print (default: 12)")
    d.set_defaults(func=cmd_product)

    c = sub.add_parser("cheapest", parents=[common], help="cheapest product match for a keyword")
    c.add_argument("query")
    c.set_defaults(func=cmd_cheapest)

    # ---- oauth
    a = sub.add_parser("auth", parents=[common], help="log in to your Skroutz account (OAuth 2.1 + PKCE)")
    auth_sub = a.add_subparsers(dest="auth_command", required=True)

    al = auth_sub.add_parser("login", parents=[common],
                             help="register the CLI and authorize it in your browser")
    al.add_argument("--port", type=int, default=auth.DEFAULT_PORT,
                    help=f"loopback redirect port (default: {auth.DEFAULT_PORT}, 0 = any free port)")
    al.add_argument("--no-browser", action="store_true", help="only print the URL, do not open a browser")
    al.add_argument("--wait", type=int, default=auth.LOGIN_TIMEOUT,
                    help=f"seconds to wait for the browser redirect (default: {auth.LOGIN_TIMEOUT})")
    al.add_argument("--scope", default=None, help=f"override the scope (default: {auth.DEFAULT_SCOPE})")
    al.set_defaults(func=cmd_auth)

    au = auth_sub.add_parser("url", parents=[common],
                             help="print an authorization URL to open on any device")
    au.add_argument("--port", type=int, default=auth.DEFAULT_PORT,
                    help="port baked into the redirect URI (default: %d)" % auth.DEFAULT_PORT)
    au.add_argument("--scope", default=None)
    au.set_defaults(func=cmd_auth)

    ax = auth_sub.add_parser("exchange", parents=[common],
                             help="finish a login from the pasted redirect URL")
    ax.add_argument("redirect", help="the URL skroutz.gr redirected to (or just the code)")
    ax.set_defaults(func=cmd_auth)

    ast = auth_sub.add_parser("status", parents=[common], help="show the stored login")
    ast.set_defaults(func=cmd_auth)

    alo = auth_sub.add_parser("logout", parents=[common], help="delete the stored tokens")
    alo.set_defaults(func=cmd_auth)

    # ---- account (MCP: real account data — cart, orders, favourites, returns)
    ac = sub.add_parser("account", parents=[common],
                        help="your Skroutz account via the official MCP connector")
    ac_sub = ac.add_subparsers(dest="account_command", required=True)

    at = ac_sub.add_parser("tools", parents=[common], help="list the MCP tools")
    at.set_defaults(func=cmd_account)

    acall = ac_sub.add_parser("call", parents=[common], help="call any MCP tool by name")
    acall.add_argument("tool")
    acall.add_argument("tool_args", nargs="?", default=None,
                       help="JSON object of arguments, or @file.json")
    acall.add_argument("--arg", action="append", metavar="KEY=VALUE",
                       help="argument shorthand, repeatable")
    acall.set_defaults(func=cmd_account)

    aca = ac_sub.add_parser("cart", parents=[common], help="your real account cart")
    aca.add_argument("-n", "--limit", type=int, default=20)
    aca.add_argument("--address-id", default=None, help="supermarket cart of this address")
    aca.add_argument("--shop-id", default=None)
    aca.set_defaults(func=cmd_account)

    acd = ac_sub.add_parser("add", parents=[common], help="add a product to your account cart")
    acd.add_argument("target", help="product URL, sku id, or any string containing the id")
    acd.add_argument("-q", "--quantity", type=int, default=1)
    acd.add_argument("--address-id", default=None)
    acd.add_argument("--shop-id", default=None)
    acd.set_defaults(func=cmd_account)

    acr = ac_sub.add_parser("remove", parents=[common], help="remove a line item from your cart")
    acr.add_argument("line_item_id")
    acr.add_argument("--address-id", default=None)
    acr.add_argument("--shop-id", default=None)
    acr.set_defaults(func=cmd_account)

    aco = ac_sub.add_parser("orders", parents=[common], help="your paid orders, newest first")
    aco.add_argument("--search", default=None, help="product, brand or shop name")
    aco.add_argument("--state", default=None)
    aco.add_argument("--from-date", dest="from_date", default=None)
    aco.add_argument("--to-date", dest="to_date", default=None)
    aco.add_argument("-n", "--limit", type=int, default=10)
    aco.add_argument("-p", "--page", type=int, default=1)
    aco.set_defaults(func=cmd_account)

    acod = ac_sub.add_parser("order", parents=[common], help="one order, per shop, with products")
    acod.add_argument("order_code")
    acod.set_defaults(func=cmd_account)

    acad = ac_sub.add_parser("addresses", parents=[common], help="your saved delivery addresses")
    acad.set_defaults(func=cmd_account)

    acf = ac_sub.add_parser("favorites", parents=[common], help="your saved favourites")
    acf.add_argument("query", nargs="?", default=None)
    acf.add_argument("--sort", default=None)
    acf.add_argument("--after", type=int, default=None)
    acf.add_argument("-n", "--limit", type=int, default=20)
    acf.set_defaults(func=cmd_account)

    acrr = ac_sub.add_parser("reorder", parents=[common], help="put a past order in the cart again")
    acrr.add_argument("order_code")
    acrr.set_defaults(func=cmd_account)

    acdl = ac_sub.add_parser("deals", parents=[common], help="live deals, closest first")
    acdl.add_argument("query", nargs="?", default=None)
    acdl.add_argument("-n", "--limit", type=int, default=10)
    acdl.set_defaults(func=cmd_account)

    # ---- mcp introspection
    mc = sub.add_parser("mcp", parents=[common], help="inspect the Skroutz Shopping MCP server")
    mc_sub = mc.add_subparsers(dest="mcp_command", required=True)
    mi = mc_sub.add_parser("info", parents=[common], help="endpoint, protocol, tool count")
    mi.set_defaults(func=cmd_mcp)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (UsageError, auth.AuthError, mcp_mod.MCPError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
