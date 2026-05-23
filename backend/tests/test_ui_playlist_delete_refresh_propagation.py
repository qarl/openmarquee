"""Playlist add/delete/rename refresh-propagation lock (2026-05-24).

qarl-direct bug 2026-05-24: deleting a playlist from the playlist-
browser X button returned 204 + the tile stayed visible in the DOM
+ the schedule.js dropdown still showed the deleted playlist (and
clicking the X a second time returned 404 + "Could not delete"
because the backend was already gone).

Two coordinated mechanisms close the gap:

A. main.js captures the `scheduleHandle` returned from
   `mountSchedule(...)` and calls `await scheduleHandle?.refresh()`
   in three places where playlists change:
   - `deletePlaylist()` — propagate the delete to the schedule UI's
     cached `availableChoices` list.
   - `createNewPlaylist()` — propagate the add.
   - `onSavePlaylist` callback (PUT path, including renames) —
     propagate display-name changes so the dropdown labels track
     the new name.

   Before this fix, the schedule UI cached `availableChoices` at
   mount + only refreshed it from its own Add-rule button click,
   so external playlist changes never propagated.

B. `listPlaylists()` in api.js opts out of the browser HTTP cache
   with `cache: "no-store"`. Defensive against intermediate caching
   layers (service worker, proxy) that could serve a stale
   post-delete response and re-populate the rebuilt tile list with
   the just-deleted entry. Playlist GETs are infrequent enough that
   no-store is cheap.

A future "helpful" refactor could break either invariant silently:
- Removing the `scheduleHandle = mountSchedule(` capture (or
  dropping the `let scheduleHandle = null;` declaration) leaves the
  refresh calls running as no-ops against `null?.refresh()` —
  reintroduces the stale-dropdown bug.
- Dropping any of the three `scheduleHandle?.refresh()` call sites
  re-introduces the stale-dropdown bug for that specific code path.
- Dropping `cache: "no-store"` on `listPlaylists` re-opens the
  HTTP-cache hypothesis window for the tile-stays-in-DOM symptom.

Static parse — same shape as the D2 / M5 / H4 / font-load (Plan A+B)
closures (vitest infra still wedged on the virtiofs cargo / npm path).
"""

from __future__ import annotations

import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_MAIN_JS = _PROJECT_ROOT / "ui" / "src" / "main.js"
_API_JS = _PROJECT_ROOT / "ui" / "src" / "api.js"


def _read_js_source_stripped(path: Path) -> str:
    """Read a JS file and strip `//` line comments + `/* */` block
    comments so narrative mentions inside comments don't false-pass
    the assertions.

    Order matters: `//` first, then `/* */`. main.js has a `//` line
    comment containing the literal `/api/*` (path with wildcard) —
    if `/* */` runs first with DOTALL, it sees that as an open-block
    marker and silently eats everything up to the next `*/` (which
    might be 7KB away in a different scope). Stripping `//` first
    neutralizes those false openers."""
    assert path.is_file(), f"JS source not found at {path}; relocation? Update the test path."
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return text


def _extract_function_body(source: str, function_signature: str) -> str:
    """Find a function definition matching `function_signature` and
    return its body (the contents between the opening `{` and the
    matching closing `}`). The signature must appear verbatim in
    `source`. Uses brace-counting to find the matching close.
    """
    idx = source.find(function_signature)
    assert idx >= 0, (
        f"Function signature {function_signature!r} not found in source. "
        f"Refactored away? Update the test."
    )
    # Find the opening brace after the signature.
    brace_start = source.find("{", idx)
    assert brace_start >= 0, f"No opening brace after {function_signature!r}."
    depth = 0
    i = brace_start
    while i < len(source):
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[brace_start + 1 : i]
        i += 1
    raise AssertionError(f"Could not find matching close brace for {function_signature!r}.")


def test_main_declares_and_captures_schedule_handle():
    """main.js must declare `scheduleHandle` (initially null) AND
    assign it from the `mountSchedule(...)` return value. Without
    both halves, the three `scheduleHandle?.refresh()` call sites
    silently no-op against `null?.refresh()` — the bug class this
    test guards against.
    """
    source = _read_js_source_stripped(_MAIN_JS)
    assert re.search(r"let\s+scheduleHandle\s*=\s*null", source), (
        "`let scheduleHandle = null` declaration missing in main.js. "
        "Without it the assignment below either ReferenceErrors or "
        "leaks to global scope; either way the refresh() calls won't "
        "fire."
    )
    assert re.search(r"scheduleHandle\s*=\s*mountSchedule\(", source), (
        "`scheduleHandle = mountSchedule(` capture missing in main.js. "
        "The returned {refresh, flushAutoSave} handle is being discarded, "
        "so playlist add/delete/rename can't trigger a schedule-UI "
        "re-fetch + the dropdowns stay stale."
    )


