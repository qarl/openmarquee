"""Tests for the Phase 7 slice 1 RustRenderer IPC proxy.

Unit tests use a Python-impersonator subprocess (`fake_sidecar.py`) that
implements the same stdin/stdout JSON-line protocol as the real Rust
sidecar. This exercises the actual pipe + serde paths (Popen, readline,
flush, broken-pipe handling, stderr drainer) without depending on the
Rust binary being built.

Real-subprocess end-to-end tests are skipped unless `OPENMARQUEE_RUST_BINARY`
points to a runnable sidecar binary (on the dev Pi: `/usr/local/bin/
openmarquee-render` or wherever `deploy.sh` installs it; on Mac: skipped).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from openmarquee.rendering import Renderer
from openmarquee.rendering.rust_renderer import (
    CaptureResult,
    HealthState,
    Idle,
    OpenResult,
    PaintSlide,
    PaintTransition,
    RustRenderer,
    RustRendererError,
    RustRendererOpError,
    RustRendererProtocolError,
    RustRendererRespawnedError,
    RustRendererSubprocessError,
    RustRendererUnsupportedSlideError,
    SlideComplete,
)


# ============================================================
# Fake sidecar subprocess script.
#
# A Python script that runs as a real subprocess and impersonates
# the Rust IPC sidecar's wire protocol. Reads JSON-line requests
# from stdin, emits canned JSON-line responses to stdout. The
# response shape is controlled via env vars + a small "script"
# embedded in the request stream (see SCRIPT_HEADER).
# ============================================================

FAKE_SIDECAR_SOURCE = r'''
import json
import os
import sys

# Optional: dump every request to a path so tests can assert
# exactly what the proxy sent on the wire.
log_path = os.environ.get("FAKE_SIDECAR_REQUEST_LOG")
log = open(log_path, "w") if log_path else None

# Optional: dump our own inherited TZ env to a path, so tests can
# assert the proxy threaded settings.timezone into the subprocess
# env (Bug 1 follow-up). Written once at startup.
tz_probe = os.environ.get("FAKE_SIDECAR_TZ_PROBE")
if tz_probe:
    with open(tz_probe, "w") as _f:
        _f.write(os.environ.get("TZ", "<unset>"))

# Optional: exit with this returncode after the next request
# (simulates subprocess death mid-session).
DIE_AFTER_OP = os.environ.get("FAKE_SIDECAR_DIE_AFTER_OP")
DIE_RC = int(os.environ.get("FAKE_SIDECAR_DIE_RC", "1"))

# Optional: respond to all reconfigure ops with an err.
RECONFIGURE_ERR = os.environ.get("FAKE_SIDECAR_RECONFIGURE_ERR")

# Optional: emit some stderr lines on startup (tests the drainer).
STDERR_BURST = int(os.environ.get("FAKE_SIDECAR_STDERR_BURST", "0"))

# Optional: emit malformed JSON for the next response.
MALFORMED_NEXT = os.environ.get("FAKE_SIDECAR_MALFORMED_NEXT")

# Optional: mode_w / mode_h returned on open.
MODE_W = int(os.environ.get("FAKE_SIDECAR_MODE_W", "1920"))
MODE_H = int(os.environ.get("FAKE_SIDECAR_MODE_H", "1080"))

# STREAM/VLC slice 2.5: the inherited binary frame channel. The
# real sidecar reads this on begin_external_frames; we mirror that
# so render_frame() / end_external_frames() are testable on the
# actual pipe path.
_frame_fd = os.environ.get("OPENMARQUEE_FRAME_FD")
frame_reader = os.fdopen(int(_frame_fd), "rb") if _frame_fd else None

for i in range(STDERR_BURST):
    print(f"fake_sidecar stderr line {i}", file=sys.stderr, flush=True)

def send(obj):
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()

def ok(result):
    send({"ok": {"result": result}})

def err(msg):
    send({"err": {"error": msg}})

malformed_emitted = False
for raw in sys.stdin:
    raw = raw.rstrip("\n")
    if not raw:
        continue
    if log:
        log.write(raw + "\n")
        log.flush()
    try:
        req = json.loads(raw)
    except Exception:
        err(f"fake_sidecar: bad request: {raw!r}")
        continue
    op = req.get("op")
    params = req.get("params") or {}

    if MALFORMED_NEXT and not malformed_emitted:
        sys.stdout.write("this is not json\n")
        sys.stdout.flush()
        malformed_emitted = True
        continue

    if op == "open":
        ok({"command": "open_ok", "mode_w": MODE_W, "mode_h": MODE_H})
    elif op == "begin_slide":
        ok({"command": "empty"})
    elif op == "advance":
        # Return PaintSlide echoing t_ms back as t_in_slide_ms.
        ok({
            "command": "paint_slide",
            "slide_id": "01010101-0101-0101-0101-010101010101",
            "t_in_slide_ms": int(params.get("t_ms", 0)),
        })
    elif op == "begin_transition":
        ok({"command": "empty"})
    elif op == "begin_external_frames":
        # Mirror the real sidecar: ack immediately (begin_external_
        # frames is a normal request/response op), THEN enter
        # pump-mode — read length-prefixed frames off the binary
        # channel until the 0-length sentinel. No second response.
        if log:
            # pixel_format has #[serde(default)] in the real sidecar —
            # an omitted format means rgb888; mirror that here.
            log.write(
                f"begin_external_frames:{params.get('width')}x"
                f"{params.get('height')}"
                f":{params.get('pixel_format', 'rgb888')}\n"
            )
            log.flush()
        ok({"command": "empty"})
        if frame_reader is not None:
            while True:
                hdr = frame_reader.read(4)
                if len(hdr) < 4:
                    break  # channel closed
                flen = int.from_bytes(hdr, "big")
                if flen == 0:
                    break  # end sentinel
                payload = frame_reader.read(flen)
                if log:
                    log.write(f"external_frame:{len(payload)}\n")
                    log.flush()
    elif op == "capture":
        ok({"command": "capture_ok", "path": params.get("path", ""), "bytes": 184})
    elif op == "reconfigure":
        if RECONFIGURE_ERR:
            err(RECONFIGURE_ERR)
        else:
            ok({"command": "empty"})
    elif op == "close":
        ok({"command": "empty"})
        if log:
            log.close()
        sys.exit(0)
    else:
        err(f"fake_sidecar: unknown op {op!r}")

    if DIE_AFTER_OP and op == DIE_AFTER_OP:
        if log:
            log.close()
        sys.exit(DIE_RC)

if log:
    log.close()
'''


@pytest.fixture
def fake_sidecar(tmp_path: Path):
    """Builds a Path to a Python script that impersonates the Rust IPC
    sidecar. Tests instantiate RustRenderer with binary_path=fake_sidecar
    and extra_args=[]; the script reads stdin / writes stdout in the
    same JSON-line shape as the Rust binary."""
    script = tmp_path / "fake_sidecar.py"
    script.write_text(FAKE_SIDECAR_SOURCE)
    return script


@pytest.fixture
def make_renderer(fake_sidecar, monkeypatch):
    """Factory: build a RustRenderer wired to the fake sidecar.

    The fake script ignores `--ipc-sidecar`; we prepend the python
    interpreter as binary_path and pass the script as extra_args.
    Env-var overlays are set via monkeypatch so they're auto-restored
    after the test (no cross-test leakage)."""

    def _make(env_extra: dict[str, str] | None = None, **kwargs):
        for k, v in (env_extra or {}).items():
            monkeypatch.setenv(k, v)
        return RustRenderer(
            width=1920,
            height=1080,
            binary_path=sys.executable,
            content_root=str(fake_sidecar.parent),
            extra_args=[str(fake_sidecar)],
            **kwargs,
        )

    return _make


# ============================================================
# Renderer Protocol conformance.
# ============================================================


def test_rust_renderer_satisfies_renderer_protocol(make_renderer):
    """The proxy satisfies the Renderer protocol (width / height /
    render_frame / end_external_frames) so dependency-injection sites
    that type as `Renderer` don't need to special-case it."""
    r = make_renderer()
    assert isinstance(r, Renderer)
    assert r.width == 1920
    assert r.height == 1080


