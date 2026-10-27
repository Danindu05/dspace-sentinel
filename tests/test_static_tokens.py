"""The one-token-file contract of the web UI (docs/DESIGN.md principle 3, SPEC section 12.2).

Every raw design value — colours, radii, type sizes, durations, spacing steps — lives in
``static/css/tokens.css``; ``app.css``, the modules under ``static/js`` and ``index.html`` consume
tokens through ``var(--…)``. These checks fail on a raw value that slips back in, and on a
``var(--name)`` that tokens.css does not define (a typo would silently fall back to nothing).

Allowed on purpose: 1-3 px optical nudges (``padding: 2px``, ``gap: 2px``), component geometry in
padding/margin (the ladder rail's 46 px), ``0``/``transparent``/``inherit`` and system colour keywords.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "dspace_sentinel" / "static"
TOKENS = STATIC / "css" / "tokens.css"
APP_CSS = STATIC / "css" / "app.css"
INDEX = STATIC / "index.html"
JS_FILES = sorted((STATIC / "js").rglob("*.js"))

COMMENT = re.compile(r"/\*.*?\*/", re.S)
HEX_COLOUR = re.compile(r"#[0-9A-Fa-f]{3,8}\b")
FUNC_COLOUR = re.compile(r"\b(?:rgba?|hsla?|hwb|oklab|oklch|lab|lch)\(", re.I)
DURATION = re.compile(r"(?<![\w.-])\d*\.?\d+m?s\b")
LENGTH = re.compile(r"(?<![\w.-])(\d*\.?\d+)(px|rem|em)\b")
ABSOLUTE = re.compile(r"(?<![\w.-])\d*\.?\d+(?:px|rem)\b")
VAR_USE = re.compile(r"var\(\s*--([a-z0-9-]+)")
TOKEN_DEF = re.compile(r"(?<![\w-])--([a-z0-9-]+)\s*:")


def _declarations(css: str) -> list[tuple[str, str, str]]:
    """``(selector, property, value)`` for every declaration of every innermost rule body."""
    css = COMMENT.sub("", css)
    out: list[tuple[str, str, str]] = []
    for match in re.finditer(r"([^{}]*)\{([^{}]*)\}", css):
        selector = match.group(1).strip().split("\n")[-1].strip()
        for decl in match.group(2).split(";"):
            if ":" not in decl:
                continue
            prop, value = decl.split(":", 1)
            out.append((selector, prop.strip().lower(), value.strip()))
    return out


def _app_decls() -> list[tuple[str, str, str]]:
    return _declarations(APP_CSS.read_text(encoding="utf-8"))


def test_app_css_has_no_raw_colours() -> None:
    bad = [
        f"{sel} {{ {prop}: {val} }}"
        for sel, prop, val in _app_decls()
        if HEX_COLOUR.search(val) or FUNC_COLOUR.search(val)
    ]
    assert not bad, "raw colours in app.css (add a token to tokens.css):\n" + "\n".join(bad)


def test_app_css_has_no_raw_durations() -> None:
    bad = [f"{sel} {{ {prop}: {val} }}" for sel, prop, val in _app_decls() if DURATION.search(val)]
    assert not bad, "raw durations in app.css (use var(--dur) / var(--dur-panel)):\n" + "\n".join(bad)


def test_app_css_radii_come_from_tokens() -> None:
    bad = []
    for sel, prop, val in _app_decls():
        if "radius" not in prop:
            continue
        rest = re.sub(r"var\([^)]*\)", "", val).split()
        if any(part not in ("0", "inherit") for part in rest):
            bad.append(f"{sel} {{ {prop}: {val} }}")
    hint = "use --r-card/--r-panel/--r-pill/--r-inner/--r-mark"
    assert not bad, f"raw radii in app.css ({hint}):\n" + "\n".join(bad)


def test_app_css_type_sizes_come_from_tokens() -> None:
    """Absolute sizes (px/rem) come from --fs-*; a relative optical tweak (monospace at 0.95em) may stay."""
    bad = [
        f"{sel} {{ {prop}: {val} }}"
        for sel, prop, val in _app_decls()
        if prop in ("font", "font-size") and ABSOLUTE.search(re.sub(r"var\([^)]*\)", "", val))
    ]
    assert not bad, "raw type sizes in app.css (use a --fs-* token):\n" + "\n".join(bad)


def _px_values(value: str) -> list[float]:
    stripped = re.sub(r"var\([^)]*\)", "", value)
    return [float(num) for num, unit in LENGTH.findall(stripped) if unit == "px"]


def test_app_css_spacing_stays_on_the_six_steps() -> None:
    """Gaps are tokens; padding and margin never use the off-scale 4-15 px band (use --s1..--s6)."""
    bad = []
    for sel, prop, val in _app_decls():
        pxs = _px_values(val)
        if prop in ("gap", "row-gap", "column-gap") and any(v > 2 for v in pxs):
            bad.append(f"{sel} {{ {prop}: {val} }}")
        elif prop.startswith(("padding", "margin")) and any(4 <= abs(v) <= 15 for v in pxs):
            bad.append(f"{sel} {{ {prop}: {val} }}")
    hint = "use --s1 4 / --s2 8 / --s3 16 / --s4 24 …"
    assert not bad, f"off-scale spacing in app.css ({hint}):\n" + "\n".join(bad)


@pytest.mark.parametrize("path", [INDEX, *JS_FILES], ids=lambda p: str(p.relative_to(STATIC)))
def test_markup_and_scripts_have_no_raw_colours_or_inline_lengths(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".js":
        # comments document values, they do not use them
        text = re.sub(r"^\s*(?://|\*).*$", "", text, flags=re.M)
        colours = re.findall(r"""['"`]\s*(#[0-9A-Fa-f]{3,8})\s*['"`]""", text) + FUNC_COLOUR.findall(text)
        inline = re.findall(r"style:\s*\{[^}]*?\d+px[^}]*\}", text)
    else:
        colours = HEX_COLOUR.findall(re.sub(r'href="[^"]*"', "", text)) + FUNC_COLOUR.findall(text)
        inline = re.findall(r'style="[^"]*\d+px[^"]*"', text)
    assert not colours, f"raw colours in {path.name}: {colours}"
    assert not inline, f"inline pixel styles in {path.name}: {inline}"


def test_every_token_used_is_defined() -> None:
    defined = set(TOKEN_DEF.findall(COMMENT.sub("", TOKENS.read_text(encoding="utf-8"))))
    missing: dict[str, set[str]] = {}
    for path in [APP_CSS, INDEX, *JS_FILES]:
        for name in VAR_USE.findall(path.read_text(encoding="utf-8")):
            if name not in defined:
                missing.setdefault(name, set()).add(path.name)
    assert not missing, f"var(--…) used but not defined in tokens.css: {missing}"


def test_tokens_define_the_design_vocabulary() -> None:
    """The documented vocabulary exists (SPEC 12.2 plus the tokens the fixes added)."""
    defined = set(TOKEN_DEF.findall(COMMENT.sub("", TOKENS.read_text(encoding="utf-8"))))
    expected = set(
        "s1 s2 s3 s4 s5 s6 r-card r-panel r-pill r-inner r-mark "
        "fs-11 fs-12 fs-13 fs-14 fs-15 fs-16 fs-18 fs-20 fs-22 fs-28 fs-40 "
        "on-accent on-accent-2 on-accent-line on-accent-wash on-accent-inset scrim paper "
        "dur dur-panel ease".split()
    )
    assert expected <= defined, f"missing tokens: {sorted(expected - defined)}"
