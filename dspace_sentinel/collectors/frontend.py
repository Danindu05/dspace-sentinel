"""DSpace frontend collector: service → TCP → homepage → public URL → backend dependency.

The homepage rung fetches ``dspace.ui_url`` and expects a 200 carrying the Angular app's
``<ds-app>`` element (or, in a server-rendered page whose ``<head>`` is long, the
``<meta name="Generator" content="DSpace …">`` DSpace writes there). It tells a server-side
render (``ng-server-context="ssr"``, ``ng-version``) from the client-side shell
(``<ds-app></ds-app>``, the UI's ``index.html``): the shell keeps the rung ok with a note.

From 9.3 the UI renders on the server only for the host of its ``ui.baseUrl`` (the fix for
CVE-2026-27739) and serves the shell to any other ``Host``, logging an SSR error each time. So
when ``dspace.public_url`` names another host than ``ui_url``, the homepage request carries
``Host: <that host>`` (plus ``X-Forwarded-Proto: https`` for an https public URL), exactly what
the reverse proxy sends. A shell served for another host than ui.baseUrl's is explained by that
rule; a shell served for ui.baseUrl's own host means server-side rendering is off or failing
(:func:`csr_note`). The public URL rung runs only when ``dspace.public_url`` is set and goes
through the reverse proxy like a visitor would. A public host that is not ASCII goes into the
``Host`` header IDNA-encoded (``bücher.example`` → ``xn--bcher-kva.example``), the way a browser
and the proxy send it; ``metrics.host_header`` keeps the host as written.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Any, ClassVar

import httpx

from dspace_sentinel import dspace_version
from dspace_sentinel.collectors.base import Collector, CollectorContext, CollectorResult
from dspace_sentinel.collectors.services import (
    Ladder,
    http_message,
    rung,
    slow_message,
    systemd_rung,
    tcp_rung,
    url_parts,
)
from dspace_sentinel.health import dependency_rung, threshold_status
from dspace_sentinel.util.httpc import probe

log = logging.getLogger(__name__)

MARKER_RE = re.compile(r"<ds-app\b", re.IGNORECASE)
"""The Angular app's root element: ``<ds-app>`` in every DSpace 7–10 UI (not the bare word)."""
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
SSR_RE = re.compile(r"ng-server-context\s*=\s*[\"']?ssr", re.IGNORECASE)
CSR_SHELL_RE = re.compile(r"<ds-app>\s*</ds-app>", re.IGNORECASE)
NG_VERSION_RE = re.compile(r"\bng-version\s*=\s*[\"']?([0-9][0-9A-Za-z.\-]*)", re.IGNORECASE)
_META_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'>]+))")
HOME_MAX_BODY = 2 * 1024 * 1024
"""Server-rendered homepages inline their critical CSS in ``<head>``: read up to 2 MiB."""
NO_MARKUP = "no DSpace markup in the response"
CSR_NOTE = "client-side rendering only (server-side rendering is off or failing)"


def generator(body: str) -> str | None:
    """``<meta name="Generator" content="DSpace 9.2">`` → ``"DSpace 9.2"`` (``None`` without one)."""
    for tag in _META_RE.findall(body or ""):
        attrs = {
            m.group(1).lower(): next(g for g in m.groups()[1:] if g is not None)
            for m in _ATTR_RE.finditer(tag)
        }
        if attrs.get("name", "").lower() == "generator" and attrs.get("content", "").strip():
            return attrs["content"].strip()[:100]
    return None


def looks_like_dspace(body: str) -> bool:
    """True for the DSpace UI: a ``<ds-app>`` element, or a ``Generator`` meta naming DSpace."""
    if not body:
        return False
    if MARKER_RE.search(body):
        return True
    return (generator(body) or "").lower().startswith("dspace")


def rendering(body: str) -> str | None:
    """``"ssr"`` for a server-side render, ``"csr"`` for the client-side shell, else ``None``."""
    if not body:
        return None
    if SSR_RE.search(body):
        return "ssr"
    if CSR_SHELL_RE.search(body):
        return "csr"
    if MARKER_RE.search(body) and NG_VERSION_RE.search(body):
        return "ssr"  # ng-version is only in the HTML when Angular rendered it on the server
    return None


def angular_version(body: str) -> str | None:
    """The ``ng-version`` attribute Angular writes on ``<ds-app>`` when it renders on the server."""
    m = NG_VERSION_RE.search(body or "")
    return m.group(1) if m else None


def page_title(body: str) -> str | None:
    """The ``<title>`` text of an HTML page (whitespace collapsed, entities decoded), or ``None``."""
    m = TITLE_RE.search(body or "")
    if not m:
        return None
    title = html.unescape(" ".join(m.group(1).split()))
    return title[:200] or None