def test_render_frame_pushes_length_prefixed_frames_to_the_binary_channel(
    make_renderer, tmp_path
):
    """STREAM/VLC slice 2.5: render_frame() lazy-sends the
    begin_external_frames op on the JSON channel, then writes each
    frame length-prefixed onto the dedicated binary channel;
    end_external_frames() writes the 0-length end sentinel."""
    log_path = tmp_path / "sidecar.log"
    r = make_renderer(env_extra={"FAKE_SIDECAR_REQUEST_LOG": str(log_path)})
    r.open()
    frame_size = 1920 * 1080 * 3
    try:
        frame = b"\x00" * frame_size
        r.render_frame(frame)
        r.render_frame(frame)
        r.render_frame(frame)
        r.end_external_frames()
    finally:
        r.close()
    lines = log_path.read_text().splitlines()
    # The begin op went on the JSON channel (raw request logged).
    assert any('"op":"begin_external_frames"' in ln for ln in lines)
    # Three frames of the exact RGB888 size came through the binary
    # channel — and the 0-length sentinel ended the pump (the sidecar
    # only logs frames before the sentinel break).
    frame_lines = [ln for ln in lines if ln.startswith("external_frame:")]
    assert frame_lines == [f"external_frame:{frame_size}"] * 3


def test_render_frame_rgb888_begin_carries_panel_dims_and_format(
    make_renderer, tmp_path
):
    """HW-decode (2026-05-20): an rgb888 render_frame() run sends
    begin_external_frames with the renderer PANEL dims and an explicit
    pixel_format of rgb888."""
    log_path = tmp_path / "sidecar.log"
    r = make_renderer(env_extra={"FAKE_SIDECAR_REQUEST_LOG": str(log_path)})
    r.open()
    try:
        frame = b"\x00" * (r.width * r.height * 3)
        r.render_frame(frame)  # rgb888 is the default
        r.end_external_frames()
    finally:
        r.close()
    lines = log_path.read_text().splitlines()
    begin = [ln for ln in lines if ln.startswith("begin_external_frames:")]
    # Panel dims + the rgb888 format tag.
    assert begin == [f"begin_external_frames:{r.width}x{r.height}:rgb888"]


