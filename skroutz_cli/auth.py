"""OAuth 2.1 login for skroutz.gr (authorization code + PKCE + dynamic client registration).

Skroutz runs a standard OAuth server on the main site — the same one its ChatGPT/Claude
connectors use — discovered at ``/.well-known/oauth-authorization-server``:

    issuer                 https://www.skroutz.gr
    authorization_endpoint /oauth2/authorizations/new
    token_endpoint         /oauth2/token
    registration_endpoint  /oauth2/register      (dynamic client registration, open)
    response_types         code
    grant_types            authorization_code, refresh_token
    code_challenge         S256
    token auth methods     client_secret_post

So there is nothing to hard-code: the CLI discovers the endpoints, registers itself once as a
public OAuth client, runs a loopback redirect with PKCE, and stores the tokens under
``~/.config/skroutz-cli/``. No password ever passes through the CLI — the user authenticates in
their own browser on skroutz.gr and the site redirects back with a code.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Optional

from . import _http

CONFIG_DIR_ENV = "SKROUTZ_CONFIG_DIR"
CALLBACK_PATH = "/callback"
DEFAULT_PORT = 8765
LOGIN_TIMEOUT = 300

# Skroutz's registration endpoint rejects an empty scope with
#   "invalid_client_metadata: scope must target a single resource and may only contain: ..."
# and lists the enum. `shopping_mcp` is the consumer shopping/connector resource (the others are
# internal: merchants, support, content ops, staff, …).
DEFAULT_SCOPE = "shopping_mcp"

DEFAULT_SITE = "https://www.skroutz.gr"


class AuthError(RuntimeError):
    pass


def base_url(site: Optional[str] = None) -> str:
    """Resolve a site: a full base URL passes through, `gr`/`cy`/`de`… maps to www.skroutz.<tld>.

    Every country site serves the same OAuth metadata and names the www.skroutz.gr issuer, so one
    login is enough regardless of which site the user started from.
    """
    if not site:
        return DEFAULT_SITE
    if site.startswith(("http://", "https://")):
        return site.rstrip("/")
    return f"https://www.skroutz.{site}"


# --------------------------------------------------------------------------- storage

def config_dir() -> Path:
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "skroutz-cli"


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        return json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError):
        return None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def client_path() -> Path:
    return config_dir() / "oauth_client.json"


def token_path() -> Path:
    return config_dir() / "tokens.json"


def load_client() -> Optional[dict[str, Any]]:
    return _read_json(client_path())


def load_tokens() -> Optional[dict[str, Any]]:
    data = _read_json(token_path())
    if data and data.get("access_token"):
        return data
    return None


def save_tokens(tokens: dict[str, Any]) -> None:
    _write_json(token_path(), tokens)


def clear_tokens() -> bool:
    try:
        token_path().unlink()
        return True
    except FileNotFoundError:
        return False


# ------------------------------------------------------------------------ discovery

def discover(base: str, timeout: int = 30) -> dict[str, Any]:
    """Fetch the OAuth authorization server metadata for a Skroutz site."""
    url = base.rstrip("/") + "/.well-known/oauth-authorization-server"
    try:
        r = _http.request(url, timeout=timeout)
    except _http.TransportError as exc:
        raise AuthError(str(exc)) from exc
    if r.status != 200:
        raise AuthError(f"{url} returned HTTP {r.status}")
    try:
        meta = r.json()
    except ValueError as exc:
        raise AuthError(f"{url} did not return JSON: {exc}") from exc
    for key in ("authorization_endpoint", "token_endpoint"):
        if not meta.get(key):
            raise AuthError(f"OAuth metadata from {url} is missing {key}")
    return meta


# ------------------------------------------------------------------- client registration

def register_client(meta: dict[str, Any], redirect_uri: str, scope: Optional[str] = None,
                    timeout: int = 30) -> dict[str, Any]:
    """Dynamic client registration (RFC 7591). Returns the stored client record."""
    endpoint = meta.get("registration_endpoint")
    if not endpoint:
        raise AuthError(
            "this Skroutz site does not advertise dynamic client registration; "
            "pass --client-id/--client-secret if you registered an app manually"
        )
    methods = meta.get("token_endpoint_auth_methods_supported") or ["client_secret_post"]
    payload = {
        "client_name": "skroutz-cli",
        "redirect_uris": [redirect_uri],
        "grant_types": meta.get("grant_types_supported") or ["authorization_code", "refresh_token"],
        "response_types": meta.get("response_types_supported") or ["code"],
        "token_endpoint_auth_method": "client_secret_post" if "client_secret_post" in methods else methods[0],
    }
    if scope:
        payload["scope"] = scope
    try:
        r = _http.request(endpoint, method="POST", json_body=payload, timeout=timeout)
    except _http.TransportError as exc:
        raise AuthError(f"client registration failed: {exc}") from exc
    if r.status not in (200, 201):
        raise AuthError(f"client registration returned HTTP {r.status}: {r.text[:300]}")
    try:
        data = r.json()
    except ValueError as exc:
        raise AuthError(f"client registration returned no JSON: {exc}") from exc
    if not data.get("client_id"):
        raise AuthError("client registration response had no client_id")
    data["_issuer"] = meta.get("issuer")
    data["_redirect_uri"] = redirect_uri
    data["_scope"] = scope
    data["_registered_at"] = int(time.time())
    _write_json(client_path(), data)
    return data


def ensure_client(meta: dict[str, Any], redirect_uri: str, scope: Optional[str] = None,
                  refresh: bool = False, timeout: int = 30) -> dict[str, Any]:
    existing = None if refresh else load_client()
    if (existing and existing.get("client_id")
            and existing.get("_redirect_uri") == redirect_uri
            and existing.get("_scope") == scope):
        return existing
    return register_client(meta, redirect_uri, scope=scope, timeout=timeout)


# --------------------------------------------------------------------------- PKCE

def pkce_pair() -> tuple[str, str]:
    """Return (verifier, challenge) per RFC 7636, S256 only."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode().rstrip("=")
    return verifier, challenge


