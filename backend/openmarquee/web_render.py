"""On-device web-slide renderer — turns a URL into a panel-sized PNG
entirely on the Pi, with no external render helper.

A WebSlide is "an image slide whose asset.png is auto-refreshed from a
render of an operator-supplied URL". Historically that render happened
on an operator's own machine (a headless-Chromium helper in
`web-helper/`) because the Pi can't run a browser. This module replaces
that: it rasterizes the page on-device with the WeasyPrint print
engine, which is small enough to fit the Pi's RAM headroom.

Pipeline: `WeasyPrint(url) -> PDF bytes -> pypdfium2 -> PNG bytes`.
WeasyPrint lays the page out and writes a PDF; pypdfium2 rasterizes
that PDF's first page to a bitmap and exports a PNG. A `@page` rule is
injected so the PDF page is sized to the slide's actual panel
dimensions (without it WeasyPrint defaults to A4 proportions and the
page fills only part of the panel — see `_build_page_css`).

RAM mitigation — short-lived subprocess. WeasyPrint's RSS (~55 MB,
mostly Pango/cairo/fontconfig) is too much to keep resident on the
Pi's tight memory budget. This module is therefore designed to run AS
A STANDALONE SUBPROCESS — `python -m openmarquee.web_render <url> <w>
<h> <outpath>` — so that footprint is reclaimed by the OS the moment
the process exits. C3's producer spawns it that way and reads the PNG
back off `<outpath>`. The module is still importable + the render
function still directly callable in-process (the tests do that, with
the heavy libs mocked) — the subprocess wrapper is an option, not a
requirement of the API.

argv contract (the subprocess entry point — see `main`):

    python -m openmarquee.web_render <url> <panel_w> <panel_h> <out_path>

    <url>       the page to render — must be http/https (validated)
    <panel_w>   panel width in pixels  (positive integer)
    <panel_h>   panel height in pixels (positive integer)
    <out_path>  filesystem path the PNG is written to

    exit 0  — PNG written to <out_path>
    exit 1  — render failed (a one-line reason on stderr)
    exit 2  — bad argv (wrong count / non-integer dims / rejected URL)

Deferred imports: `weasyprint` and `pypdfium2` are imported INSIDE
`render_web_png` (not at module top level), mirroring the helper's
deferred-Playwright pattern. The module imports — and the unit tests
run — on a host without either library installed.
"""

from __future__ import annotations

import logging
import sys
from io import BytesIO

from openmarquee.content import validate_web_url

log = logging.getLogger(__name__)

# Per-asset network fetch budget, in seconds. WeasyPrint fetches the
# page document AND every sub-resource (CSS, images, web fonts); each
# of those fetches goes through our `url_fetcher` wrapper and is capped
# at this value. A single slow asset therefore can't hang the whole
# render — it times out, that one resource is skipped/errors, and the
# render still completes in bounded time. C3's producer additionally
# wraps the whole subprocess in an overall timeout; this is the
# inner, per-fetch bound.
WEB_RENDER_FETCH_TIMEOUT_S = 15.0

# CSS reference pixels are defined as 1px == 1/96 inch; PDF user space
# is points, 1pt == 1/72 inch. A box that is N CSS px wide is therefore
# N * 72/96 == N * 0.75 pt wide in the PDF WeasyPrint emits. pypdfium2
# rasterizes at `points * scale` device pixels, so to land a P-pt page
# on exactly P_px device pixels we render at scale 96/72. See
# `_PDFIUM_RENDER_SCALE` and `render_web_png`.
_CSS_PX_PER_INCH = 96.0
_PDF_PT_PER_INCH = 72.0

# pypdfium2 render scale that maps a px-sized @page back to exactly
# that many device pixels. The @page is `<panel_w>px <panel_h>px`,
# which WeasyPrint emits as a `panel_w * 0.75` pt page; rendering that
# at scale 96/72 == 1.3333… yields `panel_w * 0.75 * 1.3333 == panel_w`
# device pixels — exact, no rounding drift.
_PDFIUM_RENDER_SCALE = _CSS_PX_PER_INCH / _PDF_PT_PER_INCH


class WebRenderError(Exception):
    """A web render failed — network failure, a WeasyPrint/pypdfium2
    error, or an empty/zero-size result. Raised by `render_web_png` so
    callers (and the subprocess `main`) get one clear, typed failure
    instead of a library-specific exception or an unbounded hang."""