def test_render_frame_nv12_begin_carries_source_dims_and_format(
    make_renderer, tmp_path
):
    """HW-decode (2026-05-20): an nv12 render_frame() run sends
    begin_external_frames with the SOURCE video dims (not the panel
    dims) and pixel_format=nv12; each NV12 frame is src_w*src_h*3//2
    bytes on the binary channel."""
    log_path = tmp_path / "sidecar.log"
    r = make_renderer(env_extra={"FAKE_SIDECAR_REQUEST_LOG": str(log_path)})
    r.open()
    src_w, src_h = 1280, 720
    nv12_size = src_w * src_h * 3 // 2
    try:
        frame = b"\x00" * nv12_size
        r.render_frame(frame, pixel_format="nv12", frame_w=src_w, frame_h=src_h)
        r.render_frame(frame, pixel_format="nv12", frame_w=src_w, frame_h=src_h)
        r.end_external_frames()
    finally:
        r.close()
    lines = log_path.read_text().splitlines()
    begin = [ln for ln in lines if ln.startswith("begin_external_frames:")]
    # SOURCE dims (1280x720), NOT the 1920x1080 panel; nv12 format.
    assert begin == [f"begin_external_frames:{src_w}x{src_h}:nv12"]
    frame_lines = [ln for ln in lines if ln.startswith("external_frame:")]
    assert frame_lines == [f"external_frame:{nv12_size}"] * 2


def test_render_frame_nv12_without_dims_raises(make_renderer):
    """HW-decode (2026-05-20): nv12 needs the source dims — the
    renderer cover-fit-scales the source onto its panel, so omitting
    frame_w/frame_h is a hard error, not a silent panel-dims default."""
    r = make_renderer()
    r.open()
    try:
        with pytest.raises(RustRendererError, match="nv12 requires frame_w"):
            r.render_frame(b"\x00" * 100, pixel_format="nv12")
    finally:
        r.close()


def test_render_frame_unknown_pixel_format_raises(make_renderer):
    """An unknown pixel_format is rejected before any wire traffic."""
    r = make_renderer()
    r.open()
    try:
        with pytest.raises(RustRendererError, match="unknown pixel_format"):
            r.render_frame(b"\x00" * 100, pixel_format="yuv444")
    finally:
        r.close()


def test_end_external_frames_without_any_frame_is_a_noop(make_renderer):
    """end_external_frames() with no prior render_frame() must be a
    no-op — the VLC pumps call it on every exit path, including ones
    that pushed zero frames. (If it weren't, it would write a
    sentinel + block reading a response for an op never sent.)"""
    r = make_renderer()
    r.open()
    try:
        r.end_external_frames()
        r.end_external_frames()  # idempotent
    finally:
        r.close()


# ============================================================
# Timezone env-threading (Bug 1 follow-up, 2026-05-20).
# ============================================================


def test_sidecar_receives_configured_timezone_in_env(make_renderer, tmp_path):
    """settings.timezone is handed to the sidecar via the TZ env var
    so the renderer's auto_mode clock (libc localtime_r) resolves the
    sign's local time, not UTC."""
    probe = tmp_path / "tz_probe.txt"
    r = make_renderer(
        env_extra={"FAKE_SIDECAR_TZ_PROBE": str(probe)},
        get_timezone=lambda: "Asia/Tokyo",
    )
    try:
        r.open()
        assert probe.read_text() == "Asia/Tokyo"
    finally:
        r.close()


def test_sidecar_tz_unset_when_timezone_is_none(make_renderer, tmp_path, monkeypatch):
    """When settings.timezone is None ('Device local'), the proxy does
    NOT force a TZ — it's dropped from the sidecar env so libc
    localtime_r falls back to the system /etc/localtime. A stale TZ
    inherited from the backend's own env must not leak through."""
    probe = tmp_path / "tz_probe.txt"
    # Even with a TZ in the backend's env, the None case must drop it.
    monkeypatch.setenv("TZ", "Europe/Paris")
    r = make_renderer(
        env_extra={"FAKE_SIDECAR_TZ_PROBE": str(probe)},
        get_timezone=lambda: None,
    )
    try:
        r.open()
        assert probe.read_text() == "<unset>"
    finally:
        r.close()


# ============================================================
# Wire-format encode/decode happy path.
# ============================================================


def test_open_returns_negotiated_mode_and_updates_dims(make_renderer):
    """Open returns OpenOk with mode_w / mode_h; the proxy's width / height
    update to the negotiated values so downstream callers see the truth."""
    r = make_renderer(env_extra={"FAKE_SIDECAR_MODE_W": "1024", "FAKE_SIDECAR_MODE_H": "768"})
    try:
        result = r.open()
        assert isinstance(result, OpenResult)
        assert result.mode_w == 1024
        assert result.mode_h == 768
        assert r.width == 1024
        assert r.height == 768
    finally:
        r.close()


def test_open_called_twice_raises(make_renderer):
    r = make_renderer()
    try:
        r.open()
        with pytest.raises(RustRendererError, match="open\\(\\) called twice"):
            r.open()
    finally:
        r.close()


def test_begin_slide_returns_none_on_empty_result(make_renderer):
    """begin_slide is an Op::Empty op; the proxy normalizes that to a
    bare `None` return (the function returns nothing)."""
    r = make_renderer()
    try:
        r.open()
        assert r.begin_slide(uuid.uuid4(), t0_ms=0, duration_ms=5000) is None
    finally:
        r.close()


