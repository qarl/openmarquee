"""On-device web-slide renderer — turns a URL into a panel-sized PNG
entirely on the Pi, with no external render helper.

A WebSlide is "an image slide whose asset.png is auto-refreshed from a
render of an operator-supplied URL". Historically that render happened
on an operator's own machine (a headless-Chromium helper in
`web-helper/`) because the Pi can't run a browser. This module replaces
that: it rasterizes the page on-device.

Render engine — Chromium headless. The page is rendered by the
**Chromium browser** running headless, driven through its built-in CLI
(`chromium --headless --screenshot ...`). This is a full modern web
engine: JavaScript runs, web fonts load, CSS animations settle — so a
page can ship its own layout/`fit()` JS and have it execute before the
screenshot is taken (`--virtual-time-budget` gives that JS a generous
slice of virtual time). Chromium is a *subprocess* this module spawns;
there is no Python import for it (the binary is `chromium` or
`chromium-browser`, resolved at call time via `shutil.which`).

Pipeline: `chromium --screenshot (render_w x render_h PNG) -> Pillow
composite onto a panel_w x panel_h black canvas -> PNG bytes`.

Pillarbox compositing. The Web slide shows on a fixed-size sign panel.
A page may be authored narrower (or differently shaped) than the panel.
The locked decision is to *pillarbox*: render the page at its own
render window size, then composite that render CENTERED on a
panel-sized solid-BLACK canvas. The leftover space becomes black bars.
The page is never cropped to its content and content is never
bitmap-upscaled — see `composite_pillarbox` for the three cases.

RAM mitigation — short-lived subprocess. Chromium's footprint is far
too large to keep resident on the Pi's tight memory budget. This module
is therefore designed to run AS A STANDALONE SUBPROCESS — `python -m
openmarquee.web_render <url> <render_w> <render_h> <panel_w> <panel_h>
<out_path>` — so that footprint is reclaimed by the OS the moment the
process exits. C3's producer spawns it that way and reads the PNG back
off `<out_path>`. The module is still importable + the render function
still directly callable in-process (the tests do that, with the
chromium subprocess mocked) — the subprocess wrapper is an option, not
a requirement of the API.

argv contract (the subprocess entry point — see `main`):

    python -m openmarquee.web_render \\
        <url> <render_w> <render_h> <panel_w> <panel_h> <out_path>

    <url>        the page to render — must be http/https (validated)
    <render_w>   Chromium render-window width  (positive integer)
    <render_h>   Chromium render-window height (positive integer)
    <panel_w>    sign panel width in pixels    (positive integer)
    <panel_h>    sign panel height in pixels   (positive integer)
    <out_path>   filesystem path the panel-sized PNG is written to

    exit 0  — panel-sized PNG written to <out_path>
    exit 1  — render failed (a one-line reason on stderr)
    exit 2  — bad argv (wrong count / non-integer or non-positive
              dims / rejected URL)

Deferred import: `PIL` (Pillow) is imported INSIDE the functions that
use it, not at module top level — mirroring the historical
deferred-import discipline. Pillow is a backend dependency, so this is
ordering hygiene rather than an optional-dependency guard. Chromium has
no Python import at all; it is resolved with `shutil.which` and spawned.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import tempfile
from io import BytesIO
from pathlib import Path

from openmarquee.content import validate_web_url

log = logging.getLogger(__name__)

# Overall wall-clock budget for the Chromium subprocess, in seconds. A
# real render on the Pi runs ~5-40s (page fetch + JS + the
# virtual-time-budget settle), so this is generous headroom. On expiry
# the subprocess is KILLED and reaped (never orphaned) and the render
# raises WebRenderError — the module never hangs unboundedly. C3's
# producer may additionally wrap the whole `python -m ...` invocation
# in its own timeout; this is the inner, Chromium-specific bound.
WEB_RENDER_TIMEOUT_S = 45.0

# Virtual-time budget handed to Chromium, in MILLISECONDS. Chromium
# advances the page's clock as fast as it can up to this many ms of
# virtual time and only then takes the screenshot — so a page's own
# JavaScript (timers, a `fit()` reflow, font swaps) gets to run before
# the capture, without us waiting that long in wall-clock terms.
WEB_RENDER_VIRTUAL_TIME_BUDGET_MS = 12000

# The candidate names of the Chromium binary, in resolution order.
# Debian/Raspberry Pi OS packages it as `chromium`; some distros still
# ship `chromium-browser`. `shutil.which` tries each; the first hit is
# used, and if neither resolves a typed WebRenderError is raised.
_CHROMIUM_BINARIES = ("chromium", "chromium-browser")

# The 8-byte PNG signature. Chromium's `--screenshot` output is
# sanity-checked against this before compositing, so a truncated or
# non-PNG file (a crash that still exited 0, say) fails loudly here
# rather than as an opaque Pillow error.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class WebRenderError(Exception):
    """A web render failed — Chromium missing, the Chromium subprocess
    erroring or timing out, a missing/non-PNG screenshot, or a Pillow
    composite error. Raised by `render_web_png` so callers (and the
    subprocess `main`) get one clear, typed failure instead of a
    library/OS-specific exception or an unbounded hang."""


def _resolve_chromium() -> str:
    """Resolve the Chromium binary path, trying each name in
    `_CHROMIUM_BINARIES`.

    Returns the absolute path of the first binary `shutil.which` finds.

    Raises:
        WebRenderError: neither `chromium` nor `chromium-browser` is on
            PATH — there is no browser to render with.
    """
    for name in _CHROMIUM_BINARIES:
        path = shutil.which(name)
        if path:
            return path
    raise WebRenderError(
        "no Chromium binary found on PATH (looked for "
        f"{', '.join(_CHROMIUM_BINARIES)})"
    )


def _build_chromium_argv(
    chromium: str, url: str, render_w: int, render_h: int, screenshot_path: str
) -> list[str]:
    """Build the Chromium headless-screenshot command line.

    Pure function — unit-tested directly. The flags:

      --headless              run without a UI / display server
      --no-sandbox            the Pi runs this as a service user with
                              no user namespaces; the sandbox can't
                              initialize, so it is disabled explicitly
      --disable-gpu           no GPU compositing path in headless on
                              the Pi's vc4 — software raster only
      --disable-dev-shm-usage write tmpfiles under /tmp not /dev/shm;
                              the Pi's small /dev/shm otherwise causes
                              Chromium to crash mid-render
      --hide-scrollbars       a captured scrollbar is render litter
      --virtual-time-budget   advance the page clock this many ms so the
                              page's own JS runs before the screenshot
      --screenshot=<path>     write the PNG capture here
      --window-size=<w>,<h>   the render viewport — Chromium emits a PNG
                              of exactly this pixel size
    """
    return [
        chromium,
        "--headless",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--hide-scrollbars",
        f"--virtual-time-budget={WEB_RENDER_VIRTUAL_TIME_BUDGET_MS}",
        f"--screenshot={screenshot_path}",
        f"--window-size={render_w},{render_h}",
        url,
    ]


def composite_pillarbox(
    render_png: bytes, panel_w: int, panel_h: int
) -> bytes:
    """Composite a Chromium render onto a panel-sized black canvas and
    return the panel-sized PNG bytes.

    Pillarbox semantics — `render_png` is a PNG of whatever size
    Chromium produced (the render window size). It is placed CENTERED on
    a fresh `panel_w x panel_h` solid-BLACK canvas. Three cases:

      1. render == panel  — the render already IS the panel; it is
         returned re-encoded with no bars (a generic landscape page
         rendered straight at panel size).
      2. render SMALLER than the panel on an axis — black bars on that
         axis, centered. This is the pillarbox: the render is pasted
         unscaled and the margin is black.
      3. render LARGER than the panel on an axis — the whole render is
         uniformly scaled DOWN to fit within the panel (aspect ratio
         preserved), then centered with black padding on the other
         axis. This is a composite/pad downscale-to-fit, NOT a
         crop-to-content + upscale (which the design explicitly
         rejects); it only handles an oversize render.

    The output PNG is always exactly `panel_w x panel_h`.

    Pure function (no I/O) — unit-tested directly. Pillow is imported
    here, not at module scope.

    Args:
        render_png: the Chromium screenshot, as PNG bytes.
        panel_w, panel_h: the sign panel dimensions in pixels.

    Returns:
        PNG bytes — a panel-sized, RGB, top-down PNG.

    Raises:
        WebRenderError: `render_png` is not a decodable image, or the
            composite otherwise fails.
    """
    from PIL import Image

    try:
        with Image.open(BytesIO(render_png)) as opened:
            # Materialize to RGB up front: the paste target is RGB, and
            # `Image.open` is lazy — load before the `with` closes the
            # backing buffer.
            render = opened.convert("RGB")
    except Exception as exc:  # noqa: BLE001 — funnel every decode error
        raise WebRenderError(
            f"Chromium screenshot is not a decodable image: {exc}"
        ) from exc

    try:
        # Case 3: the render overflows the panel on at least one axis —
        # uniformly scale it DOWN to fit inside the panel. The scale is
        # the smaller of the two per-axis ratios so neither axis spills;
        # `min(..., 1.0)` makes this a no-op when the render already
        # fits (cases 1 and 2).
        rw, rh = render.size
        scale = min(panel_w / rw, panel_h / rh, 1.0)
        if scale < 1.0:
            scaled_w = max(1, round(rw * scale))
            scaled_h = max(1, round(rh * scale))
            render = render.resize(
                (scaled_w, scaled_h), Image.LANCZOS
            )

        # Case 1 fast-ish path / general path: paste the (possibly
        # downscaled) render centered on a solid-black panel canvas.
        # When render == panel the paste covers the whole canvas and
        # there are simply no bars.
        canvas = Image.new("RGB", (panel_w, panel_h), (0, 0, 0))
        off_x = (panel_w - render.size[0]) // 2
        off_y = (panel_h - render.size[1]) // 2
        canvas.paste(render, (off_x, off_y))

        buf = BytesIO()
        canvas.save(buf, format="PNG")
        return buf.getvalue()
    except WebRenderError:
        raise
    except Exception as exc:  # noqa: BLE001 — funnel every Pillow error
        raise WebRenderError(
            f"failed to composite the web render onto the panel: {exc}"
        ) from exc


def render_web_png(
    url: str,
    render_w: int,
    render_h: int,
    panel_w: int,
    panel_h: int,
) -> bytes:
    """Render `url` with Chromium headless and composite it pillarboxed
    onto a `panel_w x panel_h` black panel; return the panel PNG bytes.

    Pipeline: Chromium headless loads `url` at a `render_w x render_h`
    window and writes a PNG screenshot (its own JavaScript gets
    `WEB_RENDER_VIRTUAL_TIME_BUDGET_MS` of virtual time to run first);
    that render is then composited CENTERED on a panel-sized solid-black
    canvas (`composite_pillarbox`) so the output is exactly the panel
    size.

    The URL is validated (`validate_web_url`, http/https only) BEFORE
    Chromium is spawned — a `file://` URL handed to a browser is a
    local-file-read vector, so a non-web URL never reaches it.

    The Chromium subprocess is bounded by `WEB_RENDER_TIMEOUT_S`; on
    timeout it is KILLED and reaped (never orphaned) and a
    WebRenderError is raised. The render therefore never hangs.

    Args:
        url: the page to render. Must be http/https.
        render_w, render_h: the Chromium render-window size in pixels.
        panel_w, panel_h: the sign panel size in pixels. The output PNG
            is exactly this size.

    Returns:
        PNG bytes — a panel-sized, top-down PNG.

    Raises:
        ValueError: `url` failed `validate_web_url` (non-http/https
            scheme, control characters, a userinfo component), or a
            dimension is non-positive. Raised before Chromium is
            spawned — a caller/input error, distinct from a render
            failure.
        WebRenderError: the render failed — Chromium missing, the
            subprocess erroring or timing out, a missing/non-PNG
            screenshot, or a composite error. Never hangs unboundedly.
    """
    # Validate FIRST — before resolving or spawning Chromium. A
    # `file://` / `data://` URL must never reach the browser. A bad URL
    # raises ValueError straight out (not WebRenderError): it's a
    # caller/input error, distinct from a render failure.
    validate_web_url(url)

    if render_w <= 0 or render_h <= 0:
        raise ValueError(
            f"render dimensions must be positive, got {render_w}x{render_h}"
        )
    if panel_w <= 0 or panel_h <= 0:
        raise ValueError(
            f"panel dimensions must be positive, got {panel_w}x{panel_h}"
        )

    chromium = _resolve_chromium()

    # Chromium writes its screenshot to a temp file; we read it back,
    # composite, and delete the temp file in the `finally`. A NamedTemp
    # is created and immediately closed so Chromium (a separate process)
    # owns the path exclusively — we only need a unique filesystem path.
    tmp = tempfile.NamedTemporaryFile(
        prefix="web-render-", suffix=".png", delete=False
    )
    tmp.close()
    screenshot_path = tmp.name

    try:
        argv = _build_chromium_argv(
            chromium, url, render_w, render_h, screenshot_path
        )
        # --- Chromium headless: URL -> screenshot PNG ------------------
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                timeout=WEB_RENDER_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            # `subprocess.run` already kills the child and reaps it on a
            # TimeoutExpired before re-raising — so the process is not
            # orphaned. Surface it as a typed, bounded failure.
            raise WebRenderError(
                f"Chromium render of {url} timed out after "
                f"{WEB_RENDER_TIMEOUT_S:.0f}s"
            ) from exc
        except OSError as exc:
            raise WebRenderError(
                f"failed to launch Chromium for {url}: {exc}"
            ) from exc

        if proc.returncode != 0:
            stderr = (proc.stderr or b"").decode(
                "utf-8", errors="replace"
            ).strip()
            # Keep the message single-line — the subprocess `main`
            # prints it straight to stderr.
            detail = stderr.splitlines()[-1] if stderr else "(no output)"
            raise WebRenderError(
                f"Chromium exited {proc.returncode} rendering {url}: "
                f"{detail}"
            )

        # --- read + sanity-check Chromium's screenshot ----------------
        shot = Path(screenshot_path)
        if not shot.exists():
            raise WebRenderError(
                f"Chromium produced no screenshot file for {url}"
            )
        render_png = shot.read_bytes()
        if not render_png:
            raise WebRenderError(
                f"Chromium produced an empty screenshot for {url}"
            )
        if not render_png.startswith(_PNG_MAGIC):
            raise WebRenderError(
                f"Chromium screenshot for {url} is not a PNG"
            )

        # --- Pillow: pillarbox-composite onto the panel ---------------
        return composite_pillarbox(render_png, panel_w, panel_h)
    finally:
        # Always clean up Chromium's temp output, success or failure.
        try:
            Path(screenshot_path).unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover — best-effort cleanup
            log.warning(
                "web-render: could not remove temp screenshot %s: %s",
                screenshot_path,
                exc,
            )


def main(argv: list[str] | None = None) -> int:
    """Subprocess entry point — see the module docstring's argv
    contract.

    Parses argv (`<url> <render_w> <render_h> <panel_w> <panel_h>
    <out_path>`), renders the page, and writes the panel-sized PNG to
    `<out_path>`. Returns a process exit code; `__main__` below passes
    it to `sys.exit`.

    Args:
        argv: the argument list WITHOUT the program name (defaults to
            `sys.argv[1:]`). Explicit so the unit tests can drive
            `main` directly.

    Returns:
        0  — success, panel-sized PNG written.
        1  — the render failed (a clear reason printed to stderr).
        2  — bad argv: wrong argument count, a non-integer / non-
             positive dimension, or a URL that failed validation.
    """
    if argv is None:
        argv = sys.argv[1:]

    if len(argv) != 6:
        print(
            "usage: python -m openmarquee.web_render "
            "<url> <render_w> <render_h> <panel_w> <panel_h> <out_path>",
            file=sys.stderr,
        )
        return 2

    url, raw_rw, raw_rh, raw_pw, raw_ph, out_path = argv

    try:
        render_w = int(raw_rw)
        render_h = int(raw_rh)
        panel_w = int(raw_pw)
        panel_h = int(raw_ph)
    except ValueError:
        print(
            "web-render: render/panel dimensions must be integers, got "
            f"render={raw_rw!r}x{raw_rh!r} panel={raw_pw!r}x{raw_ph!r}",
            file=sys.stderr,
        )
        return 2
    if render_w <= 0 or render_h <= 0 or panel_w <= 0 or panel_h <= 0:
        print(
            "web-render: render/panel dimensions must be positive, got "
            f"render={render_w}x{render_h} panel={panel_w}x{panel_h}",
            file=sys.stderr,
        )
        return 2

    try:
        # A rejected URL (file://, etc.) raises ValueError — treat it
        # as a bad-argv error (exit 2), not a render failure: the
        # operator gave us input we will not even attempt to render.
        png_bytes = render_web_png(
            url, render_w, render_h, panel_w, panel_h
        )
    except ValueError as exc:
        print(f"web-render: invalid URL: {exc}", file=sys.stderr)
        return 2
    except WebRenderError as exc:
        print(f"web-render: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — last-ditch guard
        # render_web_png funnels expected errors into WebRenderError,
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
    "composite_pillarbox",
    "main",
    "WebRenderError",
    "WEB_RENDER_TIMEOUT_S",
    "WEB_RENDER_VIRTUAL_TIME_BUDGET_MS",
]
