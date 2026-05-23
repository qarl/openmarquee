"""Tests for the on-device web-slide renderer (Web slide C2).

`openmarquee.web_render` turns an operator-supplied URL into a
display-sized PNG entirely on the Pi: `chromium-headless-shell`'s
`--screenshot` rendered at the sign's live display resolution, returned
verbatim (no panel / letterbox compositing — the render IS the display
size).

The Chromium shell is a *subprocess* the module spawns (there is no
Python import for it). These tests mock `subprocess.run` — so the whole
suite runs on a host WITHOUT Chromium installed — and assert the wiring
(the flags, `--window-size`, `--screenshot`, the timeout + kill/reap
behavior, the typed error). `shutil.which` is also patched so binary
resolution is deterministic. The fake Chromium, like the real one,
emits a screenshot of exactly the `--window-size`.
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
    _nice_prefix,
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
    The fake `subprocess.run` records its argv/kwargs into `calls` and
    writes a screenshot to the `--screenshot=` path. When `screenshot_png`
    is None it synthesizes a solid PNG of exactly the requested
    `--window-size` — mimicking real Chromium, which emits a capture of
    the window size. A test that needs a specific (bad / non-PNG / odd)
    payload passes `screenshot_png` explicitly.
    """
    monkeypatch.setattr(
        web_render.shutil,
        "which",
        lambda name: which if name in web_render._CHROMIUM_BINARIES else None,
    )

    def _fake_run(argv, capture_output=False, timeout=None):
        if calls is not None:
            calls["argv"] = argv
            calls["timeout"] = timeout
            calls["capture_output"] = capture_output
        # Mirror real Chromium: the capture is the --window-size. When
        # the test pinned an explicit screenshot_png, use that instead.
        win = (1360, 768)
        for arg in argv:
            if arg.startswith("--window-size="):
                w, h = arg.split("=", 1)[1].split(",")
                win = (int(w), int(h))
        payload = (
            screenshot_png
            if screenshot_png is not None
            else _solid_png(win[0], win[1], (10, 120, 200))
        )
        for arg in argv:
            if arg.startswith("--screenshot="):
                path = arg.split("=", 1)[1]
                if delete_screenshot:
                    import os

                    os.unlink(path)
                elif write_screenshot:
                    with open(path, "wb") as fh:
                        fh.write(payload)
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
    """`--window-size` carries the display size; `--screenshot` carries
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
    assert f"--virtual-time-budget={WEB_RENDER_VIRTUAL_TIME_BUDGET_MS}" in argv


def test_build_chromium_argv_omits_headless_for_headless_shell():
    """chromium-headless-shell is inherently headless — it must NOT be
    given `--headless`; the full `chromium` browser still gets it."""
    shell = _build_chromium_argv(
        "/usr/bin/chromium-headless-shell",
        "https://x.example.com",
        1024,
        600,
        "/tmp/s.png",
    )
    assert "--headless" not in shell
    assert shell[0] == "/usr/bin/chromium-headless-shell"
    # ...but the screenshot/window/url wiring is identical.
    assert "--window-size=1024,600" in shell
    assert "--screenshot=/tmp/s.png" in shell
    assert shell[-1] == "https://x.example.com"

    full = _build_chromium_argv(
        "/usr/bin/chromium",
        "https://x.example.com",
        1024,
        600,
        "/tmp/s.png",
    )
    assert "--headless" in full


def test_headless_shell_is_the_preferred_binary():
    """The slim headless shell is resolved before the full browser —
    it is the lighter footprint that fits alongside the renderer."""
    assert web_render._CHROMIUM_BINARIES[0] == "chromium-headless-shell"


# ---------------------------------------------------------------------
# _nice_prefix — the CPU de-prioritization prefix.
# ---------------------------------------------------------------------
def test_nice_prefix_uses_nice(monkeypatch):
    """When `nice` resolves, the prefix drops Chromium to the lowest
    CPU priority (nice -n 19). No ionice — the idle I/O class starves
    the render under the renderer's I/O contention."""
    monkeypatch.setattr(
        web_render.shutil,
        "which",
        lambda name: "/usr/bin/nice" if name == "nice" else None,
    )
    assert _nice_prefix() == ["/usr/bin/nice", "-n", "19"]


def test_nice_prefix_empty_when_nice_missing(monkeypatch):
    """The prefix is best-effort — a host without `nice` yields an
    empty prefix and the render still runs, bare."""
    monkeypatch.setattr(web_render.shutil, "which", lambda name: None)
    assert _nice_prefix() == []


def test_render_web_png_prepends_nice_prefix_when_available(monkeypatch):
    """render_web_png prepends the nice prefix to the spawned argv so
    the transient render yields CPU to the playback renderer."""
    calls = {}

    monkeypatch.setattr(
        web_render.shutil,
        "which",
        lambda name: {
            "chromium": "/usr/bin/chromium",
            "nice": "/usr/bin/nice",
        }.get(name),
    )

    def _fake_run(argv, capture_output=False, timeout=None):
        calls["argv"] = argv
        for arg in argv:
            if arg.startswith("--screenshot="):
                with open(arg.split("=", 1)[1], "wb") as fh:
                    fh.write(_solid_png(1360, 768, (1, 2, 3)))
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(web_render.subprocess, "run", _fake_run)
    render_web_png("https://status.example.com", 1360, 768)

    argv = calls["argv"]
    assert argv[:3] == ["/usr/bin/nice", "-n", "19"]
    assert argv[3] == "/usr/bin/chromium"
    assert argv[-1] == "https://status.example.com"