def test_advance_paint_slide_decodes_correctly(make_renderer):
    r = make_renderer()
    try:
        r.open()
        result = r.advance(t_ms=500)
        assert isinstance(result, PaintSlide)
        # Fake sidecar echoes t_ms back as t_in_slide_ms.
        assert result.t_in_slide_ms == 500
        assert isinstance(result.slide_id, uuid.UUID)
    finally:
        r.close()


def test_capture_decodes_path_and_bytes(make_renderer, tmp_path):
    r = make_renderer()
    try:
        r.open()
        target = tmp_path / "snap.png"
        result = r.capture(target)
        assert isinstance(result, CaptureResult)
        assert result.path == str(target)
        assert result.bytes == 184
    finally:
        r.close()


def test_close_is_idempotent(make_renderer):
    r = make_renderer()
    r.open()
    r.close()
    # Second close is a no-op.
    r.close()


# ============================================================
# Wire format: assert exact JSON encoding on the wire.
# ============================================================


def test_request_json_encoding_matches_spec(make_renderer, tmp_path):
    """Sanity-check the wire-format JSON the proxy emits. The fake sidecar
    logs every request line; we read the log back and assert exact JSON
    shape per renderer/src/playback.rs (IpcRequest serde tagging)."""
    log_path = tmp_path / "req.log"
    r = make_renderer(env_extra={"FAKE_SIDECAR_REQUEST_LOG": str(log_path)})
    try:
        r.open()
        slide_id = uuid.UUID("01010101-0101-0101-0101-010101010101")
        r.begin_slide(slide_id, t0_ms=100, duration_ms=5000)
        r.advance(t_ms=200)
        r.begin_transition(
            to_slide_id=uuid.UUID("02020202-0202-0202-0202-020202020202"),
            to_duration_ms=5000,
            kind="fade",
            transition_ms=800,
            t0_ms=300,
        )
        r.capture(tmp_path / "x.png")
    finally:
        r.close()

    lines = log_path.read_text().strip().splitlines()
    requests = [json.loads(ln) for ln in lines]

    # Open: externally tagged via "op", params under "params".
    assert requests[0]["op"] == "open"
    assert requests[0]["params"]["output"] == "hdmi"
    # content_root is set by the fixture.
    assert "content_root" in requests[0]["params"]

    # BeginSlide.
    assert requests[1] == {
        "op": "begin_slide",
        "params": {
            "slide_id": "01010101-0101-0101-0101-010101010101",
            "t0_ms": 100,
            "duration_ms": 5000,
        },
    }

    # Advance.
    assert requests[2] == {"op": "advance", "params": {"t_ms": 200}}

    # BeginTransition.
    assert requests[3] == {
        "op": "begin_transition",
        "params": {
            "to_slide_id": "02020202-0202-0202-0202-020202020202",
            "to_duration_ms": 5000,
            "kind": "fade",
            "transition_ms": 800,
            "t0_ms": 300,
        },
    }

    # Capture.
    assert requests[4]["op"] == "capture"
    assert requests[4]["params"]["path"].endswith("x.png")

    # Close (no params field at all per the Rust IpcRequest::Close
    # variant — serde tagged enum with no inner data).
    assert requests[5] == {"op": "close"}


# ============================================================
# Error-class dispatch: byte-stable wire-format error strings.
# ============================================================


@pytest.mark.parametrize(
    "wire_string",
    [
        # From renderer/src/ipc_main.rs::validate_paint_slide_inputs.
        "paint_slide: image_slide requires content_root (--content-root)",
        # From renderer/src/ipc_main.rs::validate_paint_transition_endpoints.
        "paint_transition: from non-text slide TBD",
        "paint_transition: to non-text slide TBD",
        # From renderer/src/ipc_main.rs::validate_capture_inputs.
        # (paint_slide for Video now succeeds via V4L2 pieces 3+4;
        # only Capture remains unimplemented for VideoSlide.)
        "Capture: VideoSlide capture not implemented (image + text only)",
    ],
)
def test_err_response_raises_op_error_with_stable_message(
    make_renderer, wire_string
):
    """The proxy translates `{"err": {"error": "..."}}` into RustRendererOpError
    with `.message` equal to the verbatim sidecar string. This pins error-
    class dispatch: callers can match `e.message == "<known string>"` to
    branch on which validator rejected them."""
    # Use reconfigure as the channel since the fake sidecar lets us
    # inject the err response there.
    r = make_renderer(env_extra={"FAKE_SIDECAR_RECONFIGURE_ERR": wire_string})
    try:
        r.open()
        with pytest.raises(RustRendererOpError) as exc_info:
            r.reconfigure(brightness=0.5)
        assert exc_info.value.message == wire_string
        # Also reachable as str(e).
        assert str(exc_info.value) == wire_string
    finally:
        r.close()


def test_op_error_is_subclass_of_renderer_error(make_renderer):
    """Callers can broad-except RustRendererError for any proxy failure."""
    r = make_renderer(env_extra={"FAKE_SIDECAR_RECONFIGURE_ERR": "some err"})
    try:
        r.open()
        with pytest.raises(RustRendererError):
            r.reconfigure(brightness=0.5)
    finally:
        r.close()