def wire_host(host: str) -> str | None:
    """``host`` as it goes into a ``Host`` header: IDNA-encoded when it is not ASCII
    (``bücher.example`` → ``xn--bcher-kva.example``, as httpx encodes the URL itself), or ``None``
    for a name that has no such form."""
    if host.isascii():
        return host
    try:
        return httpx.URL(f"http://{host}/").raw_host.decode("ascii")
    except Exception:  # not a valid IDNA name: send no Host header rather than fail the request
        return None


def same_host(a: str, b: str) -> bool:
    """True when two host names (brackets, case and IDNA spelling aside) name the same host."""
    a, b = a.strip("[]"), b.strip("[]")
    return (wire_host(a) or a).lower() == (wire_host(b) or b).lower()


def public_host_headers(ui_url: str, public_url: str) -> dict[str, str]:
    """The headers that make a request to ``ui_url`` look like one for ``public_url``.

    ``{"Host": <public host>}`` (plus ``X-Forwarded-Proto: https`` for an https public URL) when
    the public URL names another host; ``{}`` otherwise. The host is IDNA-encoded
    (:func:`wire_host`): a header is ASCII, and a name that cannot be encoded sends none.
    """
    if not public_url.strip():
        return {}
    public = url_parts(public_url)
    if same_host(public.host, url_parts(ui_url).host):
        return {}
    host = wire_host(public.host)
    if host is None:
        return {}
    host = f"[{host}]" if ":" in host else host
    headers = {"Host": host}
    if public.scheme == "https":
        headers["X-Forwarded-Proto"] = "https"
    return headers


def csr_note(version: Any, sent_host: str | None = None, ui_host: str | None = None) -> str:
    """Why the homepage was the client-side shell.

    From 9.3 DSpace renders on the server only for the host of its ui.baseUrl (``ui_host``) and
    serves the shell to any other ``Host``. That rule is named only when the request carried
    another host (``sent_host``); for ui.baseUrl's own host, or when nothing says which host it
    names, a shell means server-side rendering is off or failing (:data:`CSR_NOTE`).
    """
    v = dspace_version.parse(version)
    if dspace_version.ssr_host_restricted(v) and sent_host and ui_host and not same_host(sent_host, ui_host):
        return (
            f"client-side rendering only: DSpace {v} renders on the server only for the host in its "
            "ui.baseUrl (set dspace.public_url)"
        )
    return CSR_NOTE


