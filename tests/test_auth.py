"""OAuth flow tests against a local fake authorization server (no network)."""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from skroutz_cli import auth


class FakeOAuthServer:
    """Minimal OAuth 2.1 server: discovery, DCR, authorize (auto-approving), token."""

    def __init__(self) -> None:
        self.port = 0
        self.registered: dict | None = None
        self.token_requests: list[dict] = []
        self.code = "test-code"
        self.verifier_seen: str | None = None
        self.server: HTTPServer | None = None

    # -- helpers ---------------------------------------------------------
    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _pkce_ok(self, verifier: str, challenge: str) -> bool:
        digest = hashlib.sha256(verifier.encode()).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=") == challenge

    # -- http ------------------------------------------------------------
    def handler(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def _json(self, code: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path == "/.well-known/oauth-authorization-server":
                    return self._json(200, {
                        "issuer": outer.base,
                        "authorization_endpoint": outer.base + "/authorize",
                        "token_endpoint": outer.base + "/token",
                        "registration_endpoint": outer.base + "/register",
                        "response_types_supported": ["code"],
                        "grant_types_supported": ["authorization_code", "refresh_token"],
                        "code_challenge_methods_supported": ["S256"],
                        "token_endpoint_auth_methods_supported": ["client_secret_post"],
                    })
                if parsed.path == "/authorize":
                    q = dict(urllib.parse.parse_qsl(parsed.query))
                    if q.get("response_type") != "code" or q.get("code_challenge_method") != "S256":
                        return self._json(400, {"error": "invalid_request"})
                    outer.verifier_seen = q["code_challenge"]
                    target = q["redirect_uri"] + "?" + urllib.parse.urlencode(
                        {"code": outer.code, "state": q["state"]})
                    self.send_response(302)
                    self.send_header("Location", target)
                    self.end_headers()
                    return None
                return self._json(404, {"error": "not_found"})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode()
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path == "/register":
                    payload = json.loads(raw)
                    outer.registered = payload
                    return self._json(201, {
                        "client_id": "test-client",
                        "client_secret": "test-secret",
                        "redirect_uris": payload.get("redirect_uris"),
                        "token_endpoint_auth_method": "client_secret_post",
                    })
                if parsed.path == "/token":
                    form = dict(urllib.parse.parse_qsl(raw))
                    outer.token_requests.append(form)
                    if form.get("client_id") != "test-client" or form.get("client_secret") != "test-secret":
                        return self._json(401, {"error": "invalid_client"})
                    if form.get("grant_type") == "authorization_code":
                        if form.get("code") != outer.code:
                            return self._json(400, {"error": "invalid_grant"})
                        if not outer._pkce_ok(form.get("code_verifier", ""),
                                              outer.verifier_seen or ""):
                            return self._json(400, {"error": "invalid_grant",
                                                    "error_description": "pkce mismatch"})
                        return self._json(200, {
                            "access_token": "at-123", "refresh_token": "rt-456",
                            "token_type": "Bearer", "expires_in": 3600, "scope": "cart",
                        })
                    if form.get("grant_type") == "refresh_token":
                        if form.get("refresh_token") != "rt-456":
                            return self._json(400, {"error": "invalid_grant"})
                        return self._json(200, {
                            "access_token": "at-refreshed", "token_type": "Bearer",
                            "expires_in": 3600, "scope": "cart",
                        })
                return self._json(404, {"error": "not_found"})

            def log_message(self, *args) -> None:
                return

        return H

    def __enter__(self) -> "FakeOAuthServer":
        self.server = HTTPServer(("127.0.0.1", 0), self.handler())
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()


@pytest.fixture()
def fake_oauth(monkeypatch, tmp_path):
    monkeypatch.setenv(auth.CONFIG_DIR_ENV, str(tmp_path))
    with FakeOAuthServer() as srv:
        yield srv


# ------------------------------------------------------------------ unit

def test_pkce_pair_is_s256():
    verifier, challenge = auth.pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == expected


def test_authorize_url_has_pkce_and_state():
    meta = {"authorization_endpoint": "https://www.skroutz.gr/oauth2/authorizations/new"}
    url = auth.authorize_url(meta, "cid", "http://127.0.0.1:8765/callback", "st", "ch", "cart")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    assert q["client_id"] == "cid"
    assert q["code_challenge_method"] == "S256"
    assert q["state"] == "st"
    assert q["scope"] == "cart"
    assert q["redirect_uri"] == "http://127.0.0.1:8765/callback"


def test_token_store_roundtrip_and_permissions(fake_oauth):
    auth.save_tokens({"access_token": "a", "refresh_token": "r", "expires_at": 9999999999})
    assert auth.load_tokens()["access_token"] == "a"
    assert oct(auth.token_path().stat().st_mode & 0o777) == "0o600"
    assert auth.clear_tokens() is True
    assert auth.load_tokens() is None
    assert auth.clear_tokens() is False


def test_valid_token_returns_none_without_login(fake_oauth):
    assert auth.valid_token("gr") is None


# ------------------------------------------------------------- end to end

def test_login_flow_end_to_end(fake_oauth, monkeypatch):
    """Full authorization-code + PKCE dance against the fake server."""
    opened: list[str] = []

    def fake_open(url: str) -> bool:
        opened.append(url)
        threading.Thread(target=_follow, args=(url,), daemon=True).start()
        return True

    def _follow(url: str) -> None:
        urllib.request.urlopen(url, timeout=10).read()

    monkeypatch.setattr(auth.webbrowser, "open", fake_open)
    tokens = auth.login(site=fake_oauth.base, port=0, timeout=10)

    assert tokens["access_token"] == "at-123"
    assert tokens["refresh_token"] == "rt-456"
    assert tokens["expires_at"] > 0
    assert auth.load_tokens()["access_token"] == "at-123"
    # the client was registered dynamically and reused the loopback redirect
    assert fake_oauth.registered["client_name"] == "skroutz-cli"
    assert fake_oauth.registered["redirect_uris"][0].startswith("http://127.0.0.1:")
    # PKCE was actually verified server side
    assert opened and "code_challenge=" in opened[0]
    assert auth.load_client()["client_id"] == "test-client"


def test_valid_token_refreshes_when_expired(fake_oauth):
    auth._write_json(auth.client_path(), {"client_id": "test-client",
                                          "client_secret": "test-secret",
                                          "_redirect_uri": "http://127.0.0.1:8765/callback",
                                          "token_endpoint_auth_method": "client_secret_post"})
    auth.save_tokens({"access_token": "old", "refresh_token": "rt-456", "expires_at": 1})
    token = auth.valid_token(fake_oauth.base)
    assert token == "at-refreshed"
    assert auth.load_tokens()["access_token"] == "at-refreshed"


def _hit_authorize(url: str) -> None:
    """Stand in for the browser: the fake server records the PKCE challenge and 302s."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    try:
        urllib.request.build_opener(NoRedirect).open(url, timeout=10)
    except urllib.error.HTTPError as exc:
        assert exc.code == 302
        return
    raise AssertionError("the authorize endpoint did not redirect")


def test_begin_then_finish_from_pasted_redirect(fake_oauth):
    started = auth.begin(site=fake_oauth.base, port=8765)
    assert started["state"] in started["url"]
    _hit_authorize(started["url"])

    pasted = (f"http://127.0.0.1:8765/callback?code=test-code&state={started['state']}")
    tokens = auth.finish(pasted)
    assert tokens["access_token"] == "at-123"
    assert auth.load_tokens()["access_token"] == "at-123"
    assert not auth.pending_path().exists()


def test_finish_rejects_wrong_state(fake_oauth):
    auth.begin(site=fake_oauth.base, port=8765)
    with pytest.raises(auth.AuthError, match="state mismatch"):
        auth.finish("http://127.0.0.1:8765/callback?code=test-code&state=nope")


def test_finish_accepts_bare_code(fake_oauth):
    started = auth.begin(site=fake_oauth.base, port=8765)
    _hit_authorize(started["url"])
    assert auth.finish("test-code")["access_token"] == "at-123"


def test_finish_without_pending_login_fails(fake_oauth):
    with pytest.raises(auth.AuthError, match="no login in progress"):
        auth.finish("http://127.0.0.1:8765/callback?code=x&state=y")


def test_status_reports_login(fake_oauth):
    auth.save_tokens({"access_token": "abc", "refresh_token": "r", "expires_at": 9999999999,
                      "scope": "cart", "token_type": "Bearer"})
    info = auth.status()
    assert info["logged_in"] is True
    assert info["has_refresh_token"] is True
    assert info["scope"] == "cart"