# ============================================================
# Slice 4: RustRendererUnsupportedSlideError promotion at the
# decode boundary. The proxy translates the byte-stable sidecar
# error strings for unsupported slide kinds into the typed
# subclass so callers can dispatch on isinstance instead of
# parsing the message.
# ============================================================


@pytest.mark.parametrize(
    "wire_string",
    [
        # paint_slide for Video is now wired (V4L2 pieces 3+4); the
        # only remaining UnsupportedSlide wire-format for Video is
        # the Capture-side validator.
        "Capture: VideoSlide capture not implemented (image + text only)",
        "paint_transition: from non-text slide TBD",
        "paint_transition: to non-text slide TBD",
    ],
)
def test_unsupported_slide_wire_strings_promote_to_subclass(
    make_renderer, wire_string
):
    """Sidecar wire-format strings that signal "slide kind not yet
    supported" get promoted to RustRendererUnsupportedSlideError at
    the proxy boundary. The promotion is what lets AutoFallbackRenderer
    treat "skip this slide" differently from "the subprocess is busted"
    (which swaps to MockRenderer).
    """
    r = make_renderer(env_extra={"FAKE_SIDECAR_RECONFIGURE_ERR": wire_string})
    try:
        r.open()
        with pytest.raises(RustRendererUnsupportedSlideError) as exc_info:
            r.reconfigure(brightness=0.5)
        assert exc_info.value.message == wire_string
        # Verbatim str() so callers that log .args still see the wire string.
        assert str(exc_info.value) == wire_string
    finally:
        r.close()


def test_unsupported_slide_error_is_subclass_of_op_error(make_renderer):
    """isinstance(e, RustRendererOpError) must be True for the promoted
    subclass so existing broad-except callers still catch it. Pins the
    subclass relationship that AutoFallbackRenderer relies on for its
    except-chain ordering."""
    r = make_renderer(
        env_extra={
            "FAKE_SIDECAR_RECONFIGURE_ERR":
                "Capture: VideoSlide capture not implemented (image + text only)"
        }
    )
    try:
        r.open()
        with pytest.raises(RustRendererOpError) as exc_info:
            r.reconfigure(brightness=0.5)
        assert isinstance(exc_info.value, RustRendererUnsupportedSlideError)
    finally:
        r.close()


def test_legacy_video_slides_tbd_marker_is_gone():
    """Regression guard for qa/v1-spec-delta-2026-05-14.md final P2.

    The legacy "video slides TBD" wire-substring used to match both
    the paint_slide validator AND the Capture validator. Paint
    shipped via V4L2 pieces 3+4; Capture's marker was renamed to a
    distinct substring ("VideoSlide capture not implemented") so a
    paint_slide-side failure (asset.mp4 corrupt, codec absent,
    frame upload failure) can never be misclassified as the deferred
    Capture path. This test pins the absence of the legacy substring
    so a future regression that re-introduces it gets caught loud.
    """
    from openmarquee.rendering.rust_renderer import (
        _UNSUPPORTED_SLIDE_WIRE_MARKERS,
    )

    for marker in _UNSUPPORTED_SLIDE_WIRE_MARKERS:
        assert "video slides TBD" not in marker, (
            f"legacy 'video slides TBD' substring found in marker: {marker!r}"
        )


def test_generic_op_error_not_promoted(make_renderer):
    """Errors that aren't slide-kind-related stay as bare
    RustRendererOpError -- promotion is substring-gated, not blanket."""
    r = make_renderer(
        env_extra={
            "FAKE_SIDECAR_RECONFIGURE_ERR":
                "paint_slide: image_slide requires content_root (--content-root)"
        }
    )
    try:
        r.open()
        with pytest.raises(RustRendererOpError) as exc_info:
            r.reconfigure(brightness=0.5)
        # Not the subclass -- this is a different failure class.
        assert not isinstance(exc_info.value, RustRendererUnsupportedSlideError)
    finally:
        r.close()


# ============================================================
# Failure paths: subprocess death, broken pipe, malformed JSON.
# ============================================================


def test_subprocess_death_mid_session_raises_subprocess_error(make_renderer):
    """If the subprocess dies mid-session and reconnect is DISABLED, the
    next op raises plain RustRendererSubprocessError. This pins the
    fail-loud-no-respawn fallback path. (See
    test_subprocess_death_triggers_reconnect for the auto-reconnect
    behavior with reconnect enabled.)"""
    r = make_renderer(
        env_extra={"FAKE_SIDECAR_DIE_AFTER_OP": "begin_slide"},
        reconnect_max_retries=0,
        watchdog_enabled=False,
    )
    try:
        r.open()
        r.begin_slide(uuid.uuid4(), t0_ms=0, duration_ms=5000)
        # Give the subprocess a moment to exit.
        time.sleep(0.1)
        with pytest.raises(RustRendererSubprocessError) as exc_info:
            r.advance(t_ms=100)
        # With reconnect disabled, NOT a RespawnedError.
        assert not isinstance(exc_info.value, RustRendererRespawnedError)
        # Trail mentions "reconnect exhausted" since max_retries=0.
        assert "reconnect exhausted" in str(exc_info.value)
    finally:
        r.close()


def test_missing_binary_raises_subprocess_error(tmp_path):
    r = RustRenderer(
        width=128,
        height=96,
        binary_path=str(tmp_path / "definitely-not-here"),
        content_root=str(tmp_path),
    )
    with pytest.raises(RustRendererSubprocessError, match="not found"):
        r.open()


