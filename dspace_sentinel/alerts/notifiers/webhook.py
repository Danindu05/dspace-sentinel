"""Generic webhook: ``POST`` the notification event as JSON. Never raises.

The payload leaves the machine, so the destination's certificate is always verified
(``dspace.verify_tls`` only covers DSpace's own URLs) and redirects are never followed: a
3xx answer is reported as a failure instead of re-sending the alert to another host.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from dspace_sentinel import __version__
from dspace_sentinel.util.httpc import describe_error, make_client

if TYPE_CHECKING:  # pragma: no cover
    from dspace_sentinel.config import Config

log = logging.getLogger(__name__)

TIMEOUT_S = 5.0


def payload_for(event: dict[str, Any]) -> dict[str, Any]:
    """The JSON body: the event plus ``source`` and ``version`` (JSON-safe copy), without the
    internal ``channels`` routing hint (see ``notifiers.dispatch``)."""
    body = {k: v for k, v in event.items() if k != "channels"}
    body.setdefault("source", "dspace-sentinel")
    body.setdefault("version", __version__)
    return body


async def send(
    cfg: Config, event: dict[str, Any], *, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    """POST ``event`` to ``notifiers.webhook.url``. Returns ``{ok, status_code, error}``."""
    url = (cfg.notifiers.webhook.url or "").strip()
    if not url:
        return {"ok": False, "status_code": None, "error": "webhook URL is not configured"}
    own = client is None
    http = client or make_client(timeout=TIMEOUT_S, verify=True, follow_redirects=False)
    try:
        response = await http.post(url, json=payload_for(event), follow_redirects=False)
    except Exception as exc:
        return {"ok": False, "status_code": None, "error": describe_error(exc)}
    finally:
        if own:
            try:
                await http.aclose()
            except Exception:  # pragma: no cover
                pass
    ok = 200 <= response.status_code < 300
    error = None
    if not ok:
        error = f"HTTP {response.status_code} from webhook"
        if response.is_redirect:
            error += " (redirects are not followed)"
    return {"ok": ok, "status_code": response.status_code, "error": error}


__all__ = ["TIMEOUT_S", "payload_for", "send"]
