"""Tests for the on-device web-slide renderer (Web slide C2).

`openmarquee.web_render` turns an operator-supplied URL into a
panel-sized PNG entirely on the Pi:
`chromium --headless --screenshot -> Pillow pillarbox composite -> PNG`.

The Chromium browser is a *subprocess* the module spawns (there is no
Python import for it). These tests mock `subprocess.run` — so the whole
suite runs on a host WITHOUT Chromium installed — and assert the wiring
(the headless flags, `--window-size`, `--screenshot`, the timeout +
kill/reap behavior, the typed error). `shutil.which` is also patched so
binary resolution is deterministic.

The pillarbox composite is pure Pillow and is unit-tested directly:
a render smaller than the panel pillarboxes with black bars, a render
equal to the panel produces no bars, a render larger than the panel is
uniformly downscaled to fit and centered.
"""

import subprocess
from io import BytesIO

import pytest
from PIL import Image

from openmarquee import web_render
from openmarquee.web_render import (
    WEB_RENDER_TIMEOUT_S,
    WEB_RENDER_VIRTUAL_TIME_BUDGET_MS,
    WebRenderError,
    _build_chromium_argv,
    composite_pillarbox,
    main,
    render_web_png,
)


# ---------------------------------------------------------------------
# Helpers — build real PNG bytes so the fakes hand back genuine images.
# ---------------------------------------------------------------------
def _solid_png(width: int, height: int, color) -> bytes:
    """A real `width x height` solid-color PNG, as bytes."""
    buf = BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def _open_png(data: bytes) -> Image.Image:
    """Decode PNG bytes to a loaded RGB PIL image for pixel assertions."""
    with Image.open(BytesIO(data)) as img:
        return img.convert("RGB")


# ---------------------------------------------------------------------
# Fake Chromium subprocess.
# ---------------------------------------------------------------------
class _FakeCompletedProcess:
    """Stand-in for `subprocess.CompletedProcess`."""

    def __init__(self, returncode=0, stderr=b""):
        self.returncode = returncode
        self.stdout = b""
        self.stderr = stderr


def _install_fake_chromium(
    monkeypatch,
    *,
    which="/usr/bin/chromium",
    returncode=0,
    stderr=b"",
    raise_timeout=False,
    launch_error=None,
    screenshot_png=None,
    write_screenshot=True,
    delete_screenshot=False,
    calls=None,
):
    """Patch `shutil.which` + `subprocess.run` so `render_web_png` runs
    against a fake Chromium.

    `which` is what `shutil.which` returns (None -> no binary found).
    The fake `subprocess.run` records its argv/kwargs into `calls`,
    optionally writes `screenshot_png` to the `--screenshot=` path
    (mimicking a real Chromium capture), and then either returns a
    `_FakeCompletedProcess`, raises `subprocess.TimeoutExpired`, or
    raises `launch_error`.
    """
    if screenshot_png is None:
        screenshot_png = _solid_png(800, 600, (10, 120, 200))

    monkeypatch.setattr(
        web_render.shutil, "which",
        lambda name: which if name in web_render._CHROMIUM_BINARIES else None,
    )

    def _fake_run(argv, capture_output=False, timeout=None):
        if calls is not None:
            calls["argv"] = argv
            calls["timeout"] = timeout
            calls["capture_output"] = capture_output
        # Mimic Chromium writing its --screenshot file. The module
        # pre-creates the temp path (so Chromium owns it exclusively),
        # so it exists+empty by default; `write_screenshot` fills it,
        # `delete_screenshot` removes it to drive the missing-file path.
        for arg in argv:
            if arg.startswith("--screenshot="):
                path = arg.split("=", 1)[1]
                if delete_screenshot:
                    import os
                    os.unlink(path)
                elif write_screenshot:
                    with open(path, "wb") as fh:
                        fh.write(screenshot_png)
        if launch_error is not None:
            raise launch_error
        if raise_timeout:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)
        return _FakeCompletedProcess(returncode=returncode, stderr=stderr)

    monkeypatch.setattr(web_render.subprocess, "run", _fake_run)


# ---------------------------------------------------------------------
# _build_chromium_argv — the Chromium command-line builder.
# ---------------------------------------------------------------------
def test_build_chromium_argv_has_headless_flags():
    """The Chromium command line carries the headless / no-sandbox /
    dev-shm flags the Pi needs."""
    argv = _build_chromium_argv(
        "/usr/bin/chromium", "https://x.example.com", 1360, 768, "/tmp/s.png"
    )
    assert argv[0] == "/usr/bin/chromium"
    assert "--headless" in argv
    assert "--no-sandbox" in argv
    assert "--disable-gpu" in argv
    assert "--disable-dev-shm-usage" in argv
    assert "--hide-scrollbars" in argv


