"""Dependency-free JSON transport for Skroutz's own APIs.

Both halves of this CLI talk JSON: the OAuth endpoints on www.skroutz.gr and the
MCP endpoint on api.skroutz.gr. Neither is behind Cloudflare's managed
challenge, which only guards the HTML storefront, and this CLI never fetches
HTML. So a plain `urllib` request is enough and the package needs no HTTP
library at all.

A response is returned for any HTTP status, including 4xx/5xx; only a transport
failure (DNS, TLS, timeout) raises.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from . import __version__

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 skroutz-cli/{__version__}"
)


class TransportError(RuntimeError):
    """The request never produced an HTTP response."""


class Response:
    __slots__ = ("status", "content_type", "text")

    def __init__(self, status: int, content_type: str, text: str) -> None:
        self.status = status
        self.content_type = content_type
        self.text = text

    @property
    def is_json(self) -> bool:
        return "json" in self.content_type.lower()

    def json(self) -> Any:
        return json.loads(self.text)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Response {self.status} {self.content_type}>"


def request(url: str, *, method: str = "GET", json_body: Any = None,
            form: Optional[dict[str, Any]] = None, headers: Optional[dict[str, str]] = None,
            timeout: int = 30) -> Response:
    """Send one request and return the response without interpreting its status."""
    data: Optional[bytes] = None
    hdrs: dict[str, str] = {"User-Agent": UA, "Accept": "application/json"}
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode()
        hdrs["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    hdrs.update(headers or {})

    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as raw:
            return Response(raw.status, raw.headers.get("content-type") or "",
                            raw.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        content_type = exc.headers.get("content-type") if exc.headers else ""
        return Response(exc.code, content_type or "", exc.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise TransportError(f"cannot reach {url}: {reason}") from exc