def pending_path() -> Path:
    return config_dir() / "pending_login.json"


def begin(site: str = "gr", port: int = DEFAULT_PORT, scope: Optional[str] = None,
          timeout: int = 30) -> dict[str, Any]:
    """Start a login without listening locally: store the PKCE verifier + state and return the
    authorization URL. The user can open it on any device; `finish()` then takes the redirect
    URL they paste back (the code is in it even when the page itself fails to load)."""
    base = base_url(site)
    scope = scope or DEFAULT_SCOPE
    meta = discover(base, timeout=timeout)
    redirect_uri = f"http://127.0.0.1:{port}{CALLBACK_PATH}"
    client = ensure_client(meta, redirect_uri, scope=scope, timeout=timeout)
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    url = authorize_url(meta, client["client_id"], redirect_uri, state, challenge, scope)
    _write_json(pending_path(), {
        "verifier": verifier,
        "state": state,
        "redirect_uri": redirect_uri,
        "site": base,
        "created_at": int(time.time()),
    })
    return {"url": url, "redirect_uri": redirect_uri, "state": state,
            "expires_at": int(time.time()) + 600}


def finish(value: str, timeout: int = 30) -> dict[str, Any]:
    """Complete a login started with `begin()` from the pasted redirect URL (or the bare code)."""
    pending = _read_json(pending_path())
    if not pending:
        raise AuthError("no login in progress — run `skroutz auth url` first")
    if int(time.time()) - int(pending.get("created_at") or 0) > 900:
        raise AuthError("the pending login is older than 15 minutes — start again with `skroutz auth url`")

    value = value.strip()
    params: dict[str, str] = {}
    if "code=" in value or "state=" in value:
        query = urllib.parse.urlparse(value).query or value.split("?", 1)[-1]
        params = dict(urllib.parse.parse_qsl(query))
    elif value and " " not in value and "/" not in value:
        params = {"code": value}

    if params.get("error"):
        raise AuthError(f"authorization failed: {params['error']} {params.get('error_description', '')}".strip())
    code = params.get("code")
    if not code:
        raise AuthError("could not find an authorization code in that value; "
                        "paste the whole URL that skroutz.gr redirected you to")
    if params.get("state") and params["state"] != pending["state"]:
        raise AuthError("state mismatch in the pasted redirect — start again with `skroutz auth url`")

    meta = discover(pending.get("site") or DEFAULT_SITE, timeout=timeout)
    client = load_client()
    if not client:
        raise AuthError("no registered OAuth client — run `skroutz auth url` first")
    tokens = exchange_code(meta, client, code, pending["verifier"], pending["redirect_uri"],
                           timeout=timeout)
    try:
        pending_path().unlink()
    except FileNotFoundError:
        pass
    return tokens


