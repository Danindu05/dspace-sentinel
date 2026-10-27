"""Collector registry: module id → collector class, resolved lazily.

Collector modules for the DSpace stack are added independently; a module whose file
does not exist yet (or fails to import) is *skipped with a log line*, never a crash.
"""

from __future__ import annotations

import importlib
import logging

from dspace_sentinel.collectors.base import Collector, CollectorContext, CollectorResult
from dspace_sentinel.config import Config

log = logging.getLogger(__name__)

REGISTRY: dict[str, str] = {
    "server": "dspace_sentinel.collectors.system:SystemCollector",
    "ports": "dspace_sentinel.collectors.ports:PortsCollector",
    "security": "dspace_sentinel.collectors.security:SecurityCollector",
    "postgres": "dspace_sentinel.collectors.postgres:PostgresCollector",
    "solr": "dspace_sentinel.collectors.solr:SolrCollector",
    "backend": "dspace_sentinel.collectors.backend:BackendCollector",
    "frontend": "dspace_sentinel.collectors.frontend:FrontendCollector",
    "nginx": "dspace_sentinel.collectors.nginx:NginxCollector",
    "ssl": "dspace_sentinel.collectors.ssl:SslCollector",
    "logs": "dspace_sentinel.collectors.logs:LogsCollector",
    "repository": "dspace_sentinel.collectors.repository:RepositoryCollector",
    "assetstore": "dspace_sentinel.collectors.assetstore:AssetstoreCollector",
}
"""module id → ``"dotted.module:ClassName"``. ``incidents`` has no collector (it is an alert feature)."""

COLLECTOR_ORDER: tuple[str, ...] = (
    "server",
    "postgres",
    "solr",
    "backend",
    "frontend",
    "nginx",
    "ssl",
    "ports",
    "security",
    "logs",
    "repository",
    "assetstore",
)
"""Run order within a cycle: ``security`` follows the stack, whose versions its software rung reads."""

_overrides: dict[str, type[Collector]] = {}


def register(module_id: str, cls: type[Collector]) -> None:
    """Register (or replace) the collector class for ``module_id`` at runtime (tests, plugins)."""
    _overrides[module_id] = cls


def resolve(module_id: str) -> type[Collector] | None:
    """Import and return the collector class for ``module_id``; ``None`` when unavailable.

    Missing modules (not built yet) and broken imports are logged and skipped.
    """
    if module_id in _overrides:
        return _overrides[module_id]
    target = REGISTRY.get(module_id)
    if target is None:
        log.warning("no collector registered for module %r", module_id)
        return None
    mod_name, _, cls_name = target.partition(":")
    try:
        module = importlib.import_module(mod_name)
    except ModuleNotFoundError as exc:
        if exc.name == mod_name:
            log.info("collector %s not available yet (%s missing); skipping", module_id, mod_name)
        else:
            log.warning("collector %s cannot be imported (%s); skipping", module_id, exc)
        return None
    except Exception as exc:  # broken module must not take the app down
        log.warning("collector %s failed to import: %s; skipping", module_id, exc)
        return None
    cls = getattr(module, cls_name, None)
    if cls is None or not isinstance(cls, type) or not issubclass(cls, Collector):
        log.warning("collector %s: %s has no Collector class %s; skipping", module_id, mod_name, cls_name)
        return None
    return cls


def enabled_collectors(cfg: Config) -> list[Collector]:
    """Instantiate the collectors for the modules that are on (the server collector always).

    In demo mode (``cfg.demo``) the :class:`~dspace_sentinel.collectors.demo.DemoSuite`
    replaces every real collector.
    """
    if cfg.demo:
        from dspace_sentinel.collectors.demo import DemoSuite

        return DemoSuite(cfg).collectors()
    out: list[Collector] = []
    for module_id in COLLECTOR_ORDER:
        if not cfg.module_enabled(module_id):
            continue
        cls = resolve(module_id)
        if cls is None:
            continue
        try:
            out.append(cls())
        except Exception as exc:
            log.warning("collector %s could not be created: %s; skipping", module_id, exc)
    return out


__all__ = [
    "COLLECTOR_ORDER",
    "REGISTRY",
    "Collector",
    "CollectorContext",
    "CollectorResult",
    "enabled_collectors",
    "register",
    "resolve",
]
