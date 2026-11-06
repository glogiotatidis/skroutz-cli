# skroutz-cli

[Skroutz.gr](https://www.skroutz.gr) in the terminal: search, prices, and your real account
(cart, orders, favourites, returns) over Skroutz's **official MCP server**, the same one behind
its ChatGPT and Claude connectors.

No scraping, no browser session, no API key. One OAuth login, JSON-RPC from then on, and **no
runtime dependencies** — the package talks to Skroutz with `urllib` from the standard library.

## Install

```bash
pipx install git+https://github.com/glogiotatidis/skroutz-cli
# or, from a checkout
python3 -m venv .venv && .venv/bin/pip install -e . && ln -s "$PWD/.venv/bin/skroutz" /usr/local/bin/skroutz
```

## Log in once

```bash
skroutz auth login                 # opens your browser; you log in on skroutz.gr, nothing is typed here
skroutz auth url                   # no local listener: print a URL to open on any device
skroutz auth exchange '<redirect>' # finish that login with the URL your browser landed on
skroutz auth status                # client id, token expiry, scope
skroutz auth logout                # deletes the stored tokens
```

The CLI registers itself as an OAuth 2.1 client (dynamic client registration, no console
involved), runs authorization-code + PKCE over a loopback redirect, and stores an opaque 24 h
access token plus a rotating refresh token under `~/.config/skroutz-cli/` (mode 0600, override the
directory with `SKROUTZ_CONFIG_DIR`). Only two files: `oauth_client.json` and `tokens.json`.

The scope is required and is a resource name, not a permission: `shopping_mcp`, the consumer
shopping resource. Override with `--scope` if Skroutz adds another.

## Search and compare

```bash
skroutz search "ρουλεμάν 6000"                 # relevance, 10 results
skroutz search "rtx 5070" -n 5 -s cheapest     # price_asc
skroutz search "κράνος" --price-min 50 --price-max 120
skroutz search "πλυντήριο ρούχων" --json       # machine readable, for scripts and cron
skroutz product 59501016                       # price, specs, canonical url
skroutz product 59501016 --delivery            # plus shipping cost and date to your addresses
skroutz product <url> --size 59                # size availability
skroutz cheapest "rtx 5070"                    # cheapest match, ideal for monitoring
```

Sorts: `relevance` (popular), `cheapest`, `expensive`, `rating`, `newest`.

Two limits come from the server, not the CLI: `search_products` returns **at most 10** results per
call, and the price it reports is the one you would pay through Skroutz's cart (`cart_price`), with
`lowest_price` alongside when a shop sells it cheaper. There is no per-shop offer list on the MCP
surface; for "which shop has it cheapest" the honest answer is Skroutz's own cart price.

### JSON shape

`search` → `{query, sort, count, results: [...]}`; per product: `sku_id, name, category,
cart_price, lowest_price, can_add_to_cart, requires_size, requires_options, rating, reviews_count,
spec_summary, image_url, url`.

`product` → the same fields plus `manufacturer, key_specs[], review_aspects, purchase_options` and,
with `--delivery`, `delivery: {shop_id, price, locations: [{label, city, shipping_cost,
delivery_date, available}]}`.

Flag order matters: `skroutz search "x" --json`.

## Your account

```bash
skroutz mcp info                       # endpoint, protocol version, tool count
skroutz account tools                  # all 27 tools with one-line descriptions
skroutz account cart                   # your real account cart
skroutz account add 46116906 -q 2      # add to it
skroutz account remove 437979835       # line_item_id from `account cart`
skroutz account orders -n 5            # paid orders, newest first
skroutz account order 261105-6744249   # one order, per shop, with products
skroutz account addresses              # saved delivery addresses
skroutz account favorites [query]      # saved favourites / price alerts
skroutz account reorder <order_code>   # put a past order in the cart again
skroutz account deals [query]          # live deals, closest first
skroutz account call <tool> '{...}'    # any tool, raw JSON arguments
```

Notes that cost time to learn:

- The endpoint is `https://api.skroutz.gr/api/agents/mcp/shopping`: JSON-RPC 2.0 over streamable
  HTTP, stateless, protocol `2025-06-18`, 27 tools.
- Per-item cart prices come back as `estimated_line_total` (divide by `quantity`), not `price`.
- `sku_id` and `line_item_id` must be **integers**; strings fail schema validation.
- Responses are `application/json` here, but the client also parses `text/event-stream`
  (`data:` frames), because the spec lets a server choose per request.
- A 401 triggers one token refresh and a single retry; a second 401 means log in again.
- Refreshing **rotates** the refresh token. Persist the response before using it, or the next call
  logs you out. `auth.valid_token()` saves first; do not re-issue a refresh to "check" it.

## Tests and CI

```bash
pytest                       # offline suite: fake OAuth and MCP servers, no network
pytest -m live               # against the real OAuth server and the real MCP endpoint
pytest -m refresh            # refresh-grant check — rotates the token, run by hand only
```

Two workflows:

- **`tests.yml`** (push / PR): the offline suite on Python 3.10, 3.12 and 3.13, a CLI smoke test,
  and a wheel build installed into a clean venv, so packaging cannot rot silently.
- **`live.yml`** (daily 05:30 UTC, and manual dispatch): `-m live` against `www.skroutz.gr` and
  `api.skroutz.gr`. The metadata and MCP-reachability checks need no credentials; account tests
  read `SKROUTZ_ACCESS_TOKEN` and skip when it is absent, so a fork without secrets still runs the
  public half.

Secrets used by `live.yml` (all optional): `SKROUTZ_ACCESS_TOKEN`, `SKROUTZ_REFRESH_TOKEN`,
`SKROUTZ_CLIENT_ID`, `SKROUTZ_CLIENT_SECRET`.

## Layout

```
skroutz_cli/_http.py   urllib transport, one place, no dependencies
skroutz_cli/auth.py    OAuth 2.1: discovery, DCR, PKCE, loopback + pasted-redirect login, tokens
skroutz_cli/mcp.py     JSON-RPC client for the shopping MCP server + catalogue helpers
skroutz_cli/cli.py     argparse surface and output formatting
```

If Skroutz changes a tool's arguments or response shape, `mcp.py` is the only file to touch; the
rendering lives in `cli.py`.

## License

GPL-3.0, see [LICENSE](LICENSE).