def test_build_chromium_argv_window_size_and_screenshot():
    """`--window-size` carries the RENDER size; `--screenshot` carries
    the temp output path; the URL is the final argument."""
    argv = _build_chromium_argv(
        "/usr/bin/chromium", "https://x.example.com", 1024, 600, "/tmp/s.png"
    )
    assert "--window-size=1024,600" in argv
    assert "--screenshot=/tmp/s.png" in argv
    assert argv[-1] == "https://x.example.com"


def test_build_chromium_argv_virtual_time_budget():
    """`--virtual-time-budget` is set so the page's own JS runs before
    the screenshot is taken."""
    argv = _build_chromium_argv(
        "/usr/bin/chromium", "https://x.example.com", 1360, 768, "/tmp/s.png"
    )
    assert (
        f"--virtual-time-budget={WEB_RENDER_VIRTUAL_TIME_BUDGET_MS}" in argv
    )


# ---------------------------------------------------------------------
# composite_pillarbox — pure Pillow, the three pillarbox cases.
# ---------------------------------------------------------------------
def test_composite_pillarbox_smaller_render_gets_black_bars():
    """A render SMALLER than the panel is centered with BLACK bars —
    corners are black, the center carries the render's content."""
    # An 800x600 render onto a 1360x768 panel -> bars on all sides.
    render = _solid_png(800, 600, (10, 120, 200))
    out = composite_pillarbox(render, 1360, 768)

    img = _open_png(out)
    assert img.size == (1360, 768)
    # Corners are in the pillarbox margin -> black.
    assert img.getpixel((0, 0)) == (0, 0, 0)
    assert img.getpixel((1359, 0)) == (0, 0, 0)
    assert img.getpixel((0, 767)) == (0, 0, 0)
    assert img.getpixel((1359, 767)) == (0, 0, 0)
    # The center is inside the centered render -> the render's color.
    assert img.getpixel((680, 384)) == (10, 120, 200)


def test_composite_pillarbox_smaller_render_is_centered():
    """The smaller render is CENTERED — the content starts at the
    expected offset, not flush at (0, 0)."""
    # 800x600 on 1360x768: off_x = (1360-800)//2 = 280, off_y = 84.
    render = _solid_png(800, 600, (255, 0, 0))
    out = composite_pillarbox(render, 1360, 768)
    img = _open_png(out)

    # Just left of the content -> black; just inside -> red.
    assert img.getpixel((279, 384)) == (0, 0, 0)
    assert img.getpixel((280, 384)) == (255, 0, 0)
    # Just above the content -> black; just inside -> red.
    assert img.getpixel((680, 83)) == (0, 0, 0)
    assert img.getpixel((680, 84)) == (255, 0, 0)


def test_composite_pillarbox_equal_render_has_no_bars():
    """A render EQUAL to the panel produces no bars — every pixel,
    corners included, is the render's content."""
    render = _solid_png(1360, 768, (40, 200, 90))
    out = composite_pillarbox(render, 1360, 768)

    img = _open_png(out)
    assert img.size == (1360, 768)
    # No pillarbox -> corners carry content, not black.
    assert img.getpixel((0, 0)) == (40, 200, 90)
    assert img.getpixel((1359, 767)) == (40, 200, 90)
    assert img.getpixel((680, 384)) == (40, 200, 90)


def test_composite_pillarbox_larger_render_is_downscaled_to_fit():
    """A render LARGER than the panel is uniformly scaled DOWN to fit
    inside the panel, then centered — output is still panel-sized."""
    # A 2720x1536 render (exactly 2x the panel) onto a 1360x768 panel.
    # Uniform downscale by 0.5 -> 1360x768 -> exactly fills the panel.
    render = _solid_png(2720, 1536, (200, 50, 50))
    out = composite_pillarbox(render, 1360, 768)

    img = _open_png(out)
    assert img.size == (1360, 768)
    # 2x render downscales to exactly panel size -> no bars.
    assert img.getpixel((0, 0)) == (200, 50, 50)
    assert img.getpixel((680, 384)) == (200, 50, 50)


