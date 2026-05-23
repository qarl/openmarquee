"""On-device web-slide renderer — turns a URL into a display-sized PNG
entirely on the Pi, with no external render helper.

A WebSlide is "an image slide whose asset.png is auto-refreshed from a
render of an operator-supplied URL". Historically that render happened
off-device — a headless-Chromium helper the operator ran on their own
machine — because the Pi can't run a browser. This module replaces
that: it rasterizes the page on-device.

Render engine — the Chromium headless shell. The page is rendered by
**`chromium-headless-shell`**, the slim headless-only Chromium binary
(no browser UI / extensions / PDF stack), driven through its built-in
`--screenshot` CLI. It is still a full modern web engine: JavaScript
runs, web fonts load, CSS animations settle — so a page can ship its
own layout/`fit()` JS and have it execute before the screenshot is
taken (`--virtual-time-budget` gives that JS a generous slice of
virtual time). The slim shell is what lets a render FIT alongside the
playback renderer on the memory-tight Pi: measured on the dev Pi (a
Pi Zero 2 W) it renders in ~3 min with the renderer running, whereas
the full `chromium` browser swap-thrashes too hard to finish at all.
Chromium is a *subprocess* this module spawns; there is no Python
import for it. The binary is resolved at call time via `shutil.which`
— `chromium-headless-shell` preferred, the full `chromium` /
`chromium-browser` browser kept only as a fallback.

Render size — the live display resolution. The page is rendered at the
sign's ACTUAL current display resolution. The caller (the screenshot
producer) reads that from the renderer's negotiated DRM/KMS mode, which
is rotation-aware — the renderer reports portrait logical dims when the
sign is rotated to 90/270 (FYS bug 5). The render IS that size: there
is NO panel / letterbox / pillarbox compositing. A page authored for a
different shape letterboxes itself via its own CSS (e.g. a portrait
newspaper page centered by its dark body background). When the sign is
physically rotated to portrait and the rotation set, the display
resolution becomes portrait and the page fills it directly.

Pipeline: the headless shell's `--screenshot` writes a `width x height`
PNG; this module validates it (the PNG signature) and returns those
bytes verbatim.

argv contract (the subprocess entry point — see `main`):

    python -m openmarquee.web_render <url> <width> <height> <out_path>

    <url>       the page to render — must be http/https (validated)
    <width>     render width in pixels  (positive integer)
    <height>    render height in pixels (positive integer)
    <out_path>  filesystem path the PNG is written to

    exit 0  — PNG written to <out_path>
    exit 1  — render failed (a one-line reason on stderr)
    exit 2  — bad argv (wrong count / non-integer or non-positive
              dims / rejected URL)

The render function `render_web_png` is also directly importable and
callable in-process — that is how the screenshot producer drives it
(off the event loop, via a worker thread). The `main` subprocess entry
point is retained as a hand-testing / debugging convenience (`python -m
openmarquee.web_render <url> <w> <h> <out>` on the Pi); it is not on the
production code path.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from openmarquee.content import validate_web_url

log = logging.getLogger(__name__)

# Overall wall-clock budget for the Chromium subprocess, in seconds.
# On the memory-tight Pi a render is SLOW: measured ~5-8.5 min on the
# dev Pi (a Pi Zero 2 W) with the backend running — Chromium
# swap-thrashes the 416 MB box. This 20-minute budget covers that, plus
# the heavier FYS case (the Rust renderer also resident), with margin —
# while still bounding a genuinely hung render. On expiry the
# subprocess is KILLED and reaped (never orphaned) and the render
# raises WebRenderError — the module never hangs unboundedly.
# (The ~5-8.5 min figure above is the full-`chromium`-browser worst
# case that set this ceiling; chromium-headless-shell — the engine
# this module now uses — measured ~3 min on the dev Pi. The generous
# ceiling stays as a hang backstop.)
WEB_RENDER_TIMEOUT_S = 1200.0

# Virtual-time budget handed to Chromium, in MILLISECONDS. Chromium
# advances the page's clock as fast as it can up to this many ms of
# virtual time and only then takes the screenshot — so a page's own
# JavaScript (timers, a `fit()` reflow, font swaps) gets to run before
# the capture, without us waiting that long in wall-clock terms.
WEB_RENDER_VIRTUAL_TIME_BUDGET_MS = 12000

# The candidate Chromium binaries, in resolution order — the slim
# headless-only shell is PREFERRED. `chromium-headless-shell` is the
# purpose-built headless binary (no browser UI / extensions / PDF
# stack) — a lighter RSS footprint, which the memory-tight Pi needs to
# fit a render alongside the playback renderer. Debian/Raspberry Pi OS
# also package the full browser as `chromium` (older distros:
# `chromium-browser`), kept as fallbacks. `shutil.which` tries each in
# order; the first hit wins; if none resolves a typed WebRenderError is
# raised.
_CHROMIUM_BINARIES = (
    "chromium-headless-shell",
    "chromium",
    "chromium-browser",
)

# The 8-byte PNG signature. Chromium's `--screenshot` output is
# sanity-checked against this before it is returned, so a truncated or
# non-PNG file (a crash that still exited 0, say) fails loudly here
# rather than later as an opaque image-decode error in the bake path.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class WebRenderError(Exception):
    """A web render failed — Chromium missing, the Chromium subprocess
    erroring or timing out, or a missing / empty / non-PNG screenshot.
    Raised by `render_web_png` so callers (and the subprocess `main`)
    get one clear, typed failure instead of a library/OS-specific
    exception or an unbounded hang."""


def _resolve_chromium() -> str:
    """Resolve the Chromium binary path, trying each name in
    `_CHROMIUM_BINARIES`.

    Returns the absolute path of the first binary `shutil.which` finds.

    Raises:
        WebRenderError: none of `_CHROMIUM_BINARIES`
            (`chromium-headless-shell`, `chromium`, `chromium-browser`)
            is on PATH — there is no engine to render with.
    """
    for name in _CHROMIUM_BINARIES:
        path = shutil.which(name)
        if path:
            return path
    raise WebRenderError(
        f"no Chromium binary found on PATH (looked for {', '.join(_CHROMIUM_BINARIES)})"
    )


def _build_chromium_argv(
    chromium: str, url: str, width: int, height: int, screenshot_path: str
) -> list[str]:
    """Build the Chromium headless-screenshot command line.

    Pure function — unit-tested directly. The flags:

      --headless              run without a UI / display server. Added
                              ONLY for the full `chromium` binary;
                              `chromium-headless-shell` is inherently
                              headless and is not given it.
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
    argv = [chromium]
    # The full `chromium` binary needs --headless to suppress the UI;
    # chromium-headless-shell is headless by construction and is not
    # given it.
    if "headless-shell" not in chromium:
        argv.append("--headless")
    argv += [
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--hide-scrollbars",
        f"--virtual-time-budget={WEB_RENDER_VIRTUAL_TIME_BUDGET_MS}",
        f"--screenshot={screenshot_path}",
        f"--window-size={width},{height}",
        url,
    ]
    return argv


