"""M5 closure (2026-05-23): cancelTakeover no-backend-call contract lock.

QA audit item M5 / `ui/src/live-panel.js:1076+` (the former
`TODO(qarl-confirm)`). Cancel from a mount-time take-over-prompt
drops to plain idle (no backend call). The contract was decided
this commit — see the decision-record comment at
`ui/src/live-panel.js`'s `cancelTakeover()` body.

A future "helpful" refactor could break the no-backend-call contract
silently — e.g. adding `apiFetch("/api/live/stop", …)` to "cleanly
close" a non-existent session. That call would 404 (no session
exists at mount-time-prompt cancel) + spam the operator's network
tab + log + (worst) potentially close a sibling tab's active
session if the operator just opened a second Live panel. The bug
class is the same "well-intentioned refactor regresses a silent
contract" shape as the Slice 3 + 4 + D2 regression locks.

Static file-parse against `ui/src/live-panel.js` — no runtime
dependencies, no vitest infra needed (per the vitest-virtiofs-wedge
documented in the Slice 3 commit). Same shape as
`test_ui_chip_pill_consistency.py` (D2) +
`test_systemd_unit_whitelists_af_netlink` (Slice 4 Test A).

The complementary behavioral coverage — "cancelTakeover stays idle
even when mountInit resolves AFTER cancel" — already exists at
`ui/src/live-panel.test.js:565+` (the Bug 6 race-fix test) and is
out of scope here. Static-parse complements behavioral; doesn't
replace it.
"""

from __future__ import annotations

import re
from pathlib import Path

# Project root resolves from this file: backend/tests/X.py → repo/
_LIVE_PANEL_JS = (
    Path(__file__).resolve().parent.parent.parent / "ui" / "src" / "live-panel.js"
)

# Names that, if found inside `cancelTakeover`'s body, would
# indicate a backend call.
#
# `cancelTakeover` is defined inside `mountLivePanel()`, which
# destructures its options at `ui/src/live-panel.js:287-293` to
# rebind the api.js imports under `api*`-prefixed local names. The
# imports (`startLive`, `stopLive`, etc.) are NEVER called by their
# bare names inside mountLivePanel — every existing call site uses
# the `api*` alias. So this list pins the IN-SCOPE names that a
# refactorer would actually type, not the import names.
#
# `apiFetch` is kept as a forward-guard for a refactor that adds a
# direct apiFetch import (bypassing the mountLivePanel-options
# pattern). `apiGetStatus` IS in-scope and is included even though
# it's a GET (not a state-changing POST) — even a "let's double-
# check status after cancel" addition is a backend round trip this
# contract bans.
_BACKEND_CALL_NAMES = (
    "apiGetStatus",
    "apiStartLive",
    "apiStartRtspLive",
    "apiTakeoverLive",
    "apiTakeoverRtspLive",
    "apiStopLive",
    "apiFetch",
)


def _read_live_panel_source() -> str:
    """Read `ui/src/live-panel.js` and strip JS comments so the
    cancelTakeover body scan doesn't false-match narrative mentions
    of `apiFetch`/`stopLive`/etc in `//` line comments or `/* */`
    block comments. Naive comment-strip is sufficient — real code
    that calls these functions doesn't put `//` before the call on
    the same line."""
    assert _LIVE_PANEL_JS.is_file(), (
        f"live-panel.js not found at {_LIVE_PANEL_JS} — relocation? Update "
        f"the test path."
    )
    text = _LIVE_PANEL_JS.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)
    return text