def test_composite_pillarbox_larger_render_wrong_aspect_is_padded():
    """A render larger than the panel on one axis with a different
    aspect ratio is downscaled to fit AND padded — black bars appear on
    the axis that doesn't fill, output stays panel-sized."""
    # A 2720x768 render (2x panel width, equal height) onto 1360x768.
    # min(1360/2720, 768/768) = 0.5 -> scaled to 1360x384 -> letterbox
    # bars top and bottom.
    render = _solid_png(2720, 768, (0, 0, 255))
    out = composite_pillarbox(render, 1360, 768)

    img = _open_png(out)
    assert img.size == (1360, 768)
    # Scaled height 384, centered -> off_y = (768-384)//2 = 192.
    assert img.getpixel((680, 0)) == (0, 0, 0)        # top bar
    assert img.getpixel((680, 767)) == (0, 0, 0)      # bottom bar
    assert img.getpixel((680, 384)) == (0, 0, 255)    # content band


def test_composite_pillarbox_rejects_non_image_bytes():
    """Bytes that aren't a decodable image surface as a typed
    WebRenderError, not a raw Pillow exception."""
    with pytest.raises(WebRenderError, match="not a decodable image"):
        composite_pillarbox(b"not a png at all", 1360, 768)


# ---------------------------------------------------------------------
# render_web_png — the success path + its wiring.
# ---------------------------------------------------------------------
def test_render_web_png_returns_panel_sized_png(monkeypatch):
    """A normal render returns real PNG bytes sized to the PANEL, not
    the render window."""
    _install_fake_chromium(
        monkeypatch, screenshot_png=_solid_png(800, 600, (10, 120, 200))
    )
    png = render_web_png("https://status.example.com", 800, 600, 1360, 768)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert _open_png(png).size == (1360, 768)


def test_render_web_png_spawns_chromium_with_render_window(monkeypatch):
    """Chromium is spawned with `--window-size=<render_w>,<render_h>`
    (the render size), `--screenshot`, and the headless flags."""
    calls = {}
    _install_fake_chromium(monkeypatch, calls=calls)
    render_web_png("https://status.example.com", 1024, 600, 1360, 768)

    argv = calls["argv"]
    assert argv[0] == "/usr/bin/chromium"
    assert "--headless" in argv
    assert "--window-size=1024,600" in argv
    assert any(a.startswith("--screenshot=") for a in argv)
    assert argv[-1] == "https://status.example.com"


def test_render_web_png_passes_overall_timeout(monkeypatch):
    """The Chromium subprocess is given the overall wall-clock timeout
    so the render never hangs unboundedly."""
    calls = {}
    _install_fake_chromium(monkeypatch, calls=calls)
    render_web_png("https://status.example.com", 1360, 768, 1360, 768)
    assert calls["timeout"] == WEB_RENDER_TIMEOUT_S


def test_render_web_png_equal_render_panel_has_no_bars(monkeypatch):
    """render == panel: the render is returned as the panel with no
    pillarbox bars."""
    _install_fake_chromium(
        monkeypatch, screenshot_png=_solid_png(1360, 768, (40, 200, 90))
    )
    png = render_web_png("https://status.example.com", 1360, 768, 1360, 768)
    img = _open_png(png)
    assert img.size == (1360, 768)
    assert img.getpixel((0, 0)) == (40, 200, 90)


def test_render_web_png_resolves_chromium_browser_fallback(monkeypatch):
    """If `chromium` is absent but `chromium-browser` is present, the
    fallback name is used."""
    calls = {}
    monkeypatch.setattr(
        web_render.shutil, "which",
        lambda name: "/usr/bin/chromium-browser"
        if name == "chromium-browser" else None,
    )

    def _fake_run(argv, capture_output=False, timeout=None):
        calls["argv"] = argv
        for arg in argv:
            if arg.startswith("--screenshot="):
                with open(arg.split("=", 1)[1], "wb") as fh:
                    fh.write(_solid_png(800, 600, (1, 2, 3)))
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(web_render.subprocess, "run", _fake_run)
    render_web_png("https://status.example.com", 800, 600, 1360, 768)
    assert calls["argv"][0] == "/usr/bin/chromium-browser"


# ---------------------------------------------------------------------
# render_web_png — failure paths. Each raises the typed error; none hang.
# ---------------------------------------------------------------------
def test_render_web_png_no_chromium_binary_raises(monkeypatch):
    """Neither chromium nor chromium-browser on PATH -> WebRenderError."""
    monkeypatch.setattr(web_render.shutil, "which", lambda name: None)
    with pytest.raises(WebRenderError, match="no Chromium binary"):
        render_web_png("https://status.example.com", 1360, 768, 1360, 768)