def _build_page_css(panel_w: int, panel_h: int) -> str:
    """Build the `@page` stylesheet string that sizes the PDF page to
    the panel.

    WeasyPrint, given no `@page` size, lays the document out on its
    default A4 page — so a 1360x768 panel would get an A4-proportioned
    render that only fills part of the slide. Passing this stylesheet
    in `write_pdf(stylesheets=[...])` overrides that: the page becomes
    exactly `panel_w x panel_h` CSS px with no margin, so page 1 of the
    resulting PDF IS the panel-sized render.

    Pure function — unit-tested directly. `panel_w`/`panel_h` are the
    slide's ACTUAL panel dimensions in pixels (the caller passes
    `renderer.width` / `renderer.height`).
    """
    return f"@page {{ size: {panel_w}px {panel_h}px; margin: 0; }}"


def render_web_png(url: str, panel_w: int, panel_h: int) -> bytes:
    """Render `url` to a `panel_w x panel_h` top-down PNG and return its
    bytes.

    Pipeline: WeasyPrint lays `url` out and writes a PDF whose page is
    sized to the panel (via an injected `@page` stylesheet — see
    `_build_page_css`); pypdfium2 then rasterizes page 1 of that PDF to
    a bitmap at exactly the panel resolution and exports a PNG.

    The URL is validated (`validate_web_url`, http/https only) BEFORE
    anything is fetched — WeasyPrint can read `file://` and other
    schemes, which on operator-supplied input is a local-file-read
    vector, so a non-web URL never reaches the engine.

    `weasyprint` and `pypdfium2` are imported here, not at module
    scope, so the module imports on a host without them installed.

    Network fetches (the document + every sub-resource) are bounded by
    a per-request timeout (`WEB_RENDER_FETCH_TIMEOUT_S`) via a custom
    `url_fetcher`, so one slow asset can't hang the render.

    Args:
        url: the page to render. Must be http/https.
        panel_w, panel_h: the panel dimensions in pixels. The output
            PNG is exactly this size.

    Returns:
        PNG bytes — a standard top-down PNG. (The renderer's image-bake
        path handles any orientation concerns; this module just emits a
        normal PNG.)

    Raises:
        ValueError: `url` failed `validate_web_url` (non-http/https
            scheme, control characters, a userinfo component). Raised
            before any network access.
        WebRenderError: the render failed — a network failure, a
            WeasyPrint or pypdfium2 error, or an empty/zero-size
            result. Never hangs unboundedly.
    """
    # Validate FIRST — before the heavy imports, before any fetch. A
    # `file://` / `data://` URL must never reach WeasyPrint. A bad URL
    # raises ValueError straight out (not WebRenderError): it's a
    # caller/input error, distinct from a render failure.
    validate_web_url(url)

    if panel_w <= 0 or panel_h <= 0:
        raise ValueError(
            f"panel dimensions must be positive, got {panel_w}x{panel_h}"
        )

    # Deferred imports — keep weasyprint/pypdfium2 off the module +
    # test-suite import path (mirrors web-helper's deferred Playwright).
    import weasyprint
    import pypdfium2

    # A `url_fetcher` that wraps WeasyPrint's default with a per-request
    # timeout. WeasyPrint calls this for the document AND every
    # sub-resource; the default fetcher accepts a `timeout` kwarg but
    # WeasyPrint itself never passes one, so an asset fetch is otherwise
    # unbounded. Capping each call means a single slow asset times out
    # rather than hanging the whole render.
    def _bounded_url_fetcher(fetch_url: str):
        return weasyprint.default_url_fetcher(
            fetch_url, timeout=WEB_RENDER_FETCH_TIMEOUT_S
        )

    # --- WeasyPrint: URL -> PDF bytes -----------------------------------
    try:
        page_css = weasyprint.CSS(string=_build_page_css(panel_w, panel_h))
        pdf_bytes = weasyprint.HTML(
            url=url, url_fetcher=_bounded_url_fetcher
        ).write_pdf(stylesheets=[page_css])
    except Exception as exc:  # noqa: BLE001 — funnel every lib error
        # WeasyPrint raises a wide range of types (network errors,
        # parse errors, fontconfig failures); funnel them all into one
        # typed WebRenderError so the caller has a single thing to
        # catch. `from exc` keeps the original for the logs.
        raise WebRenderError(
            f"WeasyPrint failed to render {url}: {exc}"
        ) from exc

    if not pdf_bytes:
        raise WebRenderError(
            f"WeasyPrint produced an empty PDF for {url}"
        )

    # --- pypdfium2: PDF bytes -> panel-sized PNG bytes ------------------
    pdf = None
    try:
        pdf = pypdfium2.PdfDocument(pdf_bytes)
        if len(pdf) < 1:
            raise WebRenderError(
                f"rendered PDF for {url} has no pages"
            )
        # Page 1 IS the panel-sized render — the @page rule sized the
        # whole document to one panel-shaped page.
        page = pdf[0]
        try:
            # Render at the scale that maps the px-sized @page back to
            # exactly panel_w x panel_h device pixels — see
            # `_PDFIUM_RENDER_SCALE`. pypdfium2 renders at
            # `points * scale`; the page is `panel_w * 0.75` pt wide,
            # so `0.75 * (96/72) == 1.0` and the bitmap lands exactly.
            bitmap = page.render(scale=_PDFIUM_RENDER_SCALE)
            try:
                pil_image = bitmap.to_pil()
            finally:
                bitmap.close()
        finally:
            page.close()

        # Belt-and-braces: rounding inside pdfium can leave the bitmap
        # off by a pixel for some panel sizes. Force the exact panel
        # dimensions so the producer always saves a correctly-sized
        # asset.png. (.size is (w, h).)
        if pil_image.size != (panel_w, panel_h):
            pil_image = pil_image.resize((panel_w, panel_h))

        buf = BytesIO()
        # A standard top-down PNG — no orientation tricks here.
        pil_image.save(buf, format="PNG")
        png_bytes = buf.getvalue()
    except WebRenderError:
        raise
    except Exception as exc:  # noqa: BLE001 — funnel every lib error
        raise WebRenderError(
            f"pypdfium2 failed to rasterize {url}: {exc}"
        ) from exc
    finally:
        # PdfDocument holds native buffers — close it explicitly so the
        # subprocess doesn't depend on GC to release them before exit.
        if pdf is not None:
            pdf.close()

    if not png_bytes:
        raise WebRenderError(
            f"rasterizing {url} produced an empty PNG"
        )

    return png_bytes


