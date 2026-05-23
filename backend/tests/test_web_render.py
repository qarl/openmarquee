"""Tests for the on-device web-slide renderer (Web slide C2).

`openmarquee.web_render` turns an operator-supplied URL into a
display-sized PNG entirely on the Pi: `chromium-headless-shell`'s
`--screenshot` rendered at the sign's live display resolution, returned
verbatim (no panel / letterbox compositing — the render IS the display
size).

The Chromium shell is a *subprocess* the module spawns (there is no
Python import for it). These tests mock `subprocess.Popen` — so the
whole suite runs on a host WITHOUT Chromium installed — and assert the
wiring (the flags, `--window-size`, `--screenshot`, the timeout +
kill/reap behavior, the typed error). `shutil.which` is also patched
so binary resolution is deterministic. The fake Chromium, like the
real one, emits a screenshot of exactly the `--window-size`.
"""

import os
import signal
import subprocess
import time
from io import BytesIO
from pathlib import Path

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
class _FakePopen:
    """Stand-in for `subprocess.Popen` that matches the surface
    `render_web_png` actually uses: `pid`, `returncode`, `communicate`,
    `wait`, `kill`. The fake child is assigned a stable pretend PID
    (used by `os.killpg` assertions in the tests).
    """

    _next_pid = 50000

    def __init__(
        self, returncode=0, stderr=b"", raise_timeout=False, calls=None
    ):
        self.pid = _FakePopen._next_pid
        _FakePopen._next_pid += 1
        self.returncode = returncode
        self._stderr = stderr
        self._raise_timeout = raise_timeout
        self._calls = calls
        self._waited = False
        self._communicate_called = False

    def communicate(self, timeout=None):
        if self._communicate_called:
            # Real Popen.communicate raises ValueError on a second
            # call (pipes already closed). The renderer's finally
            # block intentionally catches this — a drain attempt
            # against an already-drained Popen is a no-op.
            raise ValueError("communicate() already called")
        # Only record the first communicate timeout — that's the
        # "render" budget, distinct from the small post-sweep drain
        # value the finally block passes.
        if self._calls is not None:
            self._calls["timeout"] = timeout
        self._communicate_called = True
        if self._raise_timeout:
            # Real Popen.communicate keeps the child alive on timeout
            # so the caller can clean up; mirror that contract — pid
            # remains a valid target for killpg.
            raise subprocess.TimeoutExpired(cmd="chromium", timeout=timeout)
        return (b"", self._stderr)

    def wait(self, timeout=None):
        self._waited = True
        return self.returncode

    def kill(self):  # pragma: no cover — defensive escalation path
        self.returncode = -9


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
    """Patch `shutil.which` + `subprocess.Popen` + `os.killpg` so
    `render_web_png` runs against a fake Chromium.

    `which` is what `shutil.which` returns (None -> no binary found).
    The fake `subprocess.Popen` records its argv/kwargs into `calls`
    and writes a screenshot to the `--screenshot=` path BEFORE Popen
    returns (so `communicate()` sees the file already on disk —
    mirroring how Chromium has written the PNG by the time it exits).
    When `screenshot_png` is None it synthesizes a solid PNG of exactly
    the requested `--window-size` — mimicking real Chromium, which
    emits a capture of the window size. A test that needs a specific
    (bad / non-PNG / odd) payload passes `screenshot_png` explicitly.

    `os.killpg` is patched to record (pgid, sig) pairs into
    `calls["killpg"]` — the regression assertion that the SIGTERM →
    grace → SIGKILL sweep actually runs against the process group.
    """
    monkeypatch.setattr(
        web_render.shutil, "which",
        lambda name: which if name in web_render._CHROMIUM_BINARIES else None,
    )

    # Capture the helper kwargs into closure locals so the Popen
    # signature's own `stderr` kwarg (the IO redirection setting) doesn't
    # shadow the test-fixture `stderr` payload.
    captured_stderr_payload = stderr
    captured_raise_timeout = raise_timeout
    captured_returncode = returncode

    def _fake_popen(argv, stdout=None, stderr=None, start_new_session=False):
        if calls is not None:
            calls["argv"] = argv
            calls["start_new_session"] = start_new_session
            calls["stdout"] = stdout
            calls["stderr"] = stderr
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
                    os.unlink(path)
                elif write_screenshot:
                    with open(path, "wb") as fh:
                        fh.write(payload)
        if launch_error is not None:
            raise launch_error
        return _FakePopen(
            returncode=captured_returncode,
            stderr=captured_stderr_payload,
            raise_timeout=captured_raise_timeout,
            calls=calls,
        )

    monkeypatch.setattr(web_render.subprocess, "Popen", _fake_popen)

    # Default-stub os.killpg into a no-op recorder. The renderer
    # finally-block invokes it on the fake's PID, which doesn't exist
    # — without this stub, the real killpg would ProcessLookupError
    # (which the renderer handles), but recording the calls lets tests
    # assert the SIGTERM→SIGKILL sweep pattern. Tests that need to
    # simulate "group still alive" can patch `os.killpg` over this.
    killpg_calls: list[tuple[int, int]] = []
    if calls is not None:
        calls["killpg"] = killpg_calls

    def _fake_killpg(pgid, sig):
        killpg_calls.append((pgid, sig))
        # Mimic clean-exit: signal 0 (existence probe) after SIGTERM
        # raises ProcessLookupError so the grace-loop exits immediately
        # rather than spinning the full 2-second window in tests.
        if sig == 0:
            raise ProcessLookupError(f"no such process group {pgid}")

    monkeypatch.setattr(web_render.os, "killpg", _fake_killpg)


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
    assert (
        f"--virtual-time-budget={WEB_RENDER_VIRTUAL_TIME_BUDGET_MS}" in argv
    )


