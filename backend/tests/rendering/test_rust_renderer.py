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
import time
import uuid
from pathlib import Path

import pytest

from openmarquee.rendering import Renderer
from openmarquee.rendering.rust_renderer import (
    CaptureResult,
    Idle,
    OpenResult,
    PaintSlide,
    PaintTransition,
    RustRenderer,
    RustRendererError,
    RustRendererOpError,
    RustRendererProtocolError,
    RustRendererSubprocessError,
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
    """The proxy nominally satisfies the Renderer protocol (width / height /
    render_frame) so dependency-injection sites that type as `Renderer`
    don't need to special-case us at the type level. Actual frame rendering
    raises NotImplementedError; slice 4 will rewire playback.py to skip
    render_frame when it's a RustRenderer."""
    r = make_renderer()
    assert isinstance(r, Renderer)
    assert r.width == 1920
    assert r.height == 1080


def test_render_frame_raises_not_implemented(make_renderer):
    r = make_renderer()
    with pytest.raises(NotImplementedError):
        r.render_frame(b"\x00" * 1920 * 1080 * 3)


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
        "paint_slide: video slides TBD (image + text both supported)",
        # From renderer/src/ipc_main.rs::validate_paint_transition_endpoints.
        "paint_transition: from non-text slide TBD",
        "paint_transition: to non-text slide TBD",
        # From renderer/src/ipc_main.rs::validate_capture_inputs.
        "Capture: video slides TBD (image + text both supported)",
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
# Failure paths: subprocess death, broken pipe, malformed JSON.
# ============================================================


def test_subprocess_death_mid_session_raises_subprocess_error(make_renderer):
    """If the subprocess dies before responding (per the fail-loud-no-respawn
    contract for slice 1), the next op raises RustRendererSubprocessError."""
    # Configure fake sidecar to exit after the first begin_slide.
    r = make_renderer(env_extra={"FAKE_SIDECAR_DIE_AFTER_OP": "begin_slide"})
    try:
        r.open()
        r.begin_slide(uuid.uuid4(), t0_ms=0, duration_ms=5000)
        # Give the subprocess a moment to exit.
        time.sleep(0.1)
        with pytest.raises(RustRendererSubprocessError):
            r.advance(t_ms=100)
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