# ---------------------------------------------------------------------
# render_web_png — the success path + its wiring.
# ---------------------------------------------------------------------
def test_render_web_png_returns_display_sized_png(monkeypatch):
    """A normal render returns real PNG bytes sized to the display
    resolution it was asked for — the render IS that size, no bars."""
    _install_fake_chromium(monkeypatch)
    png = render_web_png("https://status.example.com", 1360, 768)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert _open_png(png).size == (1360, 768)


def test_render_web_png_renders_at_portrait_resolution(monkeypatch):
    """A portrait display resolution renders portrait — the render
    follows whatever resolution the (rotation-aware) caller passes."""
    _install_fake_chromium(monkeypatch)
    png = render_web_png("https://status.example.com", 768, 1360)
    assert _open_png(png).size == (768, 1360)


def test_render_web_png_spawns_chromium_with_window_size(monkeypatch):
    """Chromium is spawned with `--window-size=<width>,<height>` (the
    display size), `--screenshot`, and the headless flags."""
    calls = {}
    _install_fake_chromium(monkeypatch, calls=calls)
    render_web_png("https://status.example.com", 1024, 600)

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
    render_web_png("https://status.example.com", 1360, 768)
    assert calls["timeout"] == WEB_RENDER_TIMEOUT_S


def test_render_web_png_resolves_chromium_browser_fallback(monkeypatch):
    """If `chromium` is absent but `chromium-browser` is present, the
    fallback name is used."""
    calls = {}
    monkeypatch.setattr(
        web_render.shutil,
        "which",
        lambda name: "/usr/bin/chromium-browser" if name == "chromium-browser" else None,
    )

    def _fake_run(argv, capture_output=False, timeout=None):
        calls["argv"] = argv
        for arg in argv:
            if arg.startswith("--screenshot="):
                with open(arg.split("=", 1)[1], "wb") as fh:
                    fh.write(_solid_png(800, 600, (1, 2, 3)))
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(web_render.subprocess, "run", _fake_run)
    render_web_png("https://status.example.com", 800, 600)
    assert calls["argv"][0] == "/usr/bin/chromium-browser"


# ---------------------------------------------------------------------
# render_web_png — failure paths. Each raises the typed error; none hang.
# ---------------------------------------------------------------------
def test_render_web_png_no_chromium_binary_raises(monkeypatch):
    """Neither chromium nor chromium-browser on PATH -> WebRenderError."""
    monkeypatch.setattr(web_render.shutil, "which", lambda name: None)
    with pytest.raises(WebRenderError, match="no Chromium binary"):
        render_web_png("https://status.example.com", 1360, 768)


def test_render_web_png_chromium_nonzero_exit_raises(monkeypatch):
    """A non-zero Chromium exit surfaces as a typed WebRenderError
    carrying the exit code."""
    _install_fake_chromium(
        monkeypatch,
        returncode=1,
        stderr=b"some chromium error\n",
        write_screenshot=False,
    )
    with pytest.raises(WebRenderError, match="Chromium exited 1"):
        render_web_png("https://status.example.com", 1360, 768)


def test_render_web_png_timeout_raises_and_does_not_hang(monkeypatch):
    """A Chromium timeout surfaces as a typed WebRenderError —
    `subprocess.run` kills+reaps the child, so nothing is orphaned."""
    _install_fake_chromium(monkeypatch, raise_timeout=True)
    with pytest.raises(WebRenderError, match="timed out"):
        render_web_png("https://status.example.com", 1360, 768)


def test_render_web_png_launch_error_raises(monkeypatch):
    """An OSError launching Chromium (e.g. binary vanished) surfaces as
    a typed WebRenderError."""
    _install_fake_chromium(monkeypatch, launch_error=OSError("exec format error"))
    with pytest.raises(WebRenderError, match="failed to launch Chromium"):
        render_web_png("https://status.example.com", 1360, 768)


def test_render_web_png_missing_screenshot_raises(monkeypatch):
    """Chromium exits 0 but the screenshot file is gone -> WebRenderError."""
    _install_fake_chromium(monkeypatch, delete_screenshot=True)
    with pytest.raises(WebRenderError, match="no screenshot"):
        render_web_png("https://status.example.com", 1360, 768)


def test_render_web_png_empty_screenshot_raises(monkeypatch):
    """Chromium exits 0 but leaves a zero-byte screenshot ->
    WebRenderError (no silent empty render)."""
    _install_fake_chromium(monkeypatch, write_screenshot=False)
    with pytest.raises(WebRenderError, match="empty screenshot"):
        render_web_png("https://status.example.com", 1360, 768)