def _nice_prefix() -> list[str]:
    """Build the command prefix that de-prioritizes the transient
    Chromium render so it yields to the playback renderer and backend.

    On the Pi a render is multi-minute and swap-thrashes the
    memory-tight box; without this the Rust renderer would lose frames
    for the whole render. `nice -n 19` drops Chromium to the lowest CPU
    priority — and the kernel derives a correspondingly low best-effort
    block-I/O priority from that nice value, so the heavy swap traffic
    is deprioritized too, WITHOUT the starvation that the idle I/O
    class (`ionice -c 3`) causes here: an idle-class render measured on
    the dev Pi with playback live blocked indefinitely in
    `folio_wait_bit_common` — starved of swap-in I/O by the renderer's
    steady disk traffic — and never finished. `nice` alone
    deprioritizes without ever starving, so that is all this uses.

    Best-effort: the prefix is included only if `nice` resolves on
    PATH, so a host without it still renders (just without the
    mitigation) — which is also what lets the unit tests, on hosts
    without `nice`, see the bare Chromium argv.

    `nice` execs its argument, so the prefix chains: the final process
    image is still Chromium and `subprocess.run` sees Chromium's own
    exit code.
    """
    prefix: list[str] = []
    nice = shutil.which("nice")
    if nice:
        prefix += [nice, "-n", "19"]
    return prefix