def test_render_web_png_chromium_nonzero_exit_raises(monkeypatch):
    """A non-zero Chromium exit surfaces as a typed WebRenderError
    carrying the exit code."""
    _install_fake_chromium(
        monkeypatch, returncode=1, stderr=b"some chromium error\n",
        write_screenshot=False,
    )
    with pytest.raises(WebRenderError, match="Chromium exited 1"):
        render_web_png("https://status.example.com", 1360, 768, 1360, 768)


def test_render_web_png_timeout_raises_and_does_not_hang(monkeypatch):
    """A Chromium timeout surfaces as a typed WebRenderError —
    `subprocess.run` kills+reaps the child, so nothing is orphaned."""
    _install_fake_chromium(monkeypatch, raise_timeout=True)
    with pytest.raises(WebRenderError, match="timed out"):
        render_web_png("https://status.example.com", 1360, 768, 1360, 768)


def test_render_web_png_launch_error_raises(monkeypatch):
    """An OSError launching Chromium (e.g. binary vanished) surfaces as
    a typed WebRenderError."""
    _install_fake_chromium(
        monkeypatch, launch_error=OSError("exec format error")
    )
    with pytest.raises(WebRenderError, match="failed to launch Chromium"):
        render_web_png("https://status.example.com", 1360, 768, 1360, 768)


def test_render_web_png_missing_screenshot_raises(monkeypatch):
    """Chromium exits 0 but the screenshot file is gone -> WebRenderError."""
    _install_fake_chromium(monkeypatch, delete_screenshot=True)
    with pytest.raises(WebRenderError, match="no screenshot"):
        render_web_png("https://status.example.com", 1360, 768, 1360, 768)


def test_render_web_png_empty_screenshot_raises(monkeypatch):
    """Chromium exits 0 but leaves a zero-byte screenshot ->
    WebRenderError (no silent empty render)."""
    _install_fake_chromium(monkeypatch, write_screenshot=False)
    with pytest.raises(WebRenderError, match="empty screenshot"):
        render_web_png("https://status.example.com", 1360, 768, 1360, 768)


def test_render_web_png_non_png_screenshot_raises(monkeypatch):
    """A screenshot file that isn't a PNG (a crash artifact) fails the
    PNG-magic check with a typed WebRenderError."""
    _install_fake_chromium(
        monkeypatch, screenshot_png=b"GIF89a not a png"
    )
    with pytest.raises(WebRenderError, match="not a PNG"):
        render_web_png("https://status.example.com", 1360, 768, 1360, 768)


def test_render_web_png_cleans_up_temp_screenshot(monkeypatch):
    """The temp Chromium-output PNG is deleted after a successful
    render — no temp-file leak."""
    seen = {}

    monkeypatch.setattr(
        web_render.shutil, "which", lambda name: "/usr/bin/chromium"
    )

    def _fake_run(argv, capture_output=False, timeout=None):
        for arg in argv:
            if arg.startswith("--screenshot="):
                path = arg.split("=", 1)[1]
                seen["path"] = path
                with open(path, "wb") as fh:
                    fh.write(_solid_png(800, 600, (5, 5, 5)))
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(web_render.subprocess, "run", _fake_run)
    render_web_png("https://status.example.com", 800, 600, 1360, 768)

    from pathlib import Path
    assert "path" in seen
    assert not Path(seen["path"]).exists()


def test_render_web_png_cleans_up_temp_screenshot_on_failure(monkeypatch):
    """The temp Chromium-output PNG is deleted even when the render
    fails (Chromium exits non-zero)."""
    seen = {}

    monkeypatch.setattr(
        web_render.shutil, "which", lambda name: "/usr/bin/chromium"
    )

    def _fake_run(argv, capture_output=False, timeout=None):
        for arg in argv:
            if arg.startswith("--screenshot="):
                path = arg.split("=", 1)[1]
                seen["path"] = path
                with open(path, "wb") as fh:
                    fh.write(b"junk")
        return _FakeCompletedProcess(returncode=1, stderr=b"boom")

    monkeypatch.setattr(web_render.subprocess, "run", _fake_run)
    with pytest.raises(WebRenderError):
        render_web_png("https://status.example.com", 800, 600, 1360, 768)

    from pathlib import Path
    assert "path" in seen
    assert not Path(seen["path"]).exists()


def test_render_web_png_rejects_file_url_before_spawn(monkeypatch):
    """A `file://` URL is rejected by validate_web_url BEFORE Chromium
    is resolved or spawned."""
    spawned = {"ran": False}

    def _fail_run(*a, **k):
        spawned["ran"] = True
        raise AssertionError("Chromium must not be spawned for file://")

    monkeypatch.setattr(web_render.subprocess, "run", _fail_run)
    with pytest.raises(ValueError, match="scheme"):
        render_web_png("file:///etc/passwd", 1360, 768, 1360, 768)
    assert spawned["ran"] is False