def test_malformed_json_response_raises_protocol_error(make_renderer):
    r = make_renderer(env_extra={"FAKE_SIDECAR_MALFORMED_NEXT": "1"})
    try:
        with pytest.raises(RustRendererProtocolError, match="Invalid JSON"):
            r.open()
    finally:
        r.close()


def test_is_alive_reflects_subprocess_state(make_renderer):
    r = make_renderer()
    assert r.is_alive is False  # not launched yet
    try:
        r.open()
        assert r.is_alive is True
    finally:
        r.close()
    assert r.is_alive is False


def test_stderr_drainer_doesnt_deadlock_on_burst(make_renderer):
    """If the subprocess writes more stderr than the pipe buffer holds
    (~64 KB) before we read, it deadlocks. Verify the drainer thread
    keeps the pipe clear by emitting a large stderr burst at startup."""
    # 2000 lines × ~30 chars = ~60 KB, right at the Linux 64 KB pipe-
    # buffer threshold. Without the drainer, the subprocess would block
    # on its second write to stderr before responding to open(). With
    # the drainer running, IPC keeps working.
    r = make_renderer(env_extra={"FAKE_SIDECAR_STDERR_BURST": "2000"})
    try:
        result = r.open()
        assert result.mode_w == 1920
        # Subsequent ops still work.
        r.begin_slide(uuid.uuid4(), t0_ms=0, duration_ms=5000)
        assert isinstance(r.advance(t_ms=100), PaintSlide)
    finally:
        r.close()


def test_context_manager_closes_on_exit(make_renderer):
    """`with RustRenderer(...) as r:` opens on enter and closes on exit,
    even if the caller raises mid-session."""
    r = make_renderer()
    with r:
        assert r.is_alive is True
        r.begin_slide(uuid.uuid4(), t0_ms=0, duration_ms=5000)
    assert r.is_alive is False


def test_context_manager_closes_even_on_exception(make_renderer):
    r = make_renderer()
    with pytest.raises(RuntimeError, match="boom"):
        with r:
            assert r.is_alive
            raise RuntimeError("boom")
    assert r.is_alive is False


# ============================================================
# All AdvanceResult variants round-trip cleanly through the
# proxy's _parse_advance_result.
# ============================================================


def test_parse_advance_result_paint_slide():
    body = {
        "__command__": "paint_slide",
        "slide_id": "01010101-0101-0101-0101-010101010101",
        "t_in_slide_ms": 500,
    }
    result = RustRenderer._parse_advance_result(body)
    assert isinstance(result, PaintSlide)
    assert result.t_in_slide_ms == 500


def test_parse_advance_result_paint_transition():
    body = {
        "__command__": "paint_transition",
        "from": "01010101-0101-0101-0101-010101010101",
        "to": "02020202-0202-0202-0202-020202020202",
        "kind": "fade",
        "progress": 0.42,
    }
    result = RustRenderer._parse_advance_result(body)
    assert isinstance(result, PaintTransition)
    assert result.kind == "fade"
    assert abs(result.progress - 0.42) < 1e-6
    assert result.from_id == uuid.UUID("01010101-0101-0101-0101-010101010101")
    assert result.to == uuid.UUID("02020202-0202-0202-0202-020202020202")


def test_parse_advance_result_slide_complete():
    body = {
        "__command__": "slide_complete",
        "slide_id": "01010101-0101-0101-0101-010101010101",
    }
    result = RustRenderer._parse_advance_result(body)
    assert isinstance(result, SlideComplete)


def test_parse_advance_result_idle():
    body = {"__command__": "idle"}
    result = RustRenderer._parse_advance_result(body)
    assert isinstance(result, Idle)


def test_parse_advance_result_unknown_command_raises_protocol_error():
    body = {"__command__": "unknown_variant_xyz"}
    with pytest.raises(RustRendererProtocolError, match="unknown command"):
        RustRenderer._parse_advance_result(body)


def test_parse_advance_result_empty_raises_protocol_error():
    with pytest.raises(RustRendererProtocolError, match="got Empty result"):
        RustRenderer._parse_advance_result(None)


# ============================================================
# Response-decode malformed-shape coverage.
# ============================================================


def test_decode_response_missing_both_ok_and_err():
    r = RustRenderer(width=1, height=1)
    with pytest.raises(RustRendererProtocolError, match="missing both"):
        r._decode_response("open", {"something_else": True})


def test_decode_response_err_missing_error_field():
    r = RustRenderer(width=1, height=1)
    with pytest.raises(RustRendererProtocolError, match="Malformed Err"):
        r._decode_response("open", {"err": {"not_error": "x"}})


def test_decode_response_ok_missing_result():
    r = RustRenderer(width=1, height=1)
    with pytest.raises(RustRendererProtocolError, match="Malformed Ok"):
        r._decode_response("open", {"ok": {}})


def test_decode_response_ok_result_missing_command_tag():
    r = RustRenderer(width=1, height=1)
    with pytest.raises(RustRendererProtocolError, match="missing 'command' tag"):
        r._decode_response("open", {"ok": {"result": {"foo": "bar"}}})