def render_web_png(url: str, width: int, height: int) -> bytes:
    """Render `url` with Chromium headless at `width` x `height` and
    return the PNG bytes.

    `width`/`height` are the sign's live display resolution — the
    caller reads them from the renderer's negotiated, rotation-aware
    DRM/KMS mode, so the render always matches the screen the slide is
    shown on (landscape, or portrait when the sign is rotated). The
    page is rendered AT that size; there is no panel / letterbox
    compositing — a page authored for a different shape letterboxes
    itself via its own CSS.

    The URL is validated (`validate_web_url`, http/https only) BEFORE
    Chromium is spawned — a `file://` URL handed to a browser is a
    local-file-read vector, so a non-web URL never reaches it.

    The Chromium subprocess is bounded by `WEB_RENDER_TIMEOUT_S`; on
    timeout it is KILLED and reaped (never orphaned) and a
    WebRenderError is raised. The render therefore never hangs.

    Args:
        url: the page to render. Must be http/https.
        width, height: the render-window size in pixels — the sign's
            live display resolution. The output PNG is this size
            (Chromium emits a PNG of exactly the `--window-size`).

    Returns:
        PNG bytes — Chromium's screenshot, verified to carry the PNG
        signature.

    Raises:
        ValueError: `url` failed `validate_web_url` (non-http/https
            scheme, control characters, a userinfo component), or a
            dimension is non-positive. Raised before Chromium is
            spawned — a caller/input error, distinct from a render
            failure.
        WebRenderError: the render failed — Chromium missing, the
            subprocess erroring or timing out, or a missing / empty /
            non-PNG screenshot. Never hangs unboundedly.
    """
    # Validate FIRST — before resolving or spawning Chromium. A
    # `file://` / `data://` URL must never reach the browser. A bad URL
    # raises ValueError straight out (not WebRenderError): it's a
    # caller/input error, distinct from a render failure.
    validate_web_url(url)

    if width <= 0 or height <= 0:
        raise ValueError(f"render dimensions must be positive, got {width}x{height}")

    chromium = _resolve_chromium()

    # Chromium writes its screenshot to a temp file; we read it back
    # and delete the temp file in the `finally`. A NamedTemp is created
    # and immediately closed so Chromium (a separate process) owns the
    # path exclusively — we only need a unique filesystem path.
    with tempfile.NamedTemporaryFile(prefix="web-render-", suffix=".png", delete=False) as tmp:
        screenshot_path = tmp.name

    try:
        # Prefix nice so the multi-minute render yields CPU to the
        # playback renderer instead of stuttering the sign.
        argv = _nice_prefix() + _build_chromium_argv(chromium, url, width, height, screenshot_path)
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
                f"Chromium render of {url} timed out after {WEB_RENDER_TIMEOUT_S:.0f}s"
            ) from exc
        except OSError as exc:
            raise WebRenderError(f"failed to launch Chromium for {url}: {exc}") from exc

        if proc.returncode != 0:
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            # Keep the message single-line — the subprocess `main`
            # prints it straight to stderr.
            detail = stderr.splitlines()[-1] if stderr else "(no output)"
            raise WebRenderError(f"Chromium exited {proc.returncode} rendering {url}: {detail}")

        # --- read + sanity-check Chromium's screenshot ----------------
        shot = Path(screenshot_path)
        if not shot.exists():
            raise WebRenderError(f"Chromium produced no screenshot file for {url}")
        render_png = shot.read_bytes()
        if not render_png:
            raise WebRenderError(f"Chromium produced an empty screenshot for {url}")
        if not render_png.startswith(_PNG_MAGIC):
            raise WebRenderError(f"Chromium screenshot for {url} is not a PNG")

        return render_png
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

    Parses argv (`<url> <width> <height> <out_path>`), renders the
    page, and writes the PNG to `<out_path>`. Returns a process exit
    code; `__main__` below passes it to `sys.exit`.

    Args:
        argv: the argument list WITHOUT the program name (defaults to
            `sys.argv[1:]`). Explicit so the unit tests can drive
            `main` directly.

    Returns:
        0  — success, the PNG was written.
        1  — the render failed (a clear reason printed to stderr).
        2  — bad argv: wrong argument count, a non-integer / non-
             positive dimension, or a URL that failed validation.
    """
    if argv is None:
        argv = sys.argv[1:]

    if len(argv) != 4:
        print(
            "usage: python -m openmarquee.web_render <url> <width> <height> <out_path>",
            file=sys.stderr,
        )
        return 2

    url, raw_w, raw_h, out_path = argv

    try:
        width = int(raw_w)
        height = int(raw_h)
    except ValueError:
        print(
            f"web-render: render dimensions must be integers, got {raw_w!r}x{raw_h!r}",
            file=sys.stderr,
        )
        return 2
    if width <= 0 or height <= 0:
        print(
            f"web-render: render dimensions must be positive, got {width}x{height}",
            file=sys.stderr,
        )
        return 2

    try:
        # A rejected URL (file://, etc.) raises ValueError — treat it
        # as a bad-argv error (exit 2), not a render failure: the
        # operator gave us input we will not even attempt to render.
        png_bytes = render_web_png(url, width, height)
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
    "main",
    "WebRenderError",
    "WEB_RENDER_TIMEOUT_S",
    "WEB_RENDER_VIRTUAL_TIME_BUDGET_MS",
]
