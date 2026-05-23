"""D2 audit closure (2026-05-23): chip-pill universal-application lock.

QA audit item D2 / `qa/captures/ui-popup-consistency-recon-2026-05-17.md`
flagged 17 popup-shaped UI surfaces across 4 visual patterns. qarl
picked Interpretation X (chip-pill universal-application) on
2026-05-23. Verification against HEAD showed the work had silently
shipped on 2026-05-19 — the canonical `.om-pulldown` class was
extracted and propagated across every Pattern-B `<select>` + the
sole Pattern-D site (per the CSS comment block at
`ui/styles.css:622-633`). 14 of 15 `<select>` elements in
`ui/src/*.js` already wear `.om-pulldown`; the 1 exception is a
documented hidden `<select>` driven by the Pattern-C font-picker
(operator never sees its chrome).

This test codifies the current state so future drift is caught at
CI time:

- Every `<select>` element in `ui/src/*.js` (excluding test files)
  must EITHER wear `.om-pulldown` OR be the documented hidden
  font-family pattern at `editor.js`'s `field-font-family`.
- A failure here means a new `<select>` was added without chip-pill
  and the operator-visible chrome will be inconsistent with the
  rest of the UI's pulldown surfaces.

Static file-parse — no runtime dependencies, no vitest infra needed
(per the vitest-virtiofs-wedge documented in the Slice 3 commit).
Same shape as `test_systemd_unit_whitelists_af_netlink` (Slice 4
Test A) for the same rationale: catch the config/markup regression
at the file layer.
"""

from __future__ import annotations

import re
from pathlib import Path

# `<select>` opening tag. Captures the attributes between `<select`
# and the closing `>` so we can pull the `class="..."` value out.
# Non-greedy so a `<select>` followed by another `>` later in the
# line doesn't over-match.
_SELECT_OPEN = re.compile(r"<select\b([^>]*?)>", re.DOTALL)

# `class="..."` (or `class='...'`) — extracts the literal value. We
# don't try to handle template-interpolated classes (no `class=${...}`
# patterns exist in the repo today; if one appears, this test will
# fail to find a `class=` group and surface the unrecognized shape).
_CLASS_ATTR = re.compile(r"""class\s*=\s*['"]([^'"]*)['"]""")

# The documented hidden exception (recon row #8 + the Pattern-C
# font-picker design). The `<select>` is form-value-tracking only;
# the user sees `font-picker.js`'s custom trigger + popover, not
# this `<select>`'s chrome, so chip-pill on it would be invisible
# work. Adding this site to the chip-pill set IS allowed — it just
# isn't required.
_HIDDEN_FONT_FAMILY_CLASSES = frozenset({"om-select", "field-font-family"})

# Project root resolves from this file: backend/tests/X.py → repo/
_UI_SRC = Path(__file__).resolve().parent.parent.parent / "ui" / "src"


def _iter_select_tags() -> list[tuple[Path, str]]:
    """Yield `(path, class_attr_value)` for every `<select>` opening
    tag found in non-test `ui/src/*.js` files.

    Surfaces a useful error (rather than silently skipping) if a
    `<select>` lacks a `class=` attribute, since every shipped
    site today has one and a future class-less `<select>` is
    likely an oversight worth flagging.
    """
    assert _UI_SRC.is_dir(), f"ui/src not found at {_UI_SRC}"
    out: list[tuple[Path, str]] = []
    for js in sorted(_UI_SRC.glob("*.js")):
        if js.name.endswith(".test.js"):
            continue
        # macOS AppleDouble resource forks (`._*.js`) shadow real
        # source files on rclone-mounted ~/project/. They're binary
        # so read-as-utf8 explodes; the vitest config already
        # `**/._*`-excludes them for the same reason.
        if js.name.startswith("._"):
            continue
        text = js.read_text(encoding="utf-8")
        # Strip JS comments before scanning — narrative mentions of
        # "<select>" in `//` line comments and `/* */` block comments
        # would otherwise count as markup matches. Naive (doesn't
        # account for `//` inside string literals or regex literals),
        # but real `<select class=...>` markup lines don't contain
        # `//` before the tag, so the false-negative risk is zero in
        # practice for THIS test's purpose.
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        text = re.sub(r"//[^\n]*", "", text)
        for match in _SELECT_OPEN.finditer(text):
            attrs = match.group(1)
            class_match = _CLASS_ATTR.search(attrs)
            class_value = class_match.group(1) if class_match else ""
            out.append((js, class_value))
    return out