def test_decode_response_non_object_raises_protocol_error():
    r = RustRenderer(width=1, height=1)
    with pytest.raises(RustRendererProtocolError, match="not an object"):
        r._decode_response("open", "this is a string")


# ============================================================
# Reconnect / watchdog / health-probe (2026-05-14 dispatch).
# ============================================================


def test_subprocess_death_triggers_reconnect_and_raises_respawned(make_renderer):
    """With auto-reconnect ENABLED (default 3 retries / 60s window):
    a subprocess that dies mid-session is transparently respawned.
    The failing op raises RustRendererRespawnedError (a subclass of
    SubprocessError) so callers know to replay session state.
    Subsequent ops succeed on the fresh subprocess.
    """
    r = make_renderer(
        env_extra={"FAKE_SIDECAR_DIE_AFTER_OP": "begin_slide"},
        # Disable watchdog so the only reconnect path is the in-op
        # death detection (predictable for the assertion below).
        watchdog_enabled=False,
    )
    try:
        r.open()
        r.begin_slide(uuid.uuid4(), t0_ms=0, duration_ms=5000)
        time.sleep(0.1)  # let subprocess exit
        # The advance op detects death + reconnects + raises Respawned.
        with pytest.raises(RustRendererRespawnedError) as exc_info:
            r.advance(t_ms=100)
        # Respawned is a SubprocessError subclass.
        assert isinstance(exc_info.value, RustRendererSubprocessError)
        # Proxy is now ready on the new subprocess.
        assert r.is_alive is True
        # The NEW subprocess never received a begin_slide, so DIE_AFTER_
        # OP=begin_slide hasn't fired yet — advance + capture succeed.
        result = r.advance(t_ms=200)
        assert isinstance(result, PaintSlide)
    finally:
        r.close()


def test_reconnect_exhausted_after_max_retries(make_renderer, tmp_path):
    """If the subprocess dies on every op (e.g. an unrecoverable
    binary), reconnect bounded retries (default 3) exhaust within the
    window and a plain SubprocessError is raised with the trail."""
    # Configure fake sidecar to die after EVERY op via die-after-open.
    # Each reconnect attempts open + the new sub dies immediately
    # after responding to that open.
    r = make_renderer(
        env_extra={"FAKE_SIDECAR_DIE_AFTER_OP": "open"},
        reconnect_max_retries=2,
        watchdog_enabled=False,
    )
    try:
        r.open()  # first open succeeds, sub dies AFTER responding
        time.sleep(0.1)
        # First advance: detects death → reconnect #1 (Open lands, sub
        # dies after). The post-reconnect Open succeeded so the
        # _send_op call inside reconnect didn't raise; we surface a
        # RespawnedError on this op.
        with pytest.raises(RustRendererSubprocessError):
            r.advance(t_ms=100)
        time.sleep(0.1)
        # Second advance: detects death again → reconnect #2 succeeds.
        # Surface Respawned again.
        with pytest.raises(RustRendererSubprocessError):
            r.advance(t_ms=100)
        time.sleep(0.1)
        # Third advance: would need reconnect #3, but max_retries=2 so
        # exhausted -> plain SubprocessError (NOT RespawnedError) with
        # trail in the message.
        with pytest.raises(RustRendererSubprocessError) as exc_info:
            r.advance(t_ms=100)
        assert not isinstance(exc_info.value, RustRendererRespawnedError)
        assert "reconnect exhausted" in str(exc_info.value)
        # The trail should mention both prior reconnect reasons.
        assert "trail:" in str(exc_info.value)
    finally:
        r.close()


def test_reconnect_window_resets_after_idle_period(make_renderer):
    """The reconnect counter trims out attempts older than the rolling
    window, so a long quiet period 'forgives' transient blips."""
    r = make_renderer(
        env_extra={"FAKE_SIDECAR_DIE_AFTER_OP": "begin_slide"},
        reconnect_max_retries=2,
        reconnect_window_s=0.5,  # short window for fast test
        watchdog_enabled=False,
    )
    try:
        r.open()
        r.begin_slide(uuid.uuid4(), t0_ms=0, duration_ms=5000)
        time.sleep(0.1)
        with pytest.raises(RustRendererRespawnedError):
            r.advance(t_ms=100)
        # We've used 1 of 2 reconnect slots. Wait past the window.
        time.sleep(0.7)
        # The probe + count should now show 0 in-window attempts.
        health = r.health_probe()
        assert health.reconnect_attempts_in_window == 0
    finally:
        r.close()


def test_watchdog_detects_death_and_reconnects(make_renderer):
    """The watchdog thread polls liveness; on detected death between
    ops it triggers reconnect on its own. The next caller op finds the
    proxy already healthy."""
    r = make_renderer(
        env_extra={"FAKE_SIDECAR_DIE_AFTER_OP": "begin_slide"},
        watchdog_interval_s=0.05,  # 20Hz for fast test
    )
    try:
        r.open()
        original_pid = r._proc.pid  # type: ignore[union-attr]
        r.begin_slide(uuid.uuid4(), t0_ms=0, duration_ms=5000)
        # Wait long enough for the subprocess to exit AND the watchdog
        # to notice + reconnect. 20Hz * 5 ticks = 0.25s upper bound;
        # add a generous fudge for thread scheduling.
        time.sleep(1.0)
        # Watchdog should have reconnected by now: liveness restored.
        assert r.is_alive is True
        # And the underlying pid should be different (proves respawn).
        assert r._proc.pid != original_pid  # type: ignore[union-attr]
        # Reconnect bookkeeping recorded the event.
        health = r.health_probe()
        assert health.reconnect_attempts_in_window >= 1
        assert any("watchdog" in r for r in health.reconnect_history)
    finally:
        r.close()


