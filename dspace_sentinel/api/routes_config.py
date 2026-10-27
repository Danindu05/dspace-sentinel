"""``/api/config``, ``/api/modules`` and ``/api/actions/*``."""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from dspace_sentinel.api.deps import AppContext, get_ctx
from dspace_sentinel.api.routes_status import build_meta, build_status
from dspace_sentinel.config import (
    MODULE_IDS,
    PERSONAS,
    Config,
    apply_persona,
    masked,
    merge_patch,
    save_config,
    server_access,
)
from dspace_sentinel.discovery import discover, restore_secrets

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["config"])

SERVER_READ_ONLY = "server settings are set in config/sentinel.yaml; restart to apply"

MODULE_LABELS: dict[str, str] = {
    "ports": "Ports",
    "security": "Security",
    "postgres": "PostgreSQL",
    "solr": "Solr",
    "backend": "DSpace Backend",
    "frontend": "DSpace Frontend",
    "nginx": "nginx",
    "ssl": "SSL certificate",
    "logs": "Logs",
    "repository": "Repository stats",
    "assetstore": "Assetstore",
    "incidents": "Incidents",
}
"""Module names as the More › Modules toggles show them (for the events log)."""

DISCOVERY_TTL_S = 1800.0
"""How long the secrets of the last discovery can be applied by sending their mask back."""


def _with_discovered_secrets(ctx: AppContext, patch: dict[str, Any]) -> dict[str, Any]:
    """``patch`` with the masked DB password / Solr URL of a recent discovery restored (server-side)."""
    remembered = getattr(ctx.request.app.state, "discovery", None)
    if not remembered or time.monotonic() - remembered[0] > DISCOVERY_TTL_S:
        return patch
    return restore_secrets(patch, ctx.cfg, remembered[1])


def reject_server_changes(old: Config, new: Config) -> None:
    """400 when a patch would change ``server.*`` (bind address, port, auth token, allowed hosts).

    Those settings decide who can reach the API, so a client of the API must not be able to widen
    them; sending the current values back (a GET → PATCH round trip) is fine. ``server.timezone``
    decides nothing of the kind and may change (:data:`~dspace_sentinel.config.API_SERVER_KEYS`).
    """
    if server_access(new.server) != server_access(old.server):
        raise HTTPException(status_code=400, detail=SERVER_READ_ONLY)


def apply_config(ctx: AppContext, new_cfg: Config, reason: str) -> Config:
    """Persist ``new_cfg``, swap it into the app and hot-reload the scheduler/engines."""
    save_config(new_cfg, ctx.config_path)
    ctx.cfg = new_cfg
    try:
        ctx.scheduler.reload(new_cfg)
    except Exception:
        log.exception("scheduler reload failed")
    try:
        ctx.store.add_event("config", None, reason)
    except Exception:
        log.exception("could not record config event")
    return new_cfg


def _changed_keys(patch: dict[str, Any], prefix: str = "") -> list[str]:
    keys: list[str] = []
    for key, value in patch.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            keys.extend(_changed_keys(value, f"{path}."))
        else:
            keys.append(path)
    return keys