def main(argv: list[str] | None = None) -> int:
    """Subprocess entry point — see the module docstring's argv
    contract.

    Parses argv (`<url> <panel_w> <panel_h> <out_path>`), renders the
    page, and writes the PNG to `<out_path>`. Returns a process exit
    code; `__main__` below passes it to `sys.exit`.

    Args:
        argv: the argument list WITHOUT the program name (defaults to
            `sys.argv[1:]`). Explicit so the unit tests can drive
            `main` directly.

    Returns:
        0  — success, PNG written.
        1  — the render failed (a clear reason printed to stderr).
        2  — bad argv: wrong argument count, a non-integer / non-
             positive dimension, or a URL that failed validation.
    """
    if argv is None:
        argv = sys.argv[1:]

    if len(argv) != 4:
        print(
            "usage: python -m openmarquee.web_render "
            "<url> <panel_w> <panel_h> <out_path>",
            file=sys.stderr,
        )
        return 2

    url, raw_w, raw_h, out_path = argv

    try:
        panel_w = int(raw_w)
        panel_h = int(raw_h)
    except ValueError:
        print(
            f"web-render: panel dimensions must be integers, "
            f"got width={raw_w!r} height={raw_h!r}",
            file=sys.stderr,
        )
        return 2
    if panel_w <= 0 or panel_h <= 0:
        print(
            f"web-render: panel dimensions must be positive, "
            f"got {panel_w}x{panel_h}",
            file=sys.stderr,
        )
        return 2

    try:
        # A rejected URL (file://, etc.) raises ValueError — treat it
        # as a bad-argv error (exit 2), not a render failure: the
        # operator gave us input we will not even attempt to render.
        png_bytes = render_web_png(url, panel_w, panel_h)
    except ValueError as exc:
        print(f"web-render: invalid URL: {exc}", file=sys.stderr)
        return 2
    except WebRenderError as exc:
        print(f"web-render: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — last-ditch guard
        # render_web_png funnels library errors into WebRenderError,
        # but a genuinely unexpected failure must still exit non-zero
        # with a message rather than dump a traceback the producer
        # can't parse.
        print(f"web-render: unexpected error: {exc}", file=sys.stderr)
        return 1

    try:
        with open(out_path, "wb") as fh:
            fh.write(png_bytes)
    except OSError as exc:
        print(
            f"web-render: failed to write PNG to {out_path}: {exc}",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess
    sys.exit(main())


__all__ = [
    "render_web_png",
    "main",
    "WebRenderError",
    "WEB_RENDER_FETCH_TIMEOUT_S",
]
