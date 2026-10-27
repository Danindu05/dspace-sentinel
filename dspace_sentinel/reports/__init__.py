"""Reports: analytics → HTML/Markdown/JSON files in ``data/reports`` (+ weekly schedule)."""

from __future__ import annotations

from dspace_sentinel.reports.generator import generate, generate_for_range, parse_range_string

__all__ = ["generate", "generate_for_range", "parse_range_string"]