def _extract_cancel_takeover_body(source: str) -> str:
    """Return the body of `function cancelTakeover() { ... }` from
    `live-panel.js` (comments already stripped). Pinned to the
    function NAME so a line-number drift doesn't break the test.

    Brace-balance walk rather than regex — the function body
    contains nested blocks (try/catch, if branches) that a naive
    `{.*?}` regex would mis-bound.
    """
    marker = "function cancelTakeover()"
    fn_start = source.find(marker)
    assert fn_start != -1, (
        f"`{marker}` not found in live-panel.js — was the function "
        f"renamed? Update this test to match."
    )
    # Find the opening brace AFTER the signature.
    brace_open = source.find("{", fn_start + len(marker))
    assert brace_open != -1, (
        f"`{marker}` has no opening brace? source structure changed."
    )
    # Brace-balance walk to the matching close.
    depth = 0
    for idx in range(brace_open, len(source)):
        ch = source[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                # Body is the slice between the opening `{` and the
                # closing `}` (exclusive of both).
                return source[brace_open + 1 : idx]
    raise AssertionError(
        f"unbalanced braces walking `{marker}` body — source likely truncated."
    )


def test_cancel_takeover_function_exists():
    """Pin the assumption every other test in this file relies on:
    `function cancelTakeover()` exists in live-panel.js. If the
    function gets renamed or restructured, this test fails first
    + cleanly + the other assertions are independently re-readable
    after the rename."""
    source = _read_live_panel_source()
    assert "function cancelTakeover()" in source, (
        "`function cancelTakeover()` not found in live-panel.js. Was "
        "it renamed (e.g. to `handleCancel` or moved into a class)? "
        "Update this test + the M5 decision-record comment block."
    )


def test_cancel_takeover_sets_phase_to_idle():
    """Cancel = "operator chose I want out" → panel returns to the
    idle phase (no viewfinder reopen). The full decision-record is
    in the comment block at `live-panel.js`'s cancelTakeover() body.
    """
    body = _extract_cancel_takeover_body(_read_live_panel_source())
    assert 'state.phase = "idle"' in body, (
        '`cancelTakeover()` must set state.phase = "idle" (the Path-A '
        "decision per M5 closure). Found body:\n"
        f"{body!r}"
    )


def test_cancel_takeover_latches_mountinit_cancelled():
    """The Bug 6 (2026-05-17) race-fix: `mountInitCancelled = true`
    must latch BEFORE the phase flip, so a still-pending
    `mountInit()` resuming AFTER cancel doesn't silently revive
    the panel. Locked behaviorally at `live-panel.test.js:565+`;
    this static-parse assertion provides a second axis of coverage
    that catches a refactor that drops the latch without touching
    the behavioral test (e.g. someone reordering the lines + the
    test's `await` happens to still resolve idle).
    """
    body = _extract_cancel_takeover_body(_read_live_panel_source())
    assert "mountInitCancelled = true" in body, (
        "`cancelTakeover()` must latch `mountInitCancelled = true` "
        "(Bug 6 race fix, 2026-05-17). Without it, a deferred mountInit "
        "resumption can silently revive the panel after Cancel — see "
        "the in-file comment + live-panel.test.js:565+. Found body:\n"
        f"{body!r}"
    )


def test_cancel_takeover_makes_no_backend_call():
    """The contract this test is the load-bearing lock for: Cancel
    from a mount-time take-over-prompt MUST NOT POST to any
    /api/live/* endpoint. There IS no session to stop at this point
    (the take-over-prompt fires when ANOTHER phone owns the screen);
    calling /api/live/stop would 404 + spam the network panel + log,
    and calling /api/live/takeover/start would be wrong-action.

    A future "helpful" refactor that adds e.g. `apiFetch("/api/live/
    stop", ...)` here is the explicit anti-pattern this test catches.
    """
    body = _extract_cancel_takeover_body(_read_live_panel_source())
    leaked: list[str] = []
    for name in _BACKEND_CALL_NAMES:
        # Look for invocation (`name(`) — bare name references in
        # variable assignments or imports are fine; only call-shape
        # signals an actual backend round trip.
        if re.search(rf"\b{re.escape(name)}\s*\(", body):
            leaked.append(name)
    assert not leaked, (
        f"`cancelTakeover()` invokes backend call(s): {leaked}. "
        "M5 closure (2026-05-23) decision: Cancel from a mount-time "
        "take-over-prompt MUST NOT call /api/live/* (no session "
        "exists; the call would 404). If a legitimate backend round "
        "trip is needed here for a NEW reason, update the decision-"
        "record comment block in live-panel.js AND this test in lock-"
        "step. Found body:\n"
        f"{body!r}"
    )
