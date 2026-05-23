"""Inline-preview font-load orchestration lock (2026-05-24).

qarl-direct bug 2026-05-24: tile thumbnails in the playlist panel's
inline preview render before custom @font-face fonts load, so they
paint with fallback fonts. Cache key (`inline-preview.js:910-915`)
keys on the family-name string + doesn't invalidate when fonts
finish loading → stale thumbnails persist for the session.

Fix lands two coordinated mechanisms:

A. Per-render font check + kick in `drawTextOverVideo` — for each
   text-layer's font, `document.fonts.check(...)`; on miss,
   `document.fonts.load(...).then(...)` clears `textOverlayKey` +
   calls `renderOnce()` so the next frame re-rasterizes with the
   real font.

B. One-shot listener on `document.fonts.ready` at mount setup —
   covers the common app-just-opened case where every bundled font
   is loading in parallel with the first render.

Both clear `textOverlayKey` (not `imageCache` — the image cache
holds server-rendered PNGs whose pixel content is independent of
client font-load state). Both guard on the `stopped` flag so a
late-resolving .then() can't touch torn-down state.

A future "helpful" refactor could break either invariant silently:
- Removing the per-render check (A) would let mid-session font
  additions / lazy-load patterns fall back to stale-cache forever.
- Removing the fonts.ready listener (B) would re-introduce the
  original stale-on-open bug for every page load.
- Dropping the `stopped` guard would leak a renderOnce() into a
  torn-down panel state.

Static parse — same shape as D2 / M5 / H4 / Slice 4 Test A closures.
"""

from __future__ import annotations

import re
from pathlib import Path

_INLINE_PREVIEW = (
    Path(__file__).resolve().parent.parent.parent / "ui" / "src" / "inline-preview.js"
)


def _read_inline_preview_source() -> str:
    """Read `inline-preview.js` and strip JS comments so narrative
    mentions of `document.fonts.*` in `//` line comments and `/* */`
    block comments don't false-pass the assertions. Naive comment
    strip; real code doesn't put `//` before a fonts call on the
    same line."""
    assert _INLINE_PREVIEW.is_file(), (
        f"inline-preview.js not found at {_INLINE_PREVIEW}; relocation? "
        f"Update the test path."
    )
    text = _INLINE_PREVIEW.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)
    return text


def test_inline_preview_imports_font_picker_helpers():
    """Plan A relies on `cssFontFamily` + `FONT_WEIGHT_BY_VALUE`
    from font-picker.js to build the font-load token in the same
    shape editor.js uses (so the @font-face family name matches the
    one registered at module load). A future refactor that drops
    one of these imports (or inlines a different family-name shape)
    would build a token document.fonts can't match → check() always
    returns false → load() kick fires every frame forever."""
    source = _read_inline_preview_source()
    assert "from \"./font-picker.js\"" in source, (
        "inline-preview.js must import from font-picker.js for the "
        "font-load orchestration to use the correct family-name shape."
    )
    for helper in ("cssFontFamily", "FONT_WEIGHT_BY_VALUE"):
        assert helper in source, (
            f"{helper!r} import missing — the font-load token won't "
            f"match the @font-face registration shape and document.fonts "
            f"calls won't resolve correctly."
        )


def test_inline_preview_fonts_ready_listener_wired():
    """Plan B: `document.fonts.ready.then(...)` listener at mount
    setup must clear `textOverlayKey` AND call `renderOnce()`. The
    listener catches the common case where the operator opens the
    app + the first render's cache is fallback-fonts; clearing the
    key + re-rendering with real fonts post-load resolves it without
    operator action.

    `stopped` guard is REQUIRED — the .then() can resolve after the
    panel's stop() runs, and touching torn-down state would leak
    a render call into a dead panel.
    """
    source = _read_inline_preview_source()
    # Look for the document.fonts.ready listener block + check its
    # body sets textOverlayKey to null + calls renderOnce + guards
    # on stopped.
    ready_match = re.search(
        r"document\.fonts\?\.ready[\s\S]{0,500}?\}\s*\)",
        source,
    )
    assert ready_match, (
        "document.fonts.ready listener not found in inline-preview.js. "
        "Plan B (fonts-ready cache invalidation) wire missing — re-introduces "
        "the original stale-on-open bug for every page load."
    )
    block = ready_match.group(0)
    assert "textOverlayKey = null" in block, (
        "document.fonts.ready listener doesn't clear textOverlayKey — "
        "cache lock never lifts; fonts loading after the first render "
        "won't invalidate stale rasters."
    )
    assert "renderOnce()" in block, (
        "document.fonts.ready listener doesn't call renderOnce — cache "
        "clears but the next frame doesn't fire until something else "
        "triggers it; operator sees stale render until they scrub."
    )
    assert "stopped" in block, (
        "document.fonts.ready listener doesn't guard on `stopped` — a "
        ".then() resolution after panel teardown leaks state mutation "
        "into a dead panel."
    )


def test_inline_preview_per_render_font_check_wired():
    """Plan A: in the text-overlay branch, every per-render frame
    walks `item.text_layers` and `document.fonts.check(...)`s each
    family's load state; on miss, `document.fonts.load(...).then(...)`
    clears textOverlayKey + calls renderOnce. Catches the case
    where a font finishes loading mid-session (e.g. operator adds a
    new family the bundled set doesn't include) and the fonts.ready
    listener (B) has already fired.
    """
    source = _read_inline_preview_source()
    # The per-render branch is a `document.fonts.check` call inside
    # a for-of layer loop. Find the load().then() shape next to it.
    assert "document.fonts.check" in source, (
        "document.fonts.check not found in inline-preview.js — Plan A "
        "(per-render font check) missing; late-loading fonts will not "
        "trigger a re-render."
    )
    # The .then() should clear textOverlayKey + call renderOnce.
    # Match an arrow-function .then() body anywhere that follows
    # a .load(...) call. Body uses `[^}]*` since the per-render
    # branch's .then() body contains no nested braces (just
    # statements + the early-return guard).
    load_then_match = re.search(
        r"\.load\([^)]+\)\s*\.then\(\(\)\s*=>\s*\{([^}]*)\}\s*\)",
        source,
    )
    assert load_then_match, (
        "document.fonts.load(...).then(...) handler not found in "
        "inline-preview.js — Plan A's reactive cache invalidation "
        "is missing or in a shape the test can't recognize."
    )
    block = load_then_match.group(1)
    assert "textOverlayKey = null" in block, (
        "document.fonts.load(...).then() doesn't clear textOverlayKey — "
        "font finishes loading but cache lock isn't lifted."
    )
    assert "renderOnce()" in block, (
        "document.fonts.load(...).then() doesn't call renderOnce — "
        "cache clears but no re-render is triggered."
    )
    assert "stopped" in block, (
        "document.fonts.load(...).then() doesn't guard on `stopped` — "
        "late-resolving font load leaks renderOnce() into a torn-down "
        "panel."
    )