def test_build_chromium_argv_omits_headless_for_headless_shell():
    """chromium-headless-shell is inherently headless — it must NOT be
    given `--headless`; the full `chromium` browser still gets it."""
    shell = _build_chromium_argv(
        "/usr/bin/chromium-headless-shell", "https://x.example.com",
        1024, 600, "/tmp/s.png",
    )
    assert "--headless" not in shell
    assert shell[0] == "/usr/bin/chromium-headless-shell"
    # ...but the screenshot/window/url wiring is identical.
    assert "--window-size=1024,600" in shell
    assert "--screenshot=/tmp/s.png" in shell
    assert shell[-1] == "https://x.example.com"

    full = _build_chromium_argv(
        "/usr/bin/chromium", "https://x.example.com", 1024, 600, "/tmp/s.png",
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
        web_render.shutil, "which",
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
        web_render.shutil, "which",
        lambda name: {
            "chromium": "/usr/bin/chromium",
            "nice": "/usr/bin/nice",
        }.get(name),
    )

    def _fake_popen(argv, stdout=None, stderr=None, start_new_session=False):
        calls["argv"] = argv
        for arg in argv:
            if arg.startswith("--screenshot="):
                with open(arg.split("=", 1)[1], "wb") as fh:
                    fh.write(_solid_png(1360, 768, (1, 2, 3)))
        return _FakePopen(returncode=0)

    monkeypatch.setattr(web_render.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(
        web_render.os, "killpg",
        lambda pgid, sig: (_ for _ in ()).throw(ProcessLookupError()),
    )
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
        web_render.shutil, "which",
        lambda name: "/usr/bin/chromium-browser"
        if name == "chromium-browser" else None,
    )

    def _fake_popen(argv, stdout=None, stderr=None, start_new_session=False):
        calls["argv"] = argv
        for arg in argv:
            if arg.startswith("--screenshot="):
                with open(arg.split("=", 1)[1], "wb") as fh:
                    fh.write(_solid_png(800, 600, (1, 2, 3)))
        return _FakePopen(returncode=0)

    monkeypatch.setattr(web_render.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(
        web_render.os, "killpg",
        lambda pgid, sig: (_ for _ in ()).throw(ProcessLookupError()),
    )
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
        monkeypatch, returncode=1, stderr=b"some chromium error\n",
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
    _install_fake_chromium(
        monkeypatch, launch_error=OSError("exec format error")
    )
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
    _install_fake_chromium(
        monkeypatch, screenshot_png=b"GIF89a not a png"
    )
    with pytest.raises(WebRenderError, match="not a PNG"):
        render_web_png("https://status.example.com", 1360, 768)


def test_render_web_png_cleans_up_temp_screenshot(monkeypatch):
    """The temp Chromium-output PNG is deleted after a successful
    render — no temp-file leak."""
    seen = {}

    monkeypatch.setattr(
        web_render.shutil, "which", lambda name: "/usr/bin/chromium"
    )

    def _fake_popen(argv, stdout=None, stderr=None, start_new_session=False):
        for arg in argv:
            if arg.startswith("--screenshot="):
                path = arg.split("=", 1)[1]
                seen["path"] = path
                with open(path, "wb") as fh:
                    fh.write(_solid_png(800, 600, (5, 5, 5)))
        return _FakePopen(returncode=0)

    monkeypatch.setattr(web_render.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(
        web_render.os, "killpg",
        lambda pgid, sig: (_ for _ in ()).throw(ProcessLookupError()),
    )
    render_web_png("https://status.example.com", 800, 600)

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

    def _fake_popen(argv, stdout=None, stderr=None, start_new_session=False):
        for arg in argv:
            if arg.startswith("--screenshot="):
                path = arg.split("=", 1)[1]
                seen["path"] = path
                with open(path, "wb") as fh:
                    fh.write(b"junk")
        return _FakePopen(returncode=1, stderr=b"boom")

    monkeypatch.setattr(web_render.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(
        web_render.os, "killpg",
        lambda pgid, sig: (_ for _ in ()).throw(ProcessLookupError()),
    )
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
    def _fail_popen(*a, **k):
        raise AssertionError("Chromium must not be spawned for file://")

    monkeypatch.setattr(web_render.subprocess, "Popen", _fail_popen)
    rc = main(["file:///etc/passwd", "1360", "768", "/tmp/x.png"])
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
    rc = main([
        "https://status.example.com", "800", "600",
        "/nonexistent-dir-xyz/shot.png",
    ])
    assert rc == 1
    assert "failed to write PNG" in capsys.readouterr().err


# ---------------------------------------------------------------------
# Chromium process-group sweep (QA 2026-05-23 leak fix).
#
# FYS production after 78 min backend uptime held 8 alive (not zombie)
# chromium-headless-shell processes from a single spawn cycle — each
# ~75 MB resident in swap, leading to kswapd0 thrash + dashboard
# slowness. Pkill of the leftover procs dropped swap 311 → 213 MB and
# 1-min load 8.37 → 6.06 within 30s. Confirms the dominant footprint
# is leaked helpers, not the active backend.
#
# Root cause: subprocess.run reaps only the immediate child. Chromium
# under --no-sandbox + Debian's /bin/sh wrapper forks renderer /
# utility / GPU helpers; when the browser-parent exits cleanly those
# helpers can detach and persist. The fix puts the spawn in its own
# session via `start_new_session=True` and tears the WHOLE process
# group down (SIGTERM → 2s grace → SIGKILL) in the finally block.
# ---------------------------------------------------------------------
def test_render_web_png_spawns_in_new_session_for_group_kill(monkeypatch):
    """`start_new_session=True` is on the Popen call — the spawn gets
    its own process group (PGID == proc.pid) so the finally-block sweep
    in _terminate_process_group can kill every descendant in one shot.
    Regression: the leak existed because subprocess.run had no
    equivalent option."""
    calls = {}
    _install_fake_chromium(monkeypatch, calls=calls)
    render_web_png("https://status.example.com", 1360, 768)
    assert calls["start_new_session"] is True


def test_render_web_png_sweeps_process_group_on_clean_exit(monkeypatch):
    """The finally block kills the chromium process group EVEN ON THE
    CLEAN-EXIT PATH. The common case: chromium exited 0, but renderer/
    utility helpers may have detached and persisted. SIGTERM is
    unconditional; ProcessLookupError on an already-empty group is the
    healthy outcome (caught silently)."""
    calls = {}
    _install_fake_chromium(monkeypatch, calls=calls)
    render_web_png("https://status.example.com", 1360, 768)
    sigs = [sig for _pgid, sig in calls["killpg"]]
    assert signal.SIGTERM in sigs


def test_render_web_png_sweeps_process_group_on_timeout(monkeypatch):
    """The finally block ALSO runs on the TimeoutExpired path — the
    typed WebRenderError is raised AFTER the sweep so the descendants
    are torn down before the call returns. Without the sweep, a timed-
    out chromium would leave its renderer/utility helpers behind."""
    calls = {}
    _install_fake_chromium(monkeypatch, raise_timeout=True, calls=calls)
    with pytest.raises(WebRenderError, match="timed out"):
        render_web_png("https://status.example.com", 1360, 768)
    sigs = [sig for _pgid, sig in calls["killpg"]]
    assert signal.SIGTERM in sigs


def test_render_web_png_escalates_to_sigkill_when_group_survives(
    monkeypatch,
):
    """When the process group survives SIGTERM (the failure mode this
    fix addresses on FYS), the sweep escalates to SIGKILL after the
    grace period. Simulated by stubbing killpg to NEVER raise
    ProcessLookupError — the existence-probe loop runs the full grace
    window, then SIGKILL fires."""
    seen = []

    def _stuck_killpg(pgid, sig):
        # Record every call. signal 0 = existence probe; return success
        # (don't raise) so the renderer thinks the group is still alive
        # and the grace loop spins to deadline.
        seen.append((pgid, sig))

    # Speed the grace window so this test is sub-second.
    monkeypatch.setattr(
        web_render, "_PROCESS_GROUP_TERM_GRACE_S", 0.1
    )
    _install_fake_chromium(monkeypatch)
    monkeypatch.setattr(web_render.os, "killpg", _stuck_killpg)

    render_web_png("https://status.example.com", 1360, 768)

    signals_sent = [sig for _, sig in seen]
    assert signal.SIGTERM in signals_sent
    assert signal.SIGKILL in signals_sent
    # SIGKILL fired AFTER SIGTERM (the order matters — never reverse).
    assert signals_sent.index(signal.SIGTERM) < signals_sent.index(
        signal.SIGKILL
    )


def test_render_web_png_passes_overall_timeout_via_communicate(
    monkeypatch,
):
    """The Chromium wall-clock budget is passed to Popen.communicate
    so the render never hangs unboundedly — restored after the
    subprocess.run → Popen migration."""
    calls = {}
    _install_fake_chromium(monkeypatch, calls=calls)
    render_web_png("https://status.example.com", 1360, 768)
    assert calls["timeout"] == WEB_RENDER_TIMEOUT_S


# ---------------------------------------------------------------------
# Live-fire smoke (auto-skipped when chromium-headless-shell isn't on
# PATH — runs on the Pi + on CI Linux, skips on macOS dev). Walks /proc
# for chromium-headless-shell descendants of the test process before
# and after a real render; asserts the count returns to baseline within
# the grace window. This is the regression that fences the actual leak
# QA observed on FYS — the unit tests above prove the SHAPE; this
# proves the BEHAVIOR.
# ---------------------------------------------------------------------
def _count_chromium_procs() -> int:
    """Walk /proc for procs whose comm contains 'chromium-headless'.

    Linux-only (skipped when /proc isn't a directory). No psutil
    dependency — the backend doesn't import it and we won't add a
    runtime dep for one test."""
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return 0
    count = 0
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text().strip()
        except (OSError, PermissionError):
            continue
        if "chromium-headless" in comm or "headless-shell" in comm:
            count += 1
    return count


@pytest.mark.skipif(
    __import__("shutil").which("chromium-headless-shell") is None,
    reason="chromium-headless-shell not on PATH "
    "(auto-skipped on macOS dev; runs on Pi + CI Linux)",
)
def test_render_web_png_leaves_no_chromium_helpers_alive(tmp_path):
    """Live-fire regression for QA's 2026-05-23 leak: count chromium-
    headless-shell procs before render, run a real render, poll for
    the count to return to baseline within the grace window, assert
    delta == 0.

    Pre-fix: the count would stay 7-8 above baseline (one wrapper sh
    + browser parent + ~6 renderer/utility helpers, all alive in
    swap). Post-fix: the finally-block killpg sweep returns the count
    to baseline within _PROCESS_GROUP_TERM_GRACE_S + a small reap
    margin (each-process exit isn't instantaneous after SIGKILL)."""
    baseline = _count_chromium_procs()
    # example.com is the safest possible live URL: tiny, stable, and
    # IANA-managed for exactly this kind of test. Network-dependent
    # but the live-fire gate already implies the host has network.
    render_web_png("https://example.com", 640, 480)

    # Poll up to ~3s for procs to exit after SIGTERM/SIGKILL (each
    # process honoring the signal takes a few hundred ms on the Pi).
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if _count_chromium_procs() <= baseline:
            return
        time.sleep(0.1)

    final = _count_chromium_procs()
    pytest.fail(
        f"chromium-headless procs leaked: baseline={baseline} "
        f"final={final} delta={final - baseline}"
    )
