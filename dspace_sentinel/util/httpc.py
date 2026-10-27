"""Shared ``httpx.AsyncClient`` factory with the SPEC timeouts (5 s) and sane redirect handling."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from dspace_sentinel import __version__

DEFAULT_TIMEOUT = 5.0
USER_AGENT = f"dspace-sentinel/{__version__} (+https://github.com/Danindu05/dspace-sentinel)"


def make_client(
    verify: bool = True, timeout: float = DEFAULT_TIMEOUT, follow_redirects: bool = True, **kwargs: Any
) -> httpx.AsyncClient:
    """Create the shared async HTTP client.

    * hard 5 s timeout on connect/read/write/pool,
    * at most 5 redirects (a redirect loop is a failure, not a hang),
    * TLS verification per ``dspace.verify_tls``,
    * a descriptive User-Agent so Sentinel is recognisable in access logs.
    """
    limits = httpx.Limits(max_connections=8, max_keepalive_connections=4, keepalive_expiry=30.0)
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        verify=verify,
        follow_redirects=follow_redirects,
        max_redirects=5,
        limits=limits,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.9, */*;q=0.5"},
        **kwargs,
    )


@dataclass
class HttpProbe:
    """Result of :func:`probe`: a timed GET that never raises."""

    ok: bool
    status_code: int | None
    latency_ms: float | None
    error: str | None
    url: str
    headers: dict[str, str]
    text: str
    json: Any = None

    def to_dict(self) -> dict[str, Any]:
        """Plain dict form (body truncated)."""
        return {
            "ok": self.ok,
            "status_code": self.status_code,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "url": self.url,
            "server": self.headers.get("server"),
        }


def describe_error(exc: BaseException) -> str:
    """Short human text for an httpx failure ("timeout after 5 s", "connection refused", ...).

    Unrecognised texts are returned as they are (never case-folded), so callers can still
    redact secrets such as a bot token from them.
    """
    if isinstance(exc, httpx.ConnectTimeout | httpx.ReadTimeout | httpx.WriteTimeout | httpx.PoolTimeout):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        text = str(exc)
        low = text.lower()
        if "refused" in low:
            return "connection refused"
        if "name or service not known" in low or "nodename nor servname" in low or "getaddrinfo" in low:
            return "DNS lookup failed"
        if "certificate" in low or "ssl" in low:
            return "TLS error"
        return text or "connection error"
    if isinstance(exc, httpx.TooManyRedirects):
        return "too many redirects"
    if isinstance(exc, httpx.HTTPError):
        return str(exc) or exc.__class__.__name__
    return f"{exc.__class__.__name__}: {exc}"


async def probe(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_body: int = 65536,
    expect_json: bool = False,
    timeout: float | None = None,
    **kwargs: Any,
) -> HttpProbe:
    """GET ``url`` and time it. Never raises; ``ok`` is true for 2xx/3xx responses.

    The body is truncated to ``max_body`` bytes; ``json`` is parsed when ``expect_json`` or the
    content type says JSON (``None`` when parsing fails).
    """
    started = time.perf_counter()
    try:
        if timeout is not None:
            kwargs["timeout"] = timeout
        resp = await client.get(url, **kwargs)
    except Exception as exc:
        return HttpProbe(False, None, None, f"{describe_error(exc)} ({url})", url, {}, "")
    latency = round((time.perf_counter() - started) * 1000.0, 2)
    text = resp.text[:max_body] if resp.content else ""
    headers = {k.lower(): v for k, v in resp.headers.items()}
    parsed: Any = None
    ctype = headers.get("content-type", "")
    if expect_json or "json" in ctype:
        try:
            parsed = resp.json()
        except ValueError:
            parsed = None
    ok = 200 <= resp.status_code < 400
    error = None if ok else f"HTTP {resp.status_code} ({url})"
    return HttpProbe(ok, resp.status_code, latency, error, str(resp.url), headers, text, parsed)


__all__ = ["DEFAULT_TIMEOUT", "USER_AGENT", "HttpProbe", "describe_error", "make_client", "probe"]
