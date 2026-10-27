"""What differs between DSpace 7, 8, 9 and 10, keyed on the version the REST API reports.

``GET /server/api`` reports ``dspaceVersion`` as ``"DSpace "`` plus the Maven version of
dspace-api: ``"DSpace 7.6"`` (x.0 releases print two numbers), ``"DSpace 7.6.6"``,
``"DSpace 7.6.8-SNAPSHOT"``, ``"DSpace 10.2-SNAPSHOT"``, and ``"DSpace unknown"`` or
``"DSpace none"`` when the build cannot tell. :func:`parse` reads it once; the helpers answer the
version-dependent questions the collectors and discovery ask:

* :func:`seo_reports_down` — the SEO health indicator (sitemaps, robots.txt, server-side
  rendering) makes the anonymous actuator answer ``DOWN`` on a healthy site in 7.6.4–7.6.x,
  8.2–8.4, 9.0–9.3 and 10.0; 8.5, 9.4 and 10.1 report such problems as ``UP_WITH_ISSUES``;
* :func:`ssr_host_restricted` — from 9.3 the UI renders on the server only for the host of its
  ``ui.baseUrl`` (CVE-2026-27739) and serves the client-side shell to any other ``Host``;
* :func:`expected_solr_major` — DSpace 7 and 8 run on Solr 8, DSpace 9 and 10 on Solr 9;
* :func:`optional_cores` — DSpace 8 added the ``qaevent`` and ``suggestion`` Solr cores and
  DSpace 10 the ``audit`` core, on top of the four every version needs (:data:`REQUIRED_CORES`);
* :func:`end_of_life` — 7.x reached its end of life with 10.0 (7.6.7 was the final release).

Nothing here talks to DSpace: it only reads version strings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

REQUIRED_CORES: tuple[str, ...] = ("search", "statistics", "oai", "authority")
"""The Solr cores every DSpace 7–10 installs (each behind ``solr.multicorePrefix``)."""

OPTIONAL_CORES: dict[str, int] = {"qaevent": 8, "suggestion": 8, "audit": 10}
"""Optional Solr core → the first DSpace major that ships it."""

SEO_FIXED_IN = "8.5 / 9.4 / 10.1"
"""The releases whose SEO health indicator reports ``UP_WITH_ISSUES`` instead of ``DOWN``."""

FINAL_7X_RELEASE = "7.6.7"
SUPPORTED_MAJORS: tuple[int, ...] = (8, 9, 10)
"""DSpace majors with security support (September 2026): the last three."""

SEO_DOWN_WINDOWS: tuple[tuple[tuple[int, int, int], tuple[int, int, int]], ...] = (
    ((7, 6, 4), (7, 7, 0)),
    ((8, 2, 0), (8, 5, 0)),
    ((9, 0, 0), (9, 4, 0)),
    ((10, 0, 0), (10, 1, 0)),
)
"""``[from, to)`` version ranges whose SEO health indicator answers ``DOWN`` for sitemap,
robots.txt or SSR problems (7.6.4–7.6.x, 8.2–8.4, 9.0–9.3, 10.0)."""

SSR_HOST_RESTRICTED_FROM: tuple[int, int, int] = (9, 3, 0)

_VERSION = re.compile(
    r"^(?:dspace\s*)?v?(?P<major>\d{1,2})(?:\.(?P<minor>\d+))?(?:\.(?P<patch>\d+))?"
    r"(?:-(?P<qualifier>[0-9A-Za-z][0-9A-Za-z.\-]*))?$",
    re.IGNORECASE,
)
_SOLR_MAJOR = re.compile(r"^\s*(\d{1,2})\.\d+")


@dataclass(frozen=True)
class DSpaceVersion:
    """A parsed DSpace version: ``7.6.8-SNAPSHOT`` → ``major=7, minor=6, patch=8, snapshot=True``."""

    major: int
    minor: int = 0
    patch: int | None = None
    """``None`` when the version has two numbers only (``9.4``, ``7.6``)."""
    qualifier: str = ""
    """What follows the hyphen (``SNAPSHOT``, ``beta5``); empty for a release."""

    @property
    def snapshot(self) -> bool:
        """A development build (``-SNAPSHOT``) rather than a release."""
        return self.qualifier.upper() == "SNAPSHOT"

    @property
    def key(self) -> tuple[int, int, int]:
        """``(major, minor, patch)`` for comparisons (a missing patch counts as 0)."""
        return (self.major, self.minor, self.patch or 0)

    @property
    def text(self) -> str:
        """The version as DSpace prints it, without the ``DSpace`` prefix: ``7.6.8-SNAPSHOT``."""
        out = f"{self.major}.{self.minor}"
        if self.patch is not None:
            out += f".{self.patch}"
        if self.qualifier:
            out += f"-{self.qualifier}"
        return out

    def __str__(self) -> str:
        return self.text


def parse(raw: Any) -> DSpaceVersion | None:
    """``"DSpace 9.4"`` (or ``"9.4"``) → :class:`DSpaceVersion`; ``None`` for anything else.

    Accepts ``7.6``, ``7.6.1``, ``7.6.8-SNAPSHOT``, ``10.2-SNAPSHOT``; ``"DSpace unknown"``,
    ``"DSpace none"``, empty values and non-strings give ``None``.
    """
    if not isinstance(raw, str):
        return None
    m = _VERSION.match(raw.strip())
    if not m:
        return None
    patch = m.group("patch")
    return DSpaceVersion(
        major=int(m.group("major")),
        minor=int(m.group("minor") or 0),
        patch=int(patch) if patch is not None else None,
        qualifier=m.group("qualifier") or "",
    )


def version_metrics(root: Any) -> dict[str, Any]:
    """The backend's version metrics from a REST root payload (``GET /server/api``).

    ``dspace_major``, ``dspace_minor``, ``dspace_patch`` (``None`` for two-number versions),
    ``dspace_snapshot`` and ``cris_version`` (DSpace-CRIS adds ``crisVersion``); every value is
    ``None`` when the root carries no readable version.
    """
    info = root if isinstance(root, dict) else {}
    v = parse(info.get("dspaceVersion"))
    cris = info.get("crisVersion")
    return {
        "dspace_major": v.major if v else None,
        "dspace_minor": v.minor if v else None,
        "dspace_patch": v.patch if v else None,
        "dspace_snapshot": v.snapshot if v else None,
        "cris_version": str(cris) if isinstance(cris, str) and cris.strip() else None,
    }


def seo_reports_down(v: DSpaceVersion | None) -> bool:
    """True when this DSpace answers an anonymous ``DOWN`` for SEO problems on a healthy site."""
    if v is None:
        return False
    return any(lo <= v.key < hi for lo, hi in SEO_DOWN_WINDOWS)


def ssr_host_restricted(v: DSpaceVersion | None) -> bool:
    """True from 9.3: the UI renders on the server only for the host of its ``ui.baseUrl``."""
    return v is not None and v.key >= SSR_HOST_RESTRICTED_FROM


def expected_solr_major(v: DSpaceVersion | None) -> int | None:
    """The Solr major a DSpace version is built for: 7/8 → 8, 9/10 → 9 (``None`` when unknown)."""
    if v is None:
        return None
    if v.major in (7, 8):
        return 8
    if v.major in (9, 10):
        return 9
    return None


def solr_major(raw: Any) -> int | None:
    """``"9.8.1"`` (``lucene.solr-spec-version``) → 9; ``None`` when it is not a version."""
    if not isinstance(raw, str):
        return None
    m = _SOLR_MAJOR.match(raw)
    return int(m.group(1)) if m else None


def optional_cores(v: DSpaceVersion | None) -> tuple[str, ...]:
    """The optional Solr cores this DSpace ships (every known one when the version is unknown)."""
    return tuple(core for core, since in OPTIONAL_CORES.items() if v is None or v.major >= since)


def dspace_cores(v: DSpaceVersion | None) -> tuple[str, ...]:
    """Every Solr core this DSpace installs: :data:`REQUIRED_CORES` plus :func:`optional_cores`."""
    return REQUIRED_CORES + optional_cores(v)


def end_of_life(v: DSpaceVersion | None) -> bool:
    """True for a major without security support any more (7.x and older, September 2026)."""
    return v is not None and v.major < min(SUPPORTED_MAJORS)


__all__ = [
    "FINAL_7X_RELEASE",
    "OPTIONAL_CORES",
    "REQUIRED_CORES",
    "SEO_DOWN_WINDOWS",
    "SEO_FIXED_IN",
    "SSR_HOST_RESTRICTED_FROM",
    "SUPPORTED_MAJORS",
    "DSpaceVersion",
    "dspace_cores",
    "end_of_life",
    "expected_solr_major",
    "optional_cores",
    "parse",
    "seo_reports_down",
    "solr_major",
    "ssr_host_restricted",
    "version_metrics",
]
