"""Slice 2 coverage for StreamConsumer (STREAM/VLC arc §9).

StreamConsumer spawns ffmpeg as a subprocess and yields raw NV12
frames (HW-decode arc, 2026-05-20 — see the module docstring). These
tests swap the `ffmpeg` binary for a tiny mock script (emits a known
number of fixed-size frames, writes to stderr, exits or hangs) so the
subprocess spawn / fixed-size frame read / stderr capture / teardown
machinery is exercised without a real ffmpeg or RTSP server.

The consumer ffprobes the RTSP URL for the source resolution; the
tests inject the source size via the `source_size=` constructor
kwarg so ffprobe is skipped (the probe path has its own coverage).
"""

from __future__ import annotations

import asyncio
import stat
import sys

import pytest

from openmarquee.stream_consumer import StreamConsumer, validate_stream_url


def _write_mock_ffmpeg(
    path,
    *,
    frame_size: int,
    n_frames: int,
    trailing_partial: int = 0,
    stderr_text: str = "",
    exit_code: int = 0,
    hang: bool = False,
    continuous: bool = False,
) -> str:
    """Write an executable mock-ffmpeg script and return its path.

    The mock ignores its argv (the real ffmpeg flags). It writes
    `stderr_text` to stderr, emits `n_frames` full frames of
    `frame_size` bytes (frame i filled with the byte value i % 256),
    optionally emits a `trailing_partial`-byte short final frame,
    then exits `exit_code` -- or hangs forever if `hang` is set, so a
    teardown test can verify close() reaps it.

    `continuous`: instead of a fixed `n_frames` run, emit frames in an
    endless ~50 fps loop until killed -- for tests that need a
    steadily-streaming source (e.g. pause-preemption).
    """
    body = f"#!{sys.executable}\n"
    body += "import sys, time\n"
    body += f"sys.stderr.write({stderr_text!r})\n"
    body += "sys.stderr.flush()\n"
    body += "out = sys.stdout.buffer\n"
    if continuous:
        body += "i = 0\n"
        body += "while True:\n"
        body += f"    out.write(bytes([i % 256]) * {frame_size})\n"
        body += "    out.flush()\n"
        body += "    i += 1\n"
        body += "    time.sleep(0.02)\n"
    else:
        body += f"for i in range({n_frames}):\n"
        body += f"    out.write(bytes([i % 256]) * {frame_size})\n"
        body += "    out.flush()\n"
        if trailing_partial:
            body += f"out.write(bytes({trailing_partial}))\n"
            body += "out.flush()\n"
        if hang:
            body += "while True:\n    time.sleep(0.05)\n"
        else:
            body += f"sys.exit({exit_code})\n"
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


