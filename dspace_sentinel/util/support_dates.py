"""End-of-support dates for the software the Security module grades (the ``security.software`` rung).

Sources, checked by the lead on 2026-10-05 (:data:`CHECKED_ON`):

* DSpace: 7.x reached its end of life with the DSpace 10.0 release on 2026-05-28; 8, 9 and 10
  are supported (DSpace release policy, wiki.lyrasis.org; :func:`dspace_version.end_of_life`).
* Apache Solr: 8.0 – 8.11.0 ship a Log4j affected by Log4Shell (CVE-2021-44228), fixed in 8.11.1;
  releases older than 8 are no longer supported (solr.apache.org/security.html).
* PostgreSQL: the date of each major's final release (postgresql.org/support/versioning).
* Ubuntu: end of standard support of the LTS releases (ubuntu.com/about/release-cycle).
* Debian: end of LTS support (wiki.debian.org/LTS).

Every function returns one ``metrics.software`` entry, ``{name, version, until, status, note}``:
``until`` is the last supported day (``YYYY-MM-DD``) or ``None``; ``status`` is ``ok``, ``soon``
(support ends within :data:`SOON_DAYS` days), ``ended``, ``affected`` or ``unknown`` (no verdict
for a distribution or version the table does not know). Nothing here reads the system.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any

from dspace_sentinel import dspace_version

CHECKED_ON = "2026-10-05"
"""The day the lead checked every date below against its source."""

SOON_DAYS = 90
"""Support that ends within this many days is ``soon`` (still ok, mentioned in the message)."""

DSPACE_7_END_OF_LIFE = "2026-05-28"
"""DSpace 10.0's release: the end of life of DSpace 7.x."""

LOG4SHELL_NOTE = "affected by Log4Shell (CVE-2021-44228): upgrade to 8.11.1 or later"
NO_LONGER_SUPPORTED = "no longer supported"
NO_DATE_KNOWN = "no support date known"
SUPPORTED = "supported"

POSTGRES_FINAL_RELEASE: dict[int, str] = {
    11: "2023-11-09",
    12: "2024-11-14",
    13: "2025-11-13",
    14: "2026-11-12",
    15: "2027-11-11",
    16: "2028-11-09",
    17: "2029-11-08",
}
"""PostgreSQL major → the date of its final release (postgresql.org/support/versioning)."""

UBUNTU_END_OF_STANDARD_SUPPORT: dict[str, str] = {
    "18.04": "2023-05-31",
    "20.04": "2025-05-31",
    "22.04": "2027-04-30",
    "24.04": "2029-04-30",
}
"""Ubuntu LTS release → end of standard support (ubuntu.com/about/release-cycle)."""

DEBIAN_END_OF_LTS: dict[str, str] = {
    "10": "2024-06-30",
    "11": "2026-08-31",
    "12": "2028-06-30",
}
"""Debian release → end of LTS support (wiki.debian.org/LTS)."""

_NUMBERS = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")
_POSTGRES = re.compile(r"(?:postgresql\s+)?(\d+)(?:\.(\d+))?", re.IGNORECASE)


def entry(name: str, version: str, until: str | None, status: str, note: str) -> dict[str, Any]:
    """One ``metrics.software`` entry."""
    return {"name": name, "version": version, "until": until, "status": status, "note": note}


def dated_status(until: str | None, today: _dt.date) -> str:
    """``ok``, ``soon`` (``until`` within :data:`SOON_DAYS` days) or ``ended`` (``until`` has passed).

    ``until`` is the last supported day: on that day support has not ended yet.
    """
    if until is None:
        return "ok"
    end = _dt.date.fromisoformat(until)
    if today > end:
        return "ended"
    if (end - today).days <= SOON_DAYS:
        return "soon"
    return "ok"


def _numbers(raw: str) -> tuple[int, int, int] | None:
    m = _NUMBERS.search(raw)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)


def dspace_support(version: Any, today: _dt.date) -> dict[str, Any] | None:
    """The DSpace entry for the backend's ``dspace_version`` (``"9.4"``); ``None`` when unknown."""
    if not isinstance(version, str) or not version.strip():
        return None
    parsed = dspace_version.parse(version)
    if parsed is None:
        return None
    text = parsed.text
    if dspace_version.end_of_life(parsed):
        if parsed.major == 7:
            note = f"end of life on {DSPACE_7_END_OF_LIFE}"
            return entry("DSpace", text, DSPACE_7_END_OF_LIFE, "ended", note)
        return entry("DSpace", text, None, "ended", NO_LONGER_SUPPORTED)
    return entry("DSpace", text, None, "ok", SUPPORTED)


