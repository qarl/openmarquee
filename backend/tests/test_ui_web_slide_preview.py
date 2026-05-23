"""Web-slide editor preview regression lock (Bug #5, 2026-05-23).

qarl-direct bug 2026-05-23: the web-slide editor pane rendered a
text-only placeholder card forever, even though the slide-browser
tile thumbnail showed the same asset.png correctly. The fix adds
an inline `<img class="web-preview-screenshot">` that loads
`/api/content/{id}/asset` (the same endpoint the tile thumbs +
inline-preview already use), with a `?v=` cache-bust matching the
slide-browser pattern and an `onerror` fallback to the placeholder
card for transient 404 / network issues.

Static parse — same shape as D2 / M5 / H4 / Slice 4 Test A / inline-
preview font-load closures. A future "helpful" refactor could quietly
break any of these load-bearing wires; the assertions fence them:

1. The `mediaSrc` import — drops auth-token query param, every
   subsequent request 401s + the img falls back to placeholder
   forever.
2. The `<img class="web-preview-screenshot">` element — without it
   there's no preview surface to populate.
3. The `/api/content/${state.editingId}/asset` URL — a refactor that
   builds a different URL shape would silently bypass the working
   endpoint.
4. The `?v=` cache-bust — without it, an updated screenshot post-
   peer-flock-sync gets masked by a stale HTTP cache.
5. The `error` listener on the screenshot element — without it, a
   transient 404 / blocked-by-cors / network blip leaves a broken-
   image icon in the editor instead of the URL-only placeholder.
"""

from __future__ import annotations

import re
from pathlib import Path

_WEB_SLIDE = Path(__file__).resolve().parent.parent.parent / "ui" / "src" / "web-slide.js"


def _read_web_slide_source() -> str:
    """Read `web-slide.js` and strip JS comments so narrative mentions
    of the locked symbols in `//` line comments and `/* */` block
    comments don't false-pass the assertions."""
    assert _WEB_SLIDE.is_file(), (
        f"web-slide.js not found at {_WEB_SLIDE}; relocation? Update the test path."
    )
    text = _WEB_SLIDE.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)
    return text


def test_web_slide_imports_media_src() -> None:
    """The screenshot img URL must be wrapped in `mediaSrc(...)` so
    the auth-token query param is appended. A refactor that drops
    this import (or builds the URL by string-concat without the
    helper) would 401 every request and the operator would see the
    placeholder fallback forever."""
    source = _read_web_slide_source()
    assert 'from "./api.js"' in source, (
        "web-slide.js must import from ./api.js to access mediaSrc — "
        "the asset URL needs the auth-token query param helper or "
        "every screenshot fetch 401s."
    )
    assert "mediaSrc" in source, (
        "mediaSrc import missing — asset URLs will lack the auth token and 401 in production."
    )


def test_web_slide_template_has_screenshot_img() -> None:
    """The preview pane must have an `<img class="web-preview-screenshot">`
    element with `hidden` (initial state — shown by refreshScreenshot
    once an id is set) and an `alt` attribute. Without this element
    there's no surface to populate; the editor reverts to placeholder-
    forever."""
    source = _read_web_slide_source()
    assert 'class="web-preview-screenshot"' in source, (
        '<img class="web-preview-screenshot"> element missing from '
        "the template — the editor preview has no surface to render "
        "the saved slide's asset.png on."
    )
    # Match the full img tag (multi-line attributes allowed) so we
    # can assert hidden + alt within it.
    img_match = re.search(
        r"<img[^>]*class=\"web-preview-screenshot\"[^>]*>",
        source,
        flags=re.DOTALL,
    )
    assert img_match, (
        'could not find the <img class="web-preview-screenshot"> tag '
        "in a parseable shape — refactor may have split the attribute "
        "across an awkward boundary."
    )
    img_tag = img_match.group(0)
    assert "hidden" in img_tag, (
        '<img class="web-preview-screenshot"> must start `hidden` — '
        "the unsaved-draft path relies on the placeholder showing first."
    )
    assert "alt=" in img_tag, (
        '<img class="web-preview-screenshot"> missing alt attribute — accessibility regression.'
    )


def test_web_slide_screenshot_uses_content_asset_endpoint() -> None:
    """The screenshot src must point at `/api/content/${id}/asset` —
    the same endpoint slide-browser tile thumbnails + inline-preview
    use. A refactor that builds a different URL shape (e.g. a
    `/api/web-slides/${id}/screenshot` invention) would silently bypass
    the working backend endpoint."""
    source = _read_web_slide_source()
    assert "/api/content/" in source and "/asset" in source, (
        "the /api/content/{id}/asset URL shape is missing — the "
        "preview won't fetch the same screenshot the tile thumb does."
    )
    # Confirm the id is interpolated from state.editingId, not a
    # different variable that might silently desync.
    assert re.search(
        r"/api/content/\$\{state\.editingId\}/asset",
        source,
    ), (
        "the asset URL doesn't interpolate state.editingId — a refactor "
        "that renamed the field would silently break this surface."
    )


def test_web_slide_screenshot_has_cache_bust() -> None:
    """The asset URL must include a `?v=` cache-bust matching the
    slide-browser tile pattern. Without it, an updated screenshot
    (e.g. after a peer-flock sync or a manual Pi re-render) gets
    masked by a stale HTTP cache entry."""
    source = _read_web_slide_source()
    # Must contain `?v=` adjacent to a template literal that
    # encodes the asset version state.
    assert re.search(
        r"/api/content/\$\{state\.editingId\}/asset\?v=",
        source,
    ), (
        "the asset URL is missing the `?v=` cache-bust query param — "
        "an updated screenshot will be masked by an HTTP-cached old "
        "copy until the operator hard-refreshes."
    )


def test_web_slide_screenshot_has_error_fallback() -> None:
    """An `error` listener must be wired on the screenshot element so
    a transient 404 / blocked-by-cors / network blip falls back to
    the URL-only placeholder card — the operator sees a sensible UI
    rather than a broken-image icon. The handler must re-show the
    placeholder card (`previewCardEl.hidden = false`) AND hide the
    img (`screenshotEl.hidden = true`)."""
    source = _read_web_slide_source()
    # Match the addEventListener("error", ...) call's body to verify
    # both fallback assignments are inside.
    handler_match = re.search(
        r"screenshotEl\.addEventListener\(\s*\"error\"\s*,\s*\(\)\s*=>\s*\{([^}]*)\}",
        source,
    )
    assert handler_match, (
        'screenshotEl.addEventListener("error", ...) handler not '
        "found — a transient asset fetch failure will leave a broken-"
        "image icon in the editor instead of the placeholder."
    )
    body = handler_match.group(1)
    assert "screenshotEl.hidden = true" in body, (
        "error handler doesn't hide the screenshot img — broken-image "
        "icon stays visible alongside the placeholder."
    )
    assert "previewCardEl.hidden = false" in body, (
        "error handler doesn't re-show the placeholder card — operator "
        "sees a blank pane on transient fetch failure."
    )