async def _wait_until(predicate, timeout: float = 3.0, interval: float = 0.01):
    """Poll `predicate` until truthy or `timeout` elapses."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


# --- frame yield + EOF -----------------------------------------------------


@pytest.mark.asyncio
async def test_yields_n_frames_at_frame_size(tmp_path):
    """ffmpeg emits N raw frames; the consumer yields exactly N
    buffers, each src_w*src_h*3//2 bytes (NV12), in order. EOF
    (ffmpeg exit) ends the iteration cleanly."""
    # NV12 frame size for the injected 8x8 source.
    frame_size = 8 * 8 * 3 // 2
    mock = _write_mock_ffmpeg(
        tmp_path / "ffmpeg", frame_size=frame_size, n_frames=5
    )
    consumer = StreamConsumer(
        "rtsp://host:8554/live", 8, 8, ffmpeg_bin=mock, source_size=(8, 8)
    )

    frames = [f async for f in consumer.frames()]
    await consumer.close()

    assert len(frames) == 5
    for i, frame in enumerate(frames):
        assert len(frame) == frame_size
        # Frame i is uniformly byte-value i — confirms ordering and
        # that no bytes are dropped or reordered across frames.
        assert frame == bytes([i]) * frame_size


@pytest.mark.asyncio
async def test_trailing_partial_frame_is_discarded(tmp_path):
    """A short final read (ffmpeg exiting mid-frame) is dropped — the
    consumer never yields an under-sized buffer the renderer would
    reject."""
    frame_size = 8 * 8 * 3 // 2
    mock = _write_mock_ffmpeg(
        tmp_path / "ffmpeg",
        frame_size=frame_size,
        n_frames=3,
        trailing_partial=frame_size // 2,
    )
    consumer = StreamConsumer(
        "rtsp://host:8554/live", 8, 8, ffmpeg_bin=mock, source_size=(8, 8)
    )

    frames = [f async for f in consumer.frames()]
    await consumer.close()

    assert len(frames) == 3
    assert all(len(f) == frame_size for f in frames)


# --- stderr capture --------------------------------------------------------


@pytest.mark.asyncio
async def test_stderr_is_captured(tmp_path):
    """ffmpeg's stderr is drained into the consumer's bounded tail
    buffer so a connect failure can be diagnosed."""
    frame_size = 4 * 4 * 3 // 2
    mock = _write_mock_ffmpeg(
        tmp_path / "ffmpeg",
        frame_size=frame_size,
        n_frames=2,
        stderr_text="rtsp: connection refused\n",
    )
    consumer = StreamConsumer(
        "rtsp://host:8554/live", 4, 4, ffmpeg_bin=mock, source_size=(4, 4)
    )

    frames = [f async for f in consumer.frames()]
    await consumer.close()

    assert len(frames) == 2
    assert "connection refused" in consumer.stderr_tail


# --- teardown --------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_reaps_hanging_ffmpeg(tmp_path):
    """close() terminates a still-running ffmpeg: the subprocess is
    reaped (returncode set) and the frames() iterator unblocks."""
    frame_size = 8 * 8 * 3 // 2
    mock = _write_mock_ffmpeg(
        tmp_path / "ffmpeg", frame_size=frame_size, n_frames=2, hang=True
    )
    consumer = StreamConsumer(
        "rtsp://host:8554/live", 8, 8, ffmpeg_bin=mock, source_size=(8, 8)
    )

    collected: list[bytes] = []

    async def pump():
        async for frame in consumer.frames():
            collected.append(frame)

    task = asyncio.create_task(pump())
    try:
        # Mock emits 2 frames then hangs — wait until they arrive so
        # ffmpeg is definitely running when we close.
        got = await _wait_until(lambda: len(collected) >= 2)
        assert got, "mock ffmpeg never delivered its frames"
        assert consumer._proc is not None and consumer._proc.returncode is None

        await consumer.close()

        # close() terminated ffmpeg; stdout EOF unblocks readexactly,
        # so the pump task completes on its own.
        await asyncio.wait_for(task, timeout=3.0)
        assert consumer._proc.returncode is not None
    finally:
        if not task.done():
            task.cancel()


@pytest.mark.asyncio
async def test_close_during_spawn_does_not_orphan_ffmpeg(tmp_path, monkeypatch):
    """A close() landing mid-spawn (during create_subprocess_exec)
    must not orphan ffmpeg. The spawn-and-assign is _reap_lock-
    guarded, so close()'s reap blocks until self._proc is set, then
    terminates it."""
    frame_size = 8 * 8 * 3 // 2
    mock = _write_mock_ffmpeg(
        tmp_path / "ffmpeg", frame_size=frame_size, n_frames=1, hang=True
    )
    consumer = StreamConsumer(
        "rtsp://host:8554/live", 8, 8, ffmpeg_bin=mock, source_size=(8, 8)
    )

    spawn_entered = asyncio.Event()
    release_spawn = asyncio.Event()
    real_create = asyncio.create_subprocess_exec

    async def gated_create(*args, **kwargs):
        spawn_entered.set()
        await release_spawn.wait()
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", gated_create)

    async def pump():
        async for _ in consumer.frames():
            pass

    task = asyncio.create_task(pump())
    # frames() is now suspended inside gated_create, holding _reap_lock,
    # with self._proc still None.
    await asyncio.wait_for(spawn_entered.wait(), timeout=2.0)
    assert consumer._proc is None

    # close() concurrently: it sets _closed and its _reap() blocks on
    # _reap_lock (held by the suspended frames()).
    close_task = asyncio.create_task(consumer.close())
    await asyncio.sleep(0)  # let close() reach the lock wait
    # Let the spawn finish: frames() assigns _proc + releases the lock,
    # then close()'s _reap() acquires it and terminates ffmpeg.
    release_spawn.set()

    await asyncio.wait_for(close_task, timeout=3.0)
    await asyncio.wait_for(task, timeout=3.0)

    assert consumer._proc is not None
    # Reaped, not orphaned — returncode is set.
    assert consumer._proc.returncode is not None


@pytest.mark.asyncio
async def test_nonzero_exit_logs_stderr_warning(tmp_path, caplog):
    """When ffmpeg exits non-zero, the captured stderr tail is logged
    at WARNING so an operator can see why the stream failed."""
    frame_size = 4 * 4 * 3 // 2
    mock = _write_mock_ffmpeg(
        tmp_path / "ffmpeg",
        frame_size=frame_size,
        n_frames=1,
        stderr_text="rtsp: 404 stream not found\n",
        exit_code=3,
    )
    consumer = StreamConsumer(
        "rtsp://host:8554/live", 4, 4, ffmpeg_bin=mock, source_size=(4, 4)
    )

    with caplog.at_level("WARNING", logger="openmarquee.stream_consumer"):
        frames = [f async for f in consumer.frames()]
        await consumer.close()

    assert len(frames) == 1
    assert "ffmpeg exited rc=3" in caplog.text
    assert "404 stream not found" in caplog.text


@pytest.mark.asyncio
async def test_missing_ffmpeg_binary_yields_nothing(tmp_path):
    """A missing / non-executable ffmpeg binary surfaces as a clean
    no-frames exit, not an exception — the caller's on-unreachable
    handling decides what to do."""
    consumer = StreamConsumer(
        "rtsp://host:8554/live",
        8,
        8,
        ffmpeg_bin=str(tmp_path / "does-not-exist-ffmpeg"),
        source_size=(8, 8),
    )

    frames = [f async for f in consumer.frames()]
    await consumer.close()

    assert frames == []


@pytest.mark.asyncio
async def test_second_frames_call_yields_nothing(tmp_path):
    """The consumer is single-use: once frames() has run, a second
    call yields nothing rather than spawning a second ffmpeg."""
    frame_size = 4 * 4 * 3 // 2
    mock = _write_mock_ffmpeg(
        tmp_path / "ffmpeg", frame_size=frame_size, n_frames=2
    )
    consumer = StreamConsumer(
        "rtsp://host:8554/live", 4, 4, ffmpeg_bin=mock, source_size=(4, 4)
    )

    first = [f async for f in consumer.frames()]
    second = [f async for f in consumer.frames()]
    await consumer.close()

    assert len(first) == 2
    assert second == []


# --- HW-decode ffmpeg command line -----------------------------------------


def test_argv_hw_decodes_and_emits_nv12_without_swscale():
    """HW-decode arc (2026-05-20): the ffmpeg command HW-decodes the
    H.264 input (`-c:v h264_v4l2m2m`), drops the `-vf` swscale filter
    entirely, and emits raw NV12 on stdout. swscale was the measured
    ~16fps bottleneck; the renderer does the scale + NV12→RGB on the
    GPU now."""
    consumer = StreamConsumer("rtsp://laptop:8554/live", 1920, 1080)
    argv = consumer._build_argv()

    assert argv[0] == "ffmpeg"
    # HW H.264 decode on the Pi's bcm2835 codec.
    assert argv[argv.index("-c:v") + 1] == "h264_v4l2m2m"
    assert "-an" in argv  # audio dropped on ingest
    assert "rtsp://laptop:8554/live" in argv
    # The swscale `-vf` filter is gone — the renderer cover-fits.
    assert "-vf" not in argv
    # Raw NV12 out on stdout.
    assert argv[argv.index("-pix_fmt") + 1] == "nv12"
    assert argv[-3:] == ["-f", "rawvideo", "-"]


def test_argv_no_longer_carries_renderer_dims():
    """The ffmpeg argv no longer mentions the renderer dimensions —
    output is source-resolution NV12; the cover-fit target moved to
    the renderer."""
    consumer = StreamConsumer("rtsp://laptop:8554/live", 1920, 1080)
    argv = consumer._build_argv()
    assert "1920:1080" not in " ".join(argv)
    assert "scale" not in " ".join(argv)


def test_probe_argv_queries_source_dims_over_tcp():
    """The ffprobe command reports the first video stream's width +
    height as JSON, over RTSP-TCP to match the ffmpeg ingest."""
    consumer = StreamConsumer("rtsp://laptop:8554/live", 1920, 1080)
    argv = consumer._build_probe_argv()

    assert argv[0] == "ffprobe"
    assert argv[argv.index("-rtsp_transport") + 1] == "tcp"
    assert argv[argv.index("-select_streams") + 1] == "v:0"
    entries = argv[argv.index("-show_entries") + 1]
    assert "width" in entries and "height" in entries
    assert argv[argv.index("-of") + 1] == "json"
    assert "rtsp://laptop:8554/live" in argv


def test_pixel_format_is_nv12():
    """The consumer advertises the NV12 pixel format so the push-frame
    pumps tell the renderer how to interpret the bytes."""
    consumer = StreamConsumer("rtsp://laptop:8554/live", 1920, 1080)
    assert consumer.pixel_format == "nv12"


@pytest.mark.asyncio
async def test_frame_size_is_nv12_at_source_dims(tmp_path):
    """The fixed-size frame read is src_w*src_h*3//2 (NV12), at the
    SOURCE dimensions ffprobe reported — not the renderer dims."""
    # Inject a 6x4 source; the consumer is constructed with renderer
    # dims 1920x1080 to prove the frame size tracks the source.
    src_w, src_h = 6, 4
    frame_size = src_w * src_h * 3 // 2  # = 36
    mock = _write_mock_ffmpeg(
        tmp_path / "ffmpeg", frame_size=frame_size, n_frames=3
    )
    consumer = StreamConsumer(
        "rtsp://host:8554/live", 1920, 1080,
        ffmpeg_bin=mock, source_size=(src_w, src_h),
    )

    frames = [f async for f in consumer.frames()]
    await consumer.close()

    assert len(frames) == 3
    assert all(len(f) == frame_size for f in frames)
    assert consumer.source_width == src_w
    assert consumer.source_height == src_h


@pytest.mark.asyncio
async def test_ffprobe_discovers_source_dims(tmp_path):
    """When no source_size is injected, frames() runs ffprobe to
    discover the source resolution; the mock ffprobe reports JSON
    dims and the consumer reads frames at that NV12 size."""
    src_w, src_h = 8, 6
    frame_size = src_w * src_h * 3 // 2

    probe = tmp_path / "ffprobe"
    probe_body = f"#!{sys.executable}\n"
    probe_body += "import sys, json\n"
    probe_body += (
        f"print(json.dumps({{'streams': [{{'width': {src_w}, "
        f"'height': {src_h}}}]}}))\n"
    )
    probe.write_text(probe_body)
    probe.chmod(probe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    mock = _write_mock_ffmpeg(
        tmp_path / "ffmpeg", frame_size=frame_size, n_frames=4
    )
    consumer = StreamConsumer(
        "rtsp://host:8554/live", 1920, 1080,
        ffmpeg_bin=mock, ffprobe_bin=str(probe),
    )

    frames = [f async for f in consumer.frames()]
    await consumer.close()

    assert len(frames) == 4
    assert all(len(f) == frame_size for f in frames)
    assert (consumer.source_width, consumer.source_height) == (src_w, src_h)


@pytest.mark.asyncio
async def test_ffprobe_failure_yields_no_frames(tmp_path):
    """A failed ffprobe (missing binary) surfaces as a clean no-frames
    exit — the caller's on-unreachable handling takes over, and ffmpeg
    is never spawned."""
    consumer = StreamConsumer(
        "rtsp://host:8554/live", 1920, 1080,
        ffmpeg_bin=str(tmp_path / "unused-ffmpeg"),
        ffprobe_bin=str(tmp_path / "does-not-exist-ffprobe"),
    )

    frames = [f async for f in consumer.frames()]
    await consumer.close()

    assert frames == []
    # ffprobe failed before ffmpeg could be spawned.
    assert consumer._proc is None


@pytest.mark.asyncio
async def test_ffprobe_rounds_odd_source_dims_up_to_even(tmp_path):
    """NV12 chroma is 4:2:0 — both axes must be even. An odd-dim
    source from ffprobe is rounded up so the fixed-size frame read
    cannot desync."""
    probe = tmp_path / "ffprobe"
    probe_body = f"#!{sys.executable}\n"
    probe_body += "import json\n"
    # Odd width + odd height.
    probe_body += "print(json.dumps({'streams': [{'width': 7, 'height': 5}]}))\n"
    probe.write_text(probe_body)
    probe.chmod(probe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # Don't spawn ffmpeg — point it at a non-existent binary so frames()
    # exits after the probe; we only assert the rounded source dims.
    consumer = StreamConsumer(
        "rtsp://host:8554/live", 1920, 1080,
        ffmpeg_bin=str(tmp_path / "unused-ffmpeg"),
        ffprobe_bin=str(probe),
    )

    [f async for f in consumer.frames()]
    await consumer.close()

    assert (consumer.source_width, consumer.source_height) == (8, 6)


# --- URL scheme allowlist (security) ---------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "rtsp://host:8554/live",
        "rtmp://host/app/stream",
        "rtmps://host/app/stream",
        "http://host/stream.m3u8",
        "https://host/stream.m3u8",
        "srt://host:9000",
        "udp://239.0.0.1:1234",
    ],
)
def test_validate_stream_url_accepts_allowed_schemes(url):
    """Every allowlisted stream transport (rtsp/rtmp/rtmps/http/https/
    srt/udp) passes validation without raising."""
    validate_stream_url(url)  # must not raise


def test_validate_stream_url_accepts_uppercase_scheme():
    """The scheme comparison is case-insensitive — an operator who
    types RTSP:// is not rejected."""
    validate_stream_url("RTSP://host:8554/live")  # must not raise


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "concat:in1.ts|in2.ts",
        "pipe:1",
        "subfile:start,end,,:secret",
        "data:text/plain;base64,SGVsbG8=",
        "/etc/passwd",  # bare path — no scheme at all
        "",  # empty string
        "gopher://host/1",  # bogus / unsupported scheme
    ],
)
def test_validate_stream_url_rejects_disallowed_schemes(url):
    """A non-stream scheme — the file-read / SSRF vectors and a bare
    path with no scheme — raises ValueError."""
    with pytest.raises(ValueError):
        validate_stream_url(url)