def test_delete_playlist_refreshes_schedule_handle():
    """`deletePlaylist` body must contain a `scheduleHandle?.refresh()`
    call so the schedule UI's cached `availableChoices` drops the
    deleted entry. Without this the dropdown still lists the deleted
    playlist (qarl 2026-05-24).
    """
    source = _read_js_source_stripped(_MAIN_JS)
    body = _extract_function_body(
        source,
        "async function deletePlaylist(playlistId, displayName)",
    )
    assert "scheduleHandle" in body and "refresh" in body, (
        "deletePlaylist doesn't reference scheduleHandle.refresh. "
        "Schedule dropdown will retain the deleted playlist."
    )
    assert re.search(r"scheduleHandle\?\.refresh\(\s*\)", body), (
        "scheduleHandle?.refresh() call not found in deletePlaylist "
        "body. The optional-chain shape matters — a hard "
        "scheduleHandle.refresh() would crash if mountSchedule hadn't "
        "run yet (shouldn't happen post-boot, but defensive)."
    )


def test_create_new_playlist_refreshes_schedule_handle():
    """`createNewPlaylist` body must also call `scheduleHandle?.refresh()`
    — symmetric to the delete case. Schedule's own Add-rule button
    has a one-shot re-fetch (schedule.js:177-186) for THAT specific
    interaction, but it doesn't cover the case where the operator
    creates a playlist on the Playlists page first + then navigates
    to Schedule expecting to pick it.
    """
    source = _read_js_source_stripped(_MAIN_JS)
    body = _extract_function_body(
        source,
        "async function createNewPlaylist()",
    )
    assert re.search(r"scheduleHandle\?\.refresh\(\s*\)", body), (
        "scheduleHandle?.refresh() call not found in createNewPlaylist. "
        "Newly-created playlists won't show in the schedule dropdown "
        "until the operator hits Add-rule (which has its own re-fetch) "
        "or reloads the page."
    )


def test_on_save_playlist_refreshes_schedule_handle():
    """`onSavePlaylist` (the PUT path, fired on rename / track edit
    save) must call `scheduleHandle?.refresh()` so renames propagate
    to the dropdown's display labels. The id stays stable but the
    NAME shown to the operator can change; without this the dropdown
    shows the OLD name forever.
    """
    source = _read_js_source_stripped(_MAIN_JS)
    # onSavePlaylist is an inline async-arrow option inside the
    # mountPlaylistTrack call. Find the arrow's body by extracting
    # the substring between `onSavePlaylist: async ({` and the
    # matching `},`. Brace-count won't work directly because the
    # arrow's outer wrapping is `{ ... }`. Use a regex anchored on
    # the destructure + look for the call within a generous window.
    arrow_match = re.search(
        r"onSavePlaylist:\s*async\s*\(\s*\{\s*playlistId[\s\S]*?await\s+refreshSidebarCounts\(\s*\);[\s\S]*?\},",
        source,
    )
    assert arrow_match, (
        "onSavePlaylist async-arrow body shape not found in main.js. "
        "Refactored away? Update this test."
    )
    block = arrow_match.group(0)
    assert re.search(r"scheduleHandle\?\.refresh\(\s*\)", block), (
        "scheduleHandle?.refresh() call not found in onSavePlaylist. "
        "Playlist RENAMES won't propagate to the schedule dropdown's "
        "display labels — operator sees the old name there forever."
    )


def test_list_playlists_opts_out_of_http_cache():
    """`listPlaylists()` in api.js must pass `cache: "no-store"` to
    apiFetch so the browser doesn't serve a stale post-delete GET
    from its HTTP cache. Defensive against the orthogonal HTTP-cache
    hypothesis (the diagnosed root cause was schedule-side cached
    `availableChoices`, but the tile-stays-in-DOM symptom fits a
    cached GET better than an autoSave race).

    `cache: "no-store"` is stricter than `no-cache` (no-cache still
    stores but revalidates; no-store never writes to cache at all).
    For playlist GETs the difference is moot but no-store is the
    clearer intent.
    """
    source = _read_js_source_stripped(_API_JS)
    # The listPlaylists body should contain an apiFetch call that
    # passes cache: "no-store". Grab a window around the call.
    list_match = re.search(
        r"export\s+async\s+function\s+listPlaylists\(\s*\)\s*\{[^}]*\}",
        source,
    )
    assert list_match, "listPlaylists function not found in api.js. Refactored? Update this test."
    body = list_match.group(0)
    assert re.search(r'cache:\s*"no-store"', body), (
        'listPlaylists() must pass `cache: "no-store"` to apiFetch '
        "so the browser doesn't serve a stale post-delete GET from "
        "its HTTP cache. Without this, the tile-stays-in-DOM symptom "
        "(qarl bug 2026-05-24) can re-surface intermittently."
    )