def test_watchdog_joins_cleanly_on_close(make_renderer):
    """Close() stops the watchdog thread before tearing down the
    subprocess. After close(), no watchdog thread is alive."""
    r = make_renderer(watchdog_interval_s=0.05)
    r.open()
    # Confirm watchdog is running.
    assert r._watchdog_thread is not None
    assert r._watchdog_thread.is_alive() is True
    r.close()
    assert r._watchdog_thread is None


def test_watchdog_doesnt_deadlock_under_op_pressure(make_renderer):
    """The watchdog must use non-blocking lock acquisition so that a
    slow op never deadlocks it (and the watchdog thread join in close()
    never wedges). Sanity test: fire many ops concurrently with a tight
    watchdog tick and confirm everything completes within bounds."""
    r = make_renderer(watchdog_interval_s=0.01)  # 100Hz
    results: list[Any] = []
    errors: list[Exception] = []

    def worker():
        try:
            for _ in range(50):
                results.append(r.advance(t_ms=42))
        except Exception as e:
            errors.append(e)

    try:
        r.open()
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)  # generous; should be near-instant
            assert not t.is_alive(), "worker stuck — possible deadlock"
        assert errors == [], f"workers raised: {errors}"
        # 4 threads × 50 ops each.
        assert len(results) == 200
    finally:
        r.close()


def test_health_probe_alive_after_open(make_renderer):
    r = make_renderer(watchdog_enabled=False)
    try:
        r.open()
        h = r.health_probe()
        assert isinstance(h, HealthState)
        assert h.is_alive is True
        assert h.exit_code is None
        assert h.reconnect_attempts_in_window == 0
        assert h.reconnect_history == ()
    finally:
        r.close()


def test_health_probe_dead_after_close(make_renderer):
    r = make_renderer(watchdog_enabled=False)
    r.open()
    r.close()
    h = r.health_probe()
    assert h.is_alive is False
    # After teardown, _proc is None and exit_code is also None.
    assert h.exit_code is None


def test_health_probe_records_reconnect_history(make_renderer):
    """After a reconnect, health_probe surfaces the bookkeeping."""
    r = make_renderer(
        env_extra={"FAKE_SIDECAR_DIE_AFTER_OP": "begin_slide"},
        watchdog_enabled=False,
    )
    try:
        r.open()
        r.begin_slide(uuid.uuid4(), t0_ms=0, duration_ms=5000)
        time.sleep(0.1)
        with pytest.raises(RustRendererRespawnedError):
            r.advance(t_ms=100)
        h = r.health_probe()
        assert h.is_alive is True
        assert h.reconnect_attempts_in_window == 1
        assert len(h.reconnect_history) == 1
        # The reason mentions which op triggered + that it was attempt 1.
        assert "attempt 1" in h.reconnect_history[0]
    finally:
        r.close()


def test_reconnect_disabled_when_max_retries_zero(make_renderer):
    """reconnect_max_retries=0 disables auto-reconnect: subprocess
    death immediately raises plain SubprocessError on the next op."""
    r = make_renderer(
        env_extra={"FAKE_SIDECAR_DIE_AFTER_OP": "begin_slide"},
        reconnect_max_retries=0,
        watchdog_enabled=False,
    )
    try:
        r.open()
        r.begin_slide(uuid.uuid4(), t0_ms=0, duration_ms=5000)
        time.sleep(0.1)
        with pytest.raises(RustRendererSubprocessError) as exc_info:
            r.advance(t_ms=100)
        assert not isinstance(exc_info.value, RustRendererRespawnedError)
        assert r.is_alive is False  # no respawn happened
    finally:
        r.close()


# ============================================================
# Real-subprocess end-to-end test (skip on Mac / no binary).
# ============================================================


REAL_BINARY_ENV = "OPENMARQUEE_RUST_BINARY_E2E"


def _real_binary_available() -> bool:
    path = os.environ.get(REAL_BINARY_ENV)
    if not path:
        return False
    return shutil.which(path) is not None or Path(path).is_file()


@pytest.mark.skipif(
    not _real_binary_available(),
    reason=f"Set {REAL_BINARY_ENV}=<path to openmarquee-render> to run "
    "the real-subprocess end-to-end test (typically dev Pi only).",
)
def test_real_sidecar_open_close_roundtrip(tmp_path):
    """End-to-end: launch the actual Rust sidecar binary and round-trip
    open + close. Skipped on Mac (no DRM); run on dev Pi with the env
    var set to the binary path."""
    binary = os.environ[REAL_BINARY_ENV]
    # Need a content_root that exists; tmp_path works.
    r = RustRenderer(
        width=1920,
        height=1080,
        binary_path=binary,
        content_root=str(tmp_path),
        output="hdmi",
    )
    try:
        result = r.open()
        assert result.mode_w > 0
        assert result.mode_h > 0
    finally:
        r.close()
    assert r.is_alive is False