def test_validate_stream_url_error_names_scheme_and_allowlist():
    """The rejection message names the offending scheme and lists the
    allowed ones, so the operator can fix the URL."""
    with pytest.raises(ValueError) as excinfo:
        validate_stream_url("file:///etc/passwd")
    msg = str(excinfo.value)
    assert "file" in msg
    # The allowlist is surfaced so the operator knows what is valid.
    assert "rtsp" in msg and "https" in msg


def test_consumer_init_rejects_disallowed_url():
    """StreamConsumer.__init__ is the hard security boundary — a
    non-stream URL raises ValueError before any ffmpeg/ffprobe spawn."""
    with pytest.raises(ValueError):
        StreamConsumer("file:///etc/passwd", 1920, 1080)


# --- conditional -rtsp_transport -------------------------------------------


def test_rtsp_transport_present_for_rtsp_url():
    """For an rtsp:// URL, `-rtsp_transport tcp` is in BOTH the ffmpeg
    and the ffprobe argv — RTSP-over-TCP per §3."""
    consumer = StreamConsumer("rtsp://laptop:8554/live", 1920, 1080)

    argv = consumer._build_argv()
    assert argv[argv.index("-rtsp_transport") + 1] == "tcp"

    probe_argv = consumer._build_probe_argv()
    assert probe_argv[probe_argv.index("-rtsp_transport") + 1] == "tcp"


@pytest.mark.parametrize(
    "url",
    ["http://host/stream.m3u8", "srt://host:9000"],
)
def test_rtsp_transport_absent_for_non_rtsp_url(url):
    """`-rtsp_transport` is an RTSP-demuxer-private option — for a
    non-RTSP transport (HLS/HTTP, SRT) it is omitted from BOTH the
    ffmpeg and ffprobe argv so ffmpeg does not reject/warn on it."""
    consumer = StreamConsumer(url, 1920, 1080)

    assert "-rtsp_transport" not in consumer._build_argv()
    assert "-rtsp_transport" not in consumer._build_probe_argv()
    # The rest of the argv is unchanged — input URL + HW-decode flags.
    argv = consumer._build_argv()
    assert argv[argv.index("-c:v") + 1] == "h264_v4l2m2m"
    assert argv[argv.index("-i") + 1] == url
    assert argv[argv.index("-pix_fmt") + 1] == "nv12"