def authorize_url(meta: dict[str, Any], client_id: str, redirect_uri: str,
                  state: str, challenge: str, scope: Optional[str] = None) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if scope:
        params["scope"] = scope
    return f"{meta['authorization_endpoint']}?{urllib.parse.urlencode(params)}"


# ------------------------------------------------------------------- loopback listener

class _CallbackHandler(BaseHTTPRequestHandler):
    result: dict[str, str] = {}
    done = threading.Event()

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return
        params = dict(urllib.parse.parse_qsl(parsed.query))
        type(self).result = params
        type(self).done.set()
        ok = "code" in params
        body = (
            "<html><body style='font-family:sans-serif;padding:3em'>"
            f"<h2>{'Logged in to Skroutz' if ok else 'Login failed'}</h2>"
            "<p>You can close this tab and return to the terminal.</p>"
            "</body></html>"
        ).encode()
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # silence
        return


def _free_port(preferred: int) -> int:
    for port in (preferred, 0):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("127.0.0.1", port))
                return sock.getsockname()[1]
        except OSError:
            continue
    raise AuthError("no free local port for the OAuth callback")


def _start_callback_server(port: int) -> tuple[HTTPServer, type]:
    """Bind the loopback listener before the browser is opened, so the redirect can never
    arrive before something is listening for it."""
    handler = type("Handler", (_CallbackHandler,), {"result": {}, "done": threading.Event()})
    server = HTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, handler


def _wait_for_code(handler: type, server: HTTPServer, timeout: int) -> dict[str, str]:
    try:
        if not handler.done.wait(timeout):
            raise AuthError(f"timed out after {timeout}s waiting for the browser redirect")
        return handler.result
    finally:
        server.shutdown()
        server.server_close()


# ------------------------------------------------------------------------ token calls

def _token_request(meta: dict[str, Any], client: dict[str, Any], form: dict[str, str],
                   timeout: int = 30) -> dict[str, Any]:
    form = dict(form)
    method = client.get("token_endpoint_auth_method") or "client_secret_post"
    if method == "client_secret_post":
        form["client_id"] = client["client_id"]
        form["client_secret"] = client["client_secret"]
    try:
        r = _http.request(meta["token_endpoint"], method="POST", form=form, timeout=timeout)
    except _http.TransportError as exc:
        raise AuthError(f"token request failed: {exc}") from exc
    if r.status != 200:
        detail = r.text[:300]
        try:
            err = r.json()
            detail = f"{err.get('error')}: {err.get('error_description') or ''}".strip()
        except ValueError:
            pass
        raise AuthError(f"token endpoint returned HTTP {r.status} ({detail})")
    try:
        data = r.json()
    except ValueError as exc:
        raise AuthError(f"token endpoint returned no JSON: {exc}") from exc
    if "access_token" not in data:
        raise AuthError("token endpoint returned no access_token")
    data["obtained_at"] = int(time.time())
    if data.get("expires_in"):
        data["expires_at"] = data["obtained_at"] + int(data["expires_in"])
    return data