def solr_support(version: Any, today: _dt.date) -> dict[str, Any] | None:
    """The Solr entry for Solr's ``solr-spec-version`` (``"9.6.1"``); ``None`` when unknown."""
    if not isinstance(version, str) or not version.strip():
        return None
    numbers = _numbers(version)
    if numbers is None:
        return None
    text = version.strip()
    if numbers[0] < 8:
        return entry("Solr", text, None, "ended", NO_LONGER_SUPPORTED)
    if numbers[0] == 8 and numbers <= (8, 11, 0):
        return entry("Solr", text, None, "affected", LOG4SHELL_NOTE)
    return entry("Solr", text, None, "ok", SUPPORTED)


def postgres_support(version: Any, today: _dt.date) -> dict[str, Any] | None:
    """The PostgreSQL entry for the database's ``version`` (``"PostgreSQL 15.8 (Ubuntu …)"``)."""
    if not isinstance(version, str) or not version.strip():
        return None
    m = _POSTGRES.search(version)
    if not m:
        return None
    major = int(m.group(1))
    text = f"{major}.{m.group(2)}" if m.group(2) is not None else str(major)
    until = POSTGRES_FINAL_RELEASE.get(major)
    if until is not None:
        return entry("PostgreSQL", text, until, dated_status(until, today), f"end of support on {until}")
    if major < min(POSTGRES_FINAL_RELEASE):
        return entry("PostgreSQL", text, None, "ended", NO_LONGER_SUPPORTED)
    # newer than the newest major in the table: supported longer than every major in it
    return entry("PostgreSQL", text, None, "ok", SUPPORTED)


def os_support(os_release: dict[str, str] | None, today: _dt.date) -> dict[str, Any] | None:
    """The operating system's entry from ``/etc/os-release`` (``ID``, ``VERSION_ID``, ``NAME``)."""
    if not os_release:
        return None
    os_id = (os_release.get("ID") or "").strip().lower()
    version = (os_release.get("VERSION_ID") or "").strip()
    if os_id == "ubuntu":
        name, table, kind = "Ubuntu", UBUNTU_END_OF_STANDARD_SUPPORT, "end of standard support"
    elif os_id == "debian":
        name, table, kind = "Debian", DEBIAN_END_OF_LTS, "end of LTS support"
    else:
        name = (os_release.get("NAME") or os_id or "").strip()
        if not name:
            return None
        return entry(name, version, None, "unknown", NO_DATE_KNOWN)
    until = table.get(version)
    if until is None:
        return entry(name, version, None, "unknown", NO_DATE_KNOWN)
    return entry(name, version, until, dated_status(until, today), f"{kind} on {until}")


def problem_sentence(item: dict[str, Any]) -> str:
    """What is wrong with an ``ended`` or ``affected`` entry, as the rung's message says it.

    ``"Ubuntu 20.04 reached end of standard support on 2025-05-31"``, ``"Solr 8.8.2 is affected by
    Log4Shell (CVE-2021-44228): upgrade to 8.11.1 or later"``, ``"Solr 7.7.3 is no longer supported"``.
    """
    subject = f"{item['name']} {item['version']}".strip()
    if item.get("status") == "ended" and item.get("until"):
        return f"{subject} reached {item['note']}"
    return f"{subject} is {item['note']}"


def soon_sentence(item: dict[str, Any]) -> str:
    """``"PostgreSQL 13.4 support ends 2025-11-13"`` for a ``soon`` entry."""
    return f"{item['name']} {item['version']} support ends {item['until']}".strip()


__all__ = [
    "CHECKED_ON",
    "DEBIAN_END_OF_LTS",
    "DSPACE_7_END_OF_LIFE",
    "LOG4SHELL_NOTE",
    "NO_DATE_KNOWN",
    "NO_LONGER_SUPPORTED",
    "POSTGRES_FINAL_RELEASE",
    "SOON_DAYS",
    "SUPPORTED",
    "UBUNTU_END_OF_STANDARD_SUPPORT",
    "dated_status",
    "dspace_support",
    "entry",
    "os_support",
    "postgres_support",
    "problem_sentence",
    "solr_support",
    "soon_sentence",
]
