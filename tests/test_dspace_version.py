"""The DSpace version parser and the version windows the collectors and discovery rely on.

Every version string below was seen on a live site (``dspaceVersion`` of ``GET /server/api``),
and the REST roots are real payloads (``tests/fixtures/dspace-live/rest-roots.json``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dspace_sentinel import dspace_version as dv

LIVE = Path(__file__).parent / "fixtures" / "dspace-live"


def rest_roots() -> dict[str, dict]:
    data = json.loads((LIVE / "rest-roots.json").read_text(encoding="utf-8"))
    return {key: value for key, value in data.items() if key != "_source"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("DSpace 7.6", (7, 6, None, False, "7.6")),
        ("DSpace 7.6.1", (7, 6, 1, False, "7.6.1")),
        ("DSpace 7.6.8-SNAPSHOT", (7, 6, 8, True, "7.6.8-SNAPSHOT")),
        ("DSpace 8.1", (8, 1, None, False, "8.1")),
        ("DSpace 9.0", (9, 0, None, False, "9.0")),
        ("DSpace 10.0", (10, 0, None, False, "10.0")),
        ("DSpace 10.2-SNAPSHOT", (10, 2, None, True, "10.2-SNAPSHOT")),
        ("DSpace 11.0-SNAPSHOT", (11, 0, None, True, "11.0-SNAPSHOT")),
        ("9.4", (9, 4, None, False, "9.4")),  # the backend metric keeps the number only
        ("  dspace 8.5 ", (8, 5, None, False, "8.5")),
    ],
)
def test_parse_live_version_strings(raw: str, expected: tuple) -> None:
    v = dv.parse(raw)
    assert v is not None
    assert (v.major, v.minor, v.patch, v.snapshot, v.text) == expected
    assert str(v) == expected[-1]


@pytest.mark.parametrize("raw", ["DSpace unknown", "DSpace none", "", None, "DSpace", "cris-2024.02.04", 42])
def test_parse_rejects_what_is_not_a_version(raw: object) -> None:
    assert dv.parse(raw) is None


def test_every_live_rest_root_parses() -> None:
    roots = rest_roots()
    majors = {key: dv.parse(root["dspaceVersion"]).major for key, root in roots.items()}  # type: ignore[union-attr]
    assert majors == {"7.6.6": 7, "8.3": 8, "8.4": 8, "9.0": 9, "9.2": 9, "10.0": 10, "8.2-cris": 8}


def test_version_metrics_for_the_backend() -> None:
    roots = rest_roots()
    assert dv.version_metrics(roots["7.6.6"]) == {
        "dspace_major": 7,
        "dspace_minor": 6,
        "dspace_patch": 6,
        "dspace_snapshot": False,
        "cris_version": None,
    }
    assert dv.version_metrics(roots["8.2-cris"]) == {
        "dspace_major": 8,
        "dspace_minor": 2,
        "dspace_patch": None,
        "dspace_snapshot": False,
        "cris_version": "cris-2024.02.04",
    }
    # DSpace 10 adds "uniqueType" to the root: nothing else changes
    assert dv.version_metrics(roots["10.0"])["dspace_major"] == 10
    assert dv.version_metrics({"dspaceVersion": "DSpace unknown"}) == {
        "dspace_major": None,
        "dspace_minor": None,
        "dspace_patch": None,
        "dspace_snapshot": None,
        "cris_version": None,
    }
    assert dv.version_metrics(None)["dspace_major"] is None


@pytest.mark.parametrize(
    ("raw", "down"),
    [
        ("DSpace 7.6", False),  # no SEO health indicator before 7.6.4 / 8.2
        ("DSpace 7.6.3", False),
        ("DSpace 7.6.4", True),
        ("DSpace 7.6.7", True),
        ("DSpace 7.6.8-SNAPSHOT", True),  # 7.x never got the fix
        ("DSpace 8.0", False),
        ("DSpace 8.1", False),
        ("DSpace 8.2", True),
        ("DSpace 8.4", True),
        ("DSpace 8.5", False),  # UP_WITH_ISSUES from 8.5 / 9.4 / 10.1
        ("DSpace 9.0", True),
        ("DSpace 9.3", True),
        ("DSpace 9.4", False),
        ("DSpace 10.0", True),
        ("DSpace 10.1", False),
        ("DSpace 10.2-SNAPSHOT", False),
        ("DSpace 11.0-SNAPSHOT", False),
        ("DSpace unknown", False),
    ],
)
def test_seo_reports_down_window(raw: str, down: bool) -> None:
    assert dv.seo_reports_down(dv.parse(raw)) is down


@pytest.mark.parametrize(
    ("raw", "restricted"),
    [
        ("DSpace 7.6.7", False),
        ("DSpace 8.4", False),  # 8.4 requires ui.baseUrl but does not restrict SSR by Host
        ("DSpace 8.5", False),
        ("DSpace 9.2", False),
        ("DSpace 9.3", True),
        ("DSpace 9.4", True),
        ("DSpace 10.0", True),
        ("DSpace 10.1", True),
        ("DSpace 11.0-SNAPSHOT", True),
        ("DSpace none", False),
    ],
)
def test_ssr_host_restricted(raw: str, restricted: bool) -> None:
    assert dv.ssr_host_restricted(dv.parse(raw)) is restricted


def test_expected_solr_major_and_solr_versions() -> None:
    assert [dv.expected_solr_major(dv.parse(f"DSpace {v}")) for v in ("7.6.6", "8.5", "9.0", "10.1")] == [
        8,
        8,
        9,
        9,
    ]
    assert dv.expected_solr_major(dv.parse("DSpace 11.0-SNAPSHOT")) is None  # not known yet
    assert dv.expected_solr_major(None) is None
    assert dv.solr_major("8.11.4") == 8 and dv.solr_major("9.8.1") == 9 and dv.solr_major(" 9.10.1 ") == 9
    assert dv.solr_major(None) is None and dv.solr_major("garbage") is None


def test_cores_by_version() -> None:
    assert dv.REQUIRED_CORES == ("search", "statistics", "oai", "authority")
    assert dv.optional_cores(dv.parse("DSpace 7.6.6")) == ()
    assert dv.optional_cores(dv.parse("DSpace 8.3")) == ("qaevent", "suggestion")
    assert dv.optional_cores(dv.parse("DSpace 9.4")) == ("qaevent", "suggestion")
    assert dv.optional_cores(dv.parse("DSpace 10.0")) == ("qaevent", "suggestion", "audit")
    assert dv.optional_cores(None) == ("qaevent", "suggestion", "audit")  # unknown: accept all
    assert dv.dspace_cores(dv.parse("DSpace 7.6")) == ("search", "statistics", "oai", "authority")
    assert (
        len(dv.dspace_cores(dv.parse("DSpace 9.0"))) == 6
        and len(dv.dspace_cores(dv.parse("DSpace 10.1"))) == 7
    )


def test_end_of_life() -> None:
    assert dv.end_of_life(dv.parse("DSpace 7.6.7")) is True
    assert dv.end_of_life(dv.parse("DSpace 8.5")) is False
    assert dv.end_of_life(dv.parse("DSpace 10.1")) is False
    assert dv.end_of_life(None) is False