def exchange_code(meta: dict[str, Any], client: dict[str, Any], code: str,
                  verifier: str, redirect_uri: str, **kw: Any) -> dict[str, Any]:
    tokens = _token_request(meta, client, {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }, **kw)
    save_tokens(tokens)
    return tokens


def refresh(meta: dict[str, Any], client: dict[str, Any], tokens: dict[str, Any],
            **kw: Any) -> dict[str, Any]:
    rt = tokens.get("refresh_token")
    if not rt:
        raise AuthError("no refresh_token stored; run `skroutz auth login` again")
    fresh = _token_request(meta, client, {"grant_type": "refresh_token", "refresh_token": rt}, **kw)
    # servers may rotate the refresh token; keep the old one when they do not
    fresh.setdefault("refresh_token", rt)
    fresh.setdefault("scope", tokens.get("scope"))
    save_tokens(fresh)
    return fresh


def valid_token(site: str = "gr", skew: int = 60, timeout: int = 30) -> Optional[str]:
    """Return a usable access token, refreshing it when it is close to expiry."""
    tokens = load_tokens()
    if not tokens:
        return None
    expires_at = tokens.get("expires_at")
    if expires_at and int(expires_at) - skew < int(time.time()):
        client = load_client()
        if not client:
            return None
        meta = discover(base_url(site), timeout=timeout)
        try:
            tokens = refresh(meta, client, tokens, timeout=timeout)
        except AuthError:
            clear_tokens()
            return None
    return tokens.get("access_token")


# ------------------------------------------------------------------------------ login

def login(site: str = "gr", port: int = DEFAULT_PORT, open_browser: bool = True,
          timeout: int = LOGIN_TIMEOUT, scope: Optional[str] = None,
          verbose: bool = False) -> dict[str, Any]:
    base = base_url(site)
    scope = scope or DEFAULT_SCOPE
    meta = discover(base)
    listen_port = _free_port(port)
    redirect_uri = f"http://127.0.0.1:{listen_port}{CALLBACK_PATH}"
    client = ensure_client(meta, redirect_uri, scope=scope)

    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    url = authorize_url(meta, client["client_id"], redirect_uri, state, challenge, scope)

    server, handler = _start_callback_server(listen_port)
    print(f"opening the Skroutz login page:\n\n  {url}\n", file=sys.stderr, flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - headless boxes just print the URL
            pass
    print(f"waiting for the redirect on {redirect_uri} (Ctrl-C to abort)...",
          file=sys.stderr, flush=True)

    params = _wait_for_code(handler, server, timeout)
    if params.get("error"):
        raise AuthError(f"authorization failed: {params.get('error')} "
                        f"{params.get('error_description', '')}".strip())
    if params.get("state") != state:
        raise AuthError("state mismatch in the OAuth redirect — aborting (possible CSRF)")
    code = params.get("code")
    if not code:
        raise AuthError("no authorization code in the redirect")

    tokens = exchange_code(meta, client, code, verifier, redirect_uri)
    if verbose:
        print(f"[skroutz] stored tokens in {token_path()}", file=sys.stderr)
    return tokens


def status(site: str = "gr") -> dict[str, Any]:
    tokens = load_tokens()
    client = load_client()
    info: dict[str, Any] = {
        "logged_in": bool(tokens),
        "config_dir": str(config_dir()),
        "client_id": (client or {}).get("client_id"),
        "token_file": str(token_path()),
    }
    if tokens:
        expires_at = tokens.get("expires_at")
        info.update({
            "scope": tokens.get("scope"),
            "token_type": tokens.get("token_type"),
            "obtained_at": tokens.get("obtained_at"),
            "expires_at": expires_at,
            "expires_in": (int(expires_at) - int(time.time())) if expires_at else None,
            "has_refresh_token": bool(tokens.get("refresh_token")),
            "access_token": tokens.get("access_token"),
        })
    return info