@router.get("/config")
async def get_config(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """The effective configuration with secrets masked."""
    return masked(ctx.cfg)


@router.patch("/config")
async def patch_config(
    patch: dict[str, Any] = Body(...), ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    """Deep-merge a partial config, validate, save atomically and hot-reload.

    Secrets sent as the mask string keep their current value (or, for a patch applying a recent
    discovery, take the discovered value; see :func:`~dspace_sentinel.discovery.restore_secrets`).
    """
    if isinstance(patch, dict):
        patch = _with_discovered_secrets(ctx, patch)
    try:
        new_cfg = merge_patch(ctx.cfg, patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    reject_server_changes(ctx.cfg, new_cfg)
    keys = ", ".join(_changed_keys(patch)[:12]) or "(nothing)"
    apply_config(ctx, new_cfg, f"Configuration updated: {keys}")
    return masked(ctx.cfg)


@router.get("/modules")
async def get_modules(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """``{modules: {id: bool}}``."""
    return {"modules": ctx.cfg.modules.as_dict()}


@router.patch("/modules")
async def patch_modules(
    body: dict[str, Any] = Body(...), ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    """Toggle modules (``{"postgres": true}``); the scheduler hot-reloads."""
    unknown = [k for k in body if k not in MODULE_IDS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown module(s): {', '.join(unknown)}")
    if not all(isinstance(v, bool) for v in body.values()):
        raise HTTPException(status_code=400, detail="module values must be booleans")
    try:
        new_cfg = merge_patch(ctx.cfg, {"modules": body})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    changes = ", ".join(f"{MODULE_LABELS.get(k, k)} turned {'on' if v else 'off'}" for k, v in body.items())
    apply_config(ctx, new_cfg, changes or "Modules unchanged")
    return {"modules": ctx.cfg.modules.as_dict()}


@router.post("/actions/check-now")
async def action_check_now(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Run every enabled collector immediately and return the fresh ``/api/status`` payload."""
    await ctx.scheduler.run_all_now()
    return build_status(ctx.cfg, ctx.state, ctx.store, ctx.scheduler, ctx.alert_engine)


@router.post("/actions/telegram-test")
async def action_telegram_test(
    body: dict[str, Any] = Body(default_factory=dict), ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    """Send the "connected" test message through every Telegram bot, or the one ``{"id": …}`` names.

    Each destination's bot answers ``getMe`` first, then gets the message (whether or not Telegram
    is switched on, whatever its ``min_severity``). Answers ``{ok, bot_name, error, results:
    [{id, label, chat_id, bot_name, ok, error}]}``; 400 for an id that is not configured. In demo
    mode nothing is sent (``skipped: "demo"``); the message is written to the events log as a
    ``notify.demo`` event instead, like every other would-be delivery of the demo.
    """
    try:
        from dspace_sentinel.alerts import messages
        from dspace_sentinel.alerts.notifiers import telegram  # provided by the alerts module
    except ImportError:
        return {
            "ok": False,
            "bot_name": None,
            "error": "Telegram notifier not available in this build",
            "results": [],
        }
    wanted = body.get("id")
    if wanted is not None:
        wanted = str(wanted).strip()
        if not any(chat.id == wanted for chat in ctx.cfg.notifiers.telegram.chats):
            raise HTTPException(status_code=400, detail=f"no Telegram bot with id {wanted!r} is configured")
    if ctx.cfg.demo:
        html = messages.test_html(ctx.cfg.hostname)
        text = messages.to_plain(html)
        ctx.store.add_event(
            "notify.demo",
            None,
            f"Test message (demo, not sent): {text}",
            {"kind": "test", "channels": ["telegram"], "html": html, "text": text},
        )
        return {"ok": False, "bot_name": None, "error": telegram.DEMO_ERROR, "skipped": "demo", "results": []}
    try:
        result = await telegram.send_test(ctx.cfg, destination_id=wanted)
    except Exception as exc:
        return {"ok": False, "bot_name": None, "error": str(exc), "results": []}
    return {
        "ok": bool(result.get("ok")),
        "bot_name": result.get("bot_name"),
        "error": result.get("error"),
        "results": list(result.get("results") or []),
    }


@router.post("/actions/email-test")
async def action_email_test(
    body: dict[str, Any] = Body(default_factory=dict), ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    """Send the test email to every recipient, or the one ``{"address": …}`` names.

    Sent whether or not email is switched on, whatever the recipients' ``min_severity``, as one
    message. Answers ``{ok, error, results: [{address, label, ok, error}]}``; ``ok: false`` with
    "email is not set up: …" when the login or a recipient is missing; 400 for an address that is
    not a recipient. In demo mode nothing is sent (``skipped: "demo"``) and the email is written
    to the events log as a ``notify.demo`` event.
    """
    try:
        from dspace_sentinel.alerts import email_messages
        from dspace_sentinel.alerts.notifiers import email as mail
    except ImportError:
        return {"ok": False, "error": "the email notifier is not available in this build", "results": []}
    wanted = body.get("address")
    if wanted is not None:
        wanted = str(wanted).strip()
        if not any(r.address.lower() == wanted.lower() for r in ctx.cfg.notifiers.email.recipients):
            raise HTTPException(status_code=400, detail=f"no email recipient {wanted!r} is configured")
    if ctx.cfg.demo:
        content = email_messages.connected_email(ctx.cfg.hostname)
        ctx.store.add_event(
            "notify.demo",
            None,
            f"Test email (demo, not sent): {content.subject}",
            {
                "kind": "test",
                "channels": ["email"],
                "subject": content.subject,
                "html": content.html,
                "text": content.text,
            },
        )
        return {"ok": False, "error": mail.DEMO_ERROR, "skipped": "demo", "results": []}
    try:
        result = await mail.send_test(ctx.cfg, address=wanted)
    except Exception as exc:  # send_test never raises; belt and braces
        return {"ok": False, "error": str(exc), "results": []}
    return {
        "ok": bool(result.get("ok")),
        "error": result.get("error"),
        "results": list(result.get("results") or []),
    }


@router.post("/actions/discover")
async def action_discover(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Look around the machine: ``{findings, suggested_config, detected_persona, ...}``.

    Secrets found in DSpace's configuration are masked in the answer; the real values stay in
    memory for :data:`DISCOVERY_TTL_S` so that applying the suggestion (setup, PATCH) works.
    """
    try:
        result = await asyncio.wait_for(discover(ctx.cfg), timeout=45.0)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="discovery timed out") from exc
    ctx.request.app.state.discovery = (time.monotonic(), copy.deepcopy(result.suggested_config))
    return result.to_dict()


@router.post("/actions/setup")
async def action_setup(
    body: dict[str, Any] = Body(default_factory=dict), ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    """Finish onboarding: apply ``persona`` preset + ``config_patch``, mark setup completed, return meta."""
    persona = body.get("persona") or ""
    patch = body.get("config_patch")
    if patch is None:
        patch = {}
    if persona and persona not in PERSONAS:
        raise HTTPException(
            status_code=400, detail=f"unknown persona {persona!r}; use one of {', '.join(PERSONAS)}"
        )
    if not isinstance(patch, dict):
        raise HTTPException(status_code=400, detail="config_patch must be an object")
    patch = _with_discovered_secrets(ctx, patch)
    try:
        new_cfg = apply_persona(ctx.cfg, persona) if persona else ctx.cfg
        patch = dict(patch)
        patch.setdefault("setup", {})
        patch["setup"] = {**patch["setup"], "completed": True}
        if persona:
            patch["setup"]["persona"] = persona
        new_cfg = merge_patch(new_cfg, patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    reject_server_changes(ctx.cfg, new_cfg)
    apply_config(ctx, new_cfg, f"Setup completed (persona: {persona or 'custom'})")
    return build_meta(ctx.cfg, ctx.started_ts)


@router.post("/actions/clear-history")
async def action_clear_history(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Wipe samples, checks, history, alerts, incidents and events (config and reports are kept)."""
    await asyncio.to_thread(ctx.store.clear_history)
    ctx.state.reset()
    ctx.store.add_event("config", None, "History cleared")
    ctx.bus.publish("status", {"reason": "clear-history"})
    return {"ok": True, "counts": ctx.store.table_counts()}


__all__ = [
    "DISCOVERY_TTL_S",
    "MODULE_LABELS",
    "SERVER_READ_ONLY",
    "apply_config",
    "reject_server_changes",
    "router",
]