def test_render_web_png_non_png_screenshot_raises(monkeypatch):
    """A screenshot file that isn't a PNG (a crash artifact) fails the
    PNG-magic check with a typed WebRenderError."""
    _install_fake_chromium(monkeypatch, screenshot_png=b"GIF89a not a png")
    with pytest.raises(WebRenderError, match="not a PNG"):
        render_web_png("https://status.example.com", 1360, 768)


def test_render_web_png_cleans_up_temp_screenshot(monkeypatch):
    """The temp Chromium-output PNG is deleted after a successful
    render — no temp-file leak."""
    seen = {}

    monkeypatch.setattr(web_render.shutil, "which", lambda name: "/usr/bin/chromium")

    def _fake_run(argv, capture_output=False, timeout=None):
        for arg in argv:
            if arg.startswith("--screenshot="):
                path = arg.split("=", 1)[1]
                seen["path"] = path
                with open(path, "wb") as fh:
                    fh.write(_solid_png(800, 600, (5, 5, 5)))
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(web_render.subprocess, "run", _fake_run)
    render_web_png("https://status.example.com", 800, 600)

    from pathlib import Path

    assert "path" in seen
    assert not Path(seen["path"]).exists()


def test_render_web_png_cleans_up_temp_screenshot_on_failure(monkeypatch):
    """The temp Chromium-output PNG is deleted even when the render
    fails (Chromium exits non-zero)."""
    seen = {}

    monkeypatch.setattr(web_render.shutil, "which", lambda name: "/usr/bin/chromium")

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
        render_web_png("https://status.example.com", 800, 600)

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
        render_web_png("file:///etc/passwd", 1360, 768)
    assert spawned["ran"] is False


def test_render_web_png_rejects_nonpositive_dims():
    """A zero/negative render dimension is a ValueError."""
    with pytest.raises(ValueError, match="render dimensions"):
        render_web_png("https://status.example.com", 0, 768)
    with pytest.raises(ValueError, match="render dimensions"):
        render_web_png("https://status.example.com", 1360, -1)


# ---------------------------------------------------------------------
# main() — the subprocess entry point + its argv contract.
# ---------------------------------------------------------------------
def test_main_good_args_writes_png(monkeypatch, tmp_path, capsys):
    """Good argv: the render runs and a display-sized PNG lands at
    <out_path>, exit 0, nothing on stderr."""
    _install_fake_chromium(monkeypatch)
    out = tmp_path / "shot.png"
    rc = main(["https://status.example.com", "1360", "768", str(out)])

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
    rc = main(["https://status.example.com", "wide", "768", "/tmp/x.png"])
    assert rc == 2
    assert "integers" in capsys.readouterr().err


def test_main_nonpositive_dimensions_exit_2(capsys):
    """A zero/negative dimension -> exit 2 + a clear stderr message."""
    rc = main(["https://status.example.com", "1360", "-1", "/tmp/x.png"])
    assert rc == 2
    assert "positive" in capsys.readouterr().err


def test_main_file_url_rejected_exit_2(monkeypatch, capsys):
    """A `file://` URL is rejected — exit 2 (bad input), a clear
    'invalid URL' message, and the render is never attempted."""

    def _fail_run(*a, **k):
        raise AssertionError("Chromium must not be spawned for file://")

    monkeypatch.setattr(web_render.subprocess, "run", _fail_run)
    rc = main(["file:///etc/passwd", "1360", "768", "/tmp/x.png"])
    assert rc == 2
    assert "invalid URL" in capsys.readouterr().err


def test_main_render_failure_exits_1(monkeypatch, tmp_path, capsys):
    """A render failure (Chromium non-zero exit) -> exit 1 + the reason
    on stderr; no PNG is written."""
    _install_fake_chromium(
        monkeypatch,
        returncode=1,
        stderr=b"chromium crashed\n",
        write_screenshot=False,
    )
    out = tmp_path / "shot.png"
    rc = main(["https://status.example.com", "1360", "768", str(out)])

    assert rc == 1
    assert "web-render:" in capsys.readouterr().err
    assert not out.exists()


def test_main_timeout_exits_1(monkeypatch, tmp_path, capsys):
    """A Chromium timeout -> exit 1 + a clear stderr message; no hang."""
    _install_fake_chromium(monkeypatch, raise_timeout=True)
    out = tmp_path / "shot.png"
    rc = main(["https://status.example.com", "1360", "768", str(out)])
    assert rc == 1
    assert "timed out" in capsys.readouterr().err


def test_main_unwritable_output_path_exits_1(monkeypatch, capsys):
    """A render that succeeds but can't write the PNG -> exit 1 + a
    clear stderr message."""
    _install_fake_chromium(monkeypatch)
    # A path whose parent directory does not exist -> OSError on open.
    rc = main(
        [
            "https://status.example.com",
            "800",
            "600",
            "/nonexistent-dir-xyz/shot.png",
        ]
    )
    assert rc == 1
    assert "failed to write PNG" in capsys.readouterr().err
