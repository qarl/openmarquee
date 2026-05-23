"""H4 closure (2026-05-23): parity-harness transition-coverage lock.

The parity harness at `ui/parity-harness.html` renders reference
fixtures in-browser so QA's golden-bless gate (Phase E) can compare
its output to the Rust renderer's. Each transition the harness can
render lifts a `BROWSER-SKIP` from a corresponding parity fixture
in `scripts/parity/fixtures.json`.

This test asserts the harness's `TRANSITION_FNS` table covers every
fixture-set transition (13 today). Catches:

- A new transition kind added to `scripts/parity/fixtures.json`
  without a corresponding `TRANSITION_FNS` entry — would silently
  re-introduce a BROWSER-SKIP and shrink parity coverage.
- A `TRANSITION_FNS` entry deleted without removing its fixture —
  would fail loudly at parity-harness load time, but the static
  test surfaces it at commit time.

The 3 Rust transitions that have shaders but NO parity fixture
(dissolve, halftone, blinds) are intentionally absent from
`TRANSITION_FNS`. Translating them would be unused code. If a
parity fixture is added later for any of them, this test fails
loudly + prompts the JS translation.

Static file-parse — no runtime dependencies, no vitest infra
needed (per the vitest-virtiofs-wedge documented in the Slice 3
commit). Same shape as `test_ui_chip_pill_consistency.py` (D2),
`test_ui_live_cancel_takeover.py` (M5), and
`test_systemd_unit_whitelists_af_netlink` (Slice 4 Test A).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Project root resolves from this file: backend/tests/X.py → repo/
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PARITY_HARNESS = _REPO_ROOT / "ui" / "parity-harness.html"
_PARITY_FIXTURES = _REPO_ROOT / "scripts" / "parity" / "fixtures.json"

# Rust transitions that EXIST as shaders (per
# `fs_for_transition_kind` in renderer/src/hdmi_logic.rs) but have
# NO parity fixture, so translating them to JS is unused code. If
# a fixture is added later for any of these, this set should
# shrink in lockstep — assertion below catches the drift either way.
_DOCUMENTED_OUTLIERS = frozenset({"dissolve", "halftone", "blinds"})

# JS object-key extractor for entries of the form
# `<key>(pixels, w, h, A, B, t) {` inside TRANSITION_FNS. The
# arguments are fixed across all entries; the key is the only
# variable token. Doesn't match method calls or other braces because
# the (pixels, w, h, A, B, t) signature is unique to TRANSITION_FNS.
_TRANSITION_FN_KEY = re.compile(r"\b(\w+)\s*\(pixels,\s*w,\s*h,\s*A,\s*B,\s*t\)\s*\{")


def _harness_transition_keys() -> set[str]:
    """Return the set of `TRANSITION_FNS` keys defined in
    `parity-harness.html`. Comments stripped first so a narrative
    mention of a method signature in a `//` line comment doesn't
    falsely register as a registered transition."""
    assert _PARITY_HARNESS.is_file(), (
        f"parity harness not found at {_PARITY_HARNESS}; relocation? Update the test path."
    )
    text = _PARITY_HARNESS.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)
    return {m.group(1) for m in _TRANSITION_FN_KEY.finditer(text)}


def _parity_fixture_transitions() -> set[str]:
    """Return the set of distinct `"transition"` values referenced
    in `scripts/parity/fixtures.json`. Excludes `single` /
    `transition_mid` `kind` markers — those are fixture types, not
    transitions."""
    assert _PARITY_FIXTURES.is_file(), (
        f"parity fixtures not found at {_PARITY_FIXTURES}; relocation? Update the test path."
    )
    data = json.loads(_PARITY_FIXTURES.read_text(encoding="utf-8"))
    transitions: set[str] = set()

    # The fixtures.json shape is a list (or object containing a list)
    # of fixture dicts with a "transition" key on transition_mid
    # fixtures. Recursively walk and collect.
    def _walk(node):
        if isinstance(node, dict):
            value = node.get("transition")
            if isinstance(value, str):
                transitions.add(value)
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(data)
    return transitions


def test_parity_harness_covers_every_fixture_transition():
    """`TRANSITION_FNS` in `parity-harness.html` must define a
    function for every `"transition"` value in
    `scripts/parity/fixtures.json`. A fixture referencing a
    transition the harness can't render lifts a BROWSER-SKIP and
    shrinks parity coverage — caught here at commit time.

    QA audit cross-ref: H4 / 2026-05-23.
    """
    harness_keys = _harness_transition_keys()
    assert harness_keys, (
        "TRANSITION_FNS appears empty — did the regex break? Or did the harness move?"
    )
    fixture_transitions = _parity_fixture_transitions()
    assert fixture_transitions, (
        "no `transition` values found in parity fixtures.json — fixture shape changed?"
    )
    missing = fixture_transitions - harness_keys
    assert not missing, (
        f"parity fixtures reference {len(missing)} transition(s) the "
        f"harness can't render: {sorted(missing)}. Add the missing "
        f"function(s) to TRANSITION_FNS in ui/parity-harness.html "
        f"(translate from the matching FS_<KIND> shader in "
        f"renderer/src/hdmi_logic.rs — see the H4 commit for the "
        f"established translation pattern), or drop the orphan "
        f"fixture(s) if the parity gate doesn't need them."
    )


def test_documented_outliers_stay_absent_from_harness():
    """The 3 Rust-side transitions without parity fixtures
    (dissolve, halftone, blinds) are intentionally NOT in
    TRANSITION_FNS. Translating them would be unused code. If
    fixtures get added later for any of them,
    `test_parity_harness_covers_every_fixture_transition` will
    fail first + prompt the addition; this test catches the
    opposite drift (someone adds a JS impl speculatively without
    a backing fixture).
    """
    harness_keys = _harness_transition_keys()
    fixture_transitions = _parity_fixture_transitions()
    speculative = (harness_keys & _DOCUMENTED_OUTLIERS) - fixture_transitions
    assert not speculative, (
        f"TRANSITION_FNS includes {sorted(speculative)} which is/are "
        f"in the documented-outlier set (no parity fixture exists). "
        f"Either add a fixture in scripts/parity/fixtures.json + "
        f"remove the entry from _DOCUMENTED_OUTLIERS in this test, "
        f"or drop the speculative JS implementation."
    )


def test_harness_transition_count_matches_expected():
    """Pin the current count (13 = 6 existing + 7 from H4). A
    silent half-deletion of TRANSITION_FNS entries (e.g. someone
    "tidying" the file) would shrink the count without
    necessarily failing the fixture-coverage test if the matching
    fixture was also dropped. This assertion catches a count
    change so a reviewer has to update both this test + verify the
    fixture set if the count legitimately changes.
    """
    keys = _harness_transition_keys()
    # 6 pre-H4: cut, fade, wipe, slide, scroll, pixelate.
    # 7 from H4: iris, scanline, glitch, push, flip, marquee, shutter.
    expected = {
        "cut",
        "fade",
        "wipe",
        "slide",
        "scroll",
        "pixelate",
        "iris",
        "scanline",
        "glitch",
        "push",
        "flip",
        "marquee",
        "shutter",
    }
    assert keys == expected, (
        f"TRANSITION_FNS keys drift detected. Expected {sorted(expected)}, "
        f"got {sorted(keys)}. Extra: {sorted(keys - expected)}. "
        f"Missing: {sorted(expected - keys)}. If this drift is "
        f"intentional, update this test's `expected` set AND verify "
        f"the parity fixture set is in sync."
    )