def test_every_ui_select_wears_chip_pill_or_documented_hidden():
    """Every `<select>` in `ui/src/*.js` must wear `.om-pulldown`
    (the canonical chip-pill class from the 2026-05-19 chrome
    unification — see `ui/styles.css:622+`) OR be the documented
    hidden font-family pattern at `editor.js field-font-family`
    (driven by the Pattern-C font-picker; chrome is invisible).

    Catches:
    - A new `<select>` added without the chip-pill class — the
      operator-visible chrome will fall back to browser default,
      breaking the UI's pulldown-style consistency.
    - The hidden font-family pattern silently spreading to a
      visible site — the exception is per-site documented, not a
      blanket "om-select is OK" pass.

    QA audit cross-ref: D2 / 2026-05-23. Recon (stale-as-of-2026-05-19):
    `qa/captures/ui-popup-consistency-recon-2026-05-17.md`.
    """
    selects = _iter_select_tags()
    assert selects, (
        f"no <select> elements found under {_UI_SRC} — did the regex break? "
        f"Or did the UI move? Update the test."
    )

    violations: list[tuple[Path, str]] = []
    for path, class_value in selects:
        classes = set(class_value.split())
        if "om-pulldown" in classes:
            continue
        if classes == _HIDDEN_FONT_FAMILY_CLASSES:
            # The documented hidden Pattern-C-driven select. Exact
            # match — adding extra classes here would be suspicious
            # and should re-trigger this assertion path.
            continue
        violations.append((path.relative_to(_UI_SRC.parent.parent), class_value))

    assert not violations, (
        f"{len(violations)} <select> element(s) lack the canonical chip-pill "
        f"class `.om-pulldown` and are not the documented hidden "
        f"font-family pattern. Sites: {violations}. "
        f"\n"
        f"The chip-pill chrome was unified on 2026-05-19 ('Bug 4' — see the "
        f"comment block above the `.om-pulldown` definition in ui/styles.css). "
        f"Every visible `<select>` in ui/src/ must wear `.om-pulldown` so "
        f"the operator sees consistent pulldown chrome across the UI. "
        f"Either add `om-pulldown` to the offending class list, or document "
        f"a new hidden-pattern exception in this test."
    )


def test_documented_hidden_font_family_select_still_exists():
    """Pin the assumption the test above relies on: the hidden
    Pattern-C-driven `field-font-family` <select> at editor.js is
    the ONLY exception path. If it disappears (e.g. the font-picker
    is refactored to remove the hidden select), the exception
    branch in the test above is dead code and should be deleted
    along with this assertion.

    Conversely, if it spreads to a second site, that's a smell —
    the exception is per-site documented, not a blanket allow.
    """
    selects = _iter_select_tags()
    hidden_matches = [
        (path, class_value)
        for path, class_value in selects
        if set(class_value.split()) == _HIDDEN_FONT_FAMILY_CLASSES
    ]
    assert len(hidden_matches) == 1, (
        f"expected exactly 1 hidden-font-family <select> (the "
        f"Pattern-C-driven `field-font-family` at editor.js); "
        f"found {len(hidden_matches)}: {hidden_matches}. If the "
        f"font-picker was refactored, update both this test and "
        f"the exception in test_every_ui_select_wears_chip_pill_or_"
        f"documented_hidden."
    )
