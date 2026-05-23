"""Pill `.live` modifier collision guard (Bug 3, 2026-05-24).

qarl-direct bug 2026-05-24: the topbar `BROADCASTING` pill + the
sidebar Flock card `live` pill rendered as 76x77 oversized ovals with
the pulse dot + text stacked vertically, instead of the spec'd
compact inline-flex pill (~50x21 box, padding 3px 8px, font-size
10.5px, dot 6px inline).

Root cause (QA Playwright dump, 2026-05-24): the Live panel root
selector was a bare `.live { display: flex; flex-direction: column;
padding: 1.25rem 1.5rem; max-width: 48rem }` at
`ui/styles.css` ~L3820. The bare `.live` class selector matched ANY
element with `class="live"` — including the status pill
`<span class="om-pill live">`. `.om-pill.live` only set background +
color, so display/flex-direction/padding fell through to the bare
`.live` rule. Same specificity (0,1,0) + later-in-file → cascade
elected `.live` for the pill.

Origin trail: the Stream→Live rename arc (commits 3a4fd22 + 860275d
the prior night) renamed the panel container class to `.live`
without grepping for collisions against the existing
`.om-pill.live` status-pill modifier.

Fix: tag-qualify the panel selector — `section.live { ... }` —
which physically cannot match `<span class="om-pill live">` because
the pill is a `<span>`, not a `<section>`. The pill's `display/
flex-direction/padding` then fall through cleanly to the `.om-pill`
base rule.

A future "helpful" refactor could break the invariant silently:
- Restoring the bare `.live { ... }` selector at the panel-root
  block re-opens the collision and re-introduces the oversized
  pill bug across topbar + sidebar.
- Changing the panel root element from `<section class="live">` to
  e.g. `<div class="live">` makes the tag-qualified selector
  no-match, so the panel loses its layout (different breakage but
  still bad). The static-parse test asserts the section element
  type stays `<section>`.
- Dropping the `display: inline-flex` from `.om-pill { ... }` base
  rule re-opens the original `display: inline` UA default on the
  pill `<span>`s (which would NOT stack vertically by itself, but
  would lose the padding/gap/centering that `inline-flex` enables).

Static parse — same shape as the D2 / M5 / H4 / font-load /
playlist-delete (Bug 2) closures.
"""

from __future__ import annotations

import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_STYLES_CSS = _PROJECT_ROOT / "ui" / "styles.css"
_LIVE_PANEL_JS = _PROJECT_ROOT / "ui" / "src" / "live-panel.js"


def _read_css_source_stripped(path: Path) -> str:
    """Read a CSS file and strip `/* */` block comments so narrative
    mentions of selectors inside comments (like the comment block
    we just added that documents `bare .live`) don't false-trip the
    bare-selector regex. CSS has no `//` line comments to worry
    about."""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return text


def test_styles_css_uses_tag_qualified_section_live_selector() -> None:
    """The panel root selector must be `section.live`, not bare `.live`.
    The bare form collides with the `.om-pill.live` status-pill
    modifier (same specificity, later-in-file → cascade wins on
    `display/flex-direction/padding`)."""
    css = _read_css_source_stripped(_STYLES_CSS)
    assert re.search(
        r"(?m)^\s*section\.live\s*\{", css
    ), (
        "Expected tag-qualified `section.live { ... }` selector in "
        "styles.css for the Live panel root. The pill collision guard "
        "relies on this being tag-qualified — see test docstring."
    )


def test_styles_css_does_not_contain_bare_live_selector() -> None:
    """A bare `.live { ... }` selector at the start of a rule block
    is the original bug — it cascades onto every `class="live"`
    element including the `.om-pill.live` status pills. The
    tag-qualified `section.live` form replaces it."""
    css = _read_css_source_stripped(_STYLES_CSS)
    # Match a bare `.live` followed by either whitespace+`{` or
    # `, ` (selector-list continuation). We DON'T match `.live-foo`
    # (the `-` is part of the class name) or `.something .live` (a
    # descendant selector — qualified by ancestor, not bare).
    bare_live = re.search(
        r"(?m)^\s*\.live\s*[,{]", css
    )
    assert bare_live is None, (
        "Bare `.live { ... }` selector found in styles.css — this "
        "re-introduces the Bug 3 cascade collision with the "
        "`.om-pill.live` status-pill modifier. Use a tag-qualified "
        "form like `section.live { ... }` instead. Found near: "
        f"{css[max(0, bare_live.start() - 30):bare_live.end() + 30] if bare_live else ''!r}"
    )


def test_styles_css_om_pill_base_sets_inline_flex() -> None:
    """The pill base rule MUST set `display: inline-flex` so the
    tag-qualified panel selector's no-match on `<span>` pills means
    the pill falls through to a correct layout (not UA default)."""
    css = _read_css_source_stripped(_STYLES_CSS)
    # Find the `.om-pill { ... }` rule block (NOT `.om-pill.live` or
    # `.om-pill .om-pulse`).
    pill_rule = re.search(
        r"(?m)^\.om-pill\s*\{([^}]*)\}", css
    )
    assert pill_rule is not None, (
        "Expected `.om-pill { ... }` base rule in styles.css."
    )
    body = pill_rule.group(1)
    assert "display: inline-flex" in body, (
        "`.om-pill` base rule must declare `display: inline-flex` so "
        "pills fall through to a correct layout when the tag-qualified "
        "`section.live` selector doesn't match them. Got block body: "
        f"{body!r}"
    )
    assert "padding: 3px 8px" in body, (
        "`.om-pill` base rule must declare `padding: 3px 8px` so "
        "the compact pill geometry is preserved. Got: {body!r}"
    )


def test_styles_css_om_pill_live_modifier_only_sets_color_and_background() -> None:
    """`.om-pill.live` is a STATUS-COLOR modifier — it must NOT
    re-declare `display/padding/flex-direction`, since those come
    from the `.om-pill` base. If a future refactor adds layout
    properties here, the pill's base layout could be silently
    overridden."""
    css = _read_css_source_stripped(_STYLES_CSS)
    pill_live = re.search(
        r"(?m)^\.om-pill\.live\s*\{([^}]*)\}", css
    )
    assert pill_live is not None, (
        "Expected `.om-pill.live { ... }` modifier rule in styles.css."
    )
    body = pill_live.group(1)
    forbidden = ("display:", "padding:", "flex-direction:", "max-width:", "gap:")
    for prop in forbidden:
        assert prop not in body, (
            f"`.om-pill.live` modifier must NOT declare `{prop}` — that "
            f"belongs on the `.om-pill` base rule. Got block body: "
            f"{body!r}"
        )


def test_live_panel_section_element_type_unchanged() -> None:
    """The tag-qualified `section.live` selector only matches if the
    panel root element stays a `<section>`. If a future refactor
    changes it to `<div class="live">` etc., the panel loses its
    layout (different bug but still bad)."""
    src = _LIVE_PANEL_JS.read_text(encoding="utf-8")
    assert re.search(
        r'<section\s+class="live"\s*>', src
    ), (
        "Expected `<section class=\"live\">` panel root in "
        "ui/src/live-panel.js. The tag-qualified `section.live` CSS "
        "selector relies on this element type — if the panel root "
        "becomes a different element type, the section.live rule "
        "stops matching and the panel layout breaks."
    )