def test_render_web_png_rejects_nonpositive_render_dims():
    """A zero/negative render dimension is a ValueError."""
    with pytest.raises(ValueError, match="render dimensions"):
        render_web_png("https://status.example.com", 0, 768, 1360, 768)


def test_render_web_png_rejects_nonpositive_panel_dims():
    """A zero/negative panel dimension is a ValueError."""
    with pytest.raises(ValueError, match="panel dimensions"):
        render_web_png("https://status.example.com", 1360, 768, 1360, -1)


# ---------------------------------------------------------------------
# main() — the subprocess entry point + its argv contract.
# ---------------------------------------------------------------------
def test_main_good_args_writes_panel_png(monkeypatch, tmp_path, capsys):
    """Good argv: the render runs and a panel-sized PNG lands at
    <out_path>, exit 0, nothing on stderr."""
    _install_fake_chromium(
        monkeypatch, screenshot_png=_solid_png(800, 600, (9, 9, 9))
    )
    out = tmp_path / "shot.png"
    rc = main([
        "https://status.example.com", "800", "600", "1360", "768", str(out),
    ])

    assert rc == 0
    data = out.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert _open_png(data).size == (1360, 768)
    assert capsys.readouterr().err == ""


def test_main_wrong_arg_count_exits_2(capsys):
    """Too few argv -> exit 2 + a usage line on stderr."""
    rc = main(["https://status.example.com", "1360", "768"])
    assert rc == 2
    assert "usage:" in capsys.readouterr().err


def test_main_non_integer_dimensions_exit_2(capsys):
    """A non-integer dimension -> exit 2 + a clear stderr message."""
    rc = main([
        "https://status.example.com", "wide", "768", "1360", "768",
        "/tmp/x.png",
    ])
    assert rc == 2
    assert "integers" in capsys.readouterr().err


def test_main_nonpositive_dimensions_exit_2(capsys):
    """A zero/negative dimension -> exit 2 + a clear stderr message."""
    rc = main([
        "https://status.example.com", "1360", "768", "1360", "-1",
        "/tmp/x.png",
    ])
    assert rc == 2
    assert "positive" in capsys.readouterr().err


def test_main_file_url_rejected_exit_2(monkeypatch, capsys):
    """A `file://` URL is rejected — exit 2 (bad input), a clear
    'invalid URL' message, and the render is never attempted."""
    def _fail_run(*a, **k):
        raise AssertionError("Chromium must not be spawned for file://")

    monkeypatch.setattr(web_render.subprocess, "run", _fail_run)
    rc = main([
        "file:///etc/passwd", "1360", "768", "1360", "768", "/tmp/x.png",
    ])
    assert rc == 2
    assert "invalid URL" in capsys.readouterr().err


def test_main_render_failure_exits_1(monkeypatch, tmp_path, capsys):
    """A render failure (Chromium non-zero exit) -> exit 1 + the reason
    on stderr; no PNG is written."""
    _install_fake_chromium(
        monkeypatch, returncode=1, stderr=b"chromium crashed\n",
        write_screenshot=False,
    )
    out = tmp_path / "shot.png"
    rc = main([
        "https://status.example.com", "1360", "768", "1360", "768", str(out),
    ])

    assert rc == 1
    assert "web-render:" in capsys.readouterr().err
    assert not out.exists()


def test_main_timeout_exits_1(monkeypatch, tmp_path, capsys):
    """A Chromium timeout -> exit 1 + a clear stderr message; no hang."""
    _install_fake_chromium(monkeypatch, raise_timeout=True)
    out = tmp_path / "shot.png"
    rc = main([
        "https://status.example.com", "1360", "768", "1360", "768", str(out),
    ])
    assert rc == 1
    assert "timed out" in capsys.readouterr().err


def test_main_unwritable_output_path_exits_1(monkeypatch, capsys):
    """A render that succeeds but can't write the PNG -> exit 1 + a
    clear stderr message."""
    _install_fake_chromium(monkeypatch)
    # A path whose parent directory does not exist -> OSError on open.
    rc = main([
        "https://status.example.com", "800", "600", "1360", "768",
        "/nonexistent-dir-xyz/shot.png",
    ])
    assert rc == 1
    assert "failed to write PNG" in capsys.readouterr().err