class FrontendCollector(Collector):
    """``frontend`` 🖥️: service → TCP → homepage → public URL (optional) → backend dependency."""

    id: ClassVar[str] = "frontend"
    interval_key: ClassVar[str] = "http_interval"
    label: ClassVar[str] = "DSpace Frontend"
    emoji: ClassVar[str] = "🖥️"
    depends_on: ClassVar[list[str]] = ["backend"]
    budget_s: ClassVar[float] = 20.0  # systemd 3 + TCP 2 + homepage 5 + public URL 5 + slack

    async def collect(self, ctx: CollectorContext) -> CollectorResult:
        """Climb the ladder against ``dspace.ui_url``; the backend dependency rung is always appended."""
        d = ctx.cfg.dspace
        ts = ctx.now
        base = d.ui_url.rstrip("/")
        parts = url_parts(base)
        public_url = d.public_url.strip().rstrip("/")
        tcp = ("frontend.tcp", f"TCP :{parts.port}")
        home = ("frontend.home", "Homepage")
        public = ("frontend.public", "Public URL")
        ladder = Ladder(ts)
        host_headers = public_host_headers(base, public_url)
        shown_host = None  # the Host sent, as the public URL writes it (not in its IDNA form)
        if host_headers:
            written = url_parts(public_url).host
            shown_host = f"[{written}]" if ":" in written else written
        metrics: dict[str, Any] = {
            "home_latency_ms": None,
            "public_latency_ms": None,
            "title": None,
            "rendering": None,
            "generator": None,
            "angular": None,
            "host_header": shown_host,
            "ui_url": base,
            "public_url": public_url or None,
        }
        tail = [home, public] if public_url else [home]

        ladder.add(await systemd_rung("frontend.service", d.frontend_service, ts))
        if ladder.blocked:
            ladder.skip_all([tcp, *tail])
            return self._finish(ctx, ladder, metrics)

        ladder.add(await tcp_rung(tcp[0], parts.host, parts.port, ts, label=tcp[1]))
        if ladder.blocked:
            ladder.skip_all(tail)
            return self._finish(ctx, ladder, metrics)

        # --- homepage (as the public host when public_url names another one) ----------------
        p = await probe(ctx.http, base, max_body=HOME_MAX_BODY, headers=host_headers or None)
        title = page_title(p.text) if p.ok else None
        metrics["title"] = title
        if p.ok and looks_like_dspace(p.text):
            latency = float(p.latency_ms or 0.0)
            th = ctx.cfg.alerts.home_latency_ms
            status = threshold_status(latency, th.warning, th.critical)
            mode = rendering(p.text)
            note = None
            if mode == "csr":
                sent_host = url_parts(public_url).host if host_headers else parts.host
                ui_host = self._ui_base_host(ctx, public_url)
                note = csr_note(self._backend_version(ctx), sent_host, ui_host)
            metrics.update(rendering=mode, generator=generator(p.text), angular=angular_version(p.text))
            ladder.add(
                rung(
                    home[0],
                    home[1],
                    status,
                    ts,
                    latency_ms=p.latency_ms,
                    message=(note or "") if status == "ok" else slow_message("homepage", latency),
                    slow=True if status != "ok" else None,
                    title=title,
                    url=base,
                    status_code=p.status_code,
                    rendering=mode,
                    note=note,
                    host_header=host_headers.get("Host"),
                )
            )
            metrics["home_latency_ms"] = p.latency_ms
        elif p.ok:
            ladder.add(
                rung(
                    home[0],
                    home[1],
                    "warn",
                    ts,
                    latency_ms=p.latency_ms,
                    message=NO_MARKUP,
                    title=title,
                    url=base,
                    status_code=p.status_code,
                    host_header=host_headers.get("Host"),
                )
            )
            metrics["home_latency_ms"] = p.latency_ms
        else:
            ladder.add(
                rung(
                    home[0],
                    home[1],
                    "crit",
                    ts,
                    latency_ms=p.latency_ms,
                    message=http_message(p),
                    url=base,
                    status_code=p.status_code,
                    host_header=host_headers.get("Host"),
                )
            )

        # --- public URL (through nginx), only when configured --------------------------------
        if public_url:
            if ladder.blocked:
                ladder.skip(*public)
            else:
                pp = await probe(ctx.http, public_url, max_body=HOME_MAX_BODY)
                if pp.ok and looks_like_dspace(pp.text):
                    ladder.add(
                        rung(
                            public[0],
                            public[1],
                            "ok",
                            ts,
                            latency_ms=pp.latency_ms,
                            url=public_url,
                            status_code=pp.status_code,
                            rendering=rendering(pp.text),
                        )
                    )
                    metrics["public_latency_ms"] = pp.latency_ms
                elif pp.ok:
                    ladder.add(
                        rung(
                            public[0],
                            public[1],
                            "warn",
                            ts,
                            latency_ms=pp.latency_ms,
                            message=NO_MARKUP,
                            url=public_url,
                            status_code=pp.status_code,
                        )
                    )
                    metrics["public_latency_ms"] = pp.latency_ms
                else:
                    ladder.add(
                        rung(
                            public[0],
                            public[1],
                            "crit",
                            ts,
                            latency_ms=pp.latency_ms,
                            message=http_message(pp),
                            url=public_url,
                            status_code=pp.status_code,
                        )
                    )
        return self._finish(ctx, ladder, metrics)

    @staticmethod
    def _backend_version(ctx: CollectorContext) -> Any:
        """The DSpace version the backend collector last read (``None`` before it or when it is off)."""
        backend = ctx.dependency("backend")
        metrics = getattr(backend, "metrics", None)
        return metrics.get("dspace_version") if isinstance(metrics, dict) else None

    @staticmethod
    def _ui_base_host(ctx: CollectorContext, public_url: str) -> str | None:
        """The host DSpace's ui.baseUrl names, as far as Sentinel knows.

        The backend's REST root reports ``dspaceUI`` (``dspace.ui.url``, which 8.4+ / 9.3+ require
        to match ui.baseUrl); without it, ``dspace.public_url``; ``None`` when neither is known.
        """
        backend = ctx.dependency("backend")
        rest = next((r for r in getattr(backend, "chain", None) or [] if r.id == "backend.rest"), None)
        dspace_ui = rest.detail.get("dspaceUI") if rest is not None else None
        if isinstance(dspace_ui, str) and dspace_ui.strip():
            return url_parts(dspace_ui).host
        return url_parts(public_url).host if public_url else None

    @staticmethod
    def _finish(ctx: CollectorContext, ladder: Ladder, metrics: dict[str, Any]) -> CollectorResult:
        """Append the backend dependency rung when the backend module is on."""
        if ctx.cfg.module_enabled("backend"):
            ladder.add(
                dependency_rung(
                    "frontend.dep.backend", "Backend dependency", ctx.dependency("backend"), ladder.ts
                )
            )
        return CollectorResult(checks=ladder.checks, metrics=metrics)


__all__ = [
    "CSR_NOTE",
    "HOME_MAX_BODY",
    "MARKER_RE",
    "NO_MARKUP",
    "FrontendCollector",
    "angular_version",
    "csr_note",
    "generator",
    "looks_like_dspace",
    "page_title",
    "public_host_headers",
    "rendering",
    "same_host",
    "wire_host",
]
