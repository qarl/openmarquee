"""Slice 2 coverage for VlcRtspConsumer (STREAM/VLC arc §9).

VlcRtspConsumer spawns ffmpeg as a subprocess and yields raw RGB888
frames. These tests swap the `ffmpeg` binary for a tiny mock script
(emits a known number of fixed-size frames, writes to stderr, exits
or hangs) so the subprocess spawn / fixed-size frame read / stderr
capture / teardown machinery is exercised without a real ffmpeg or
RTSP server.
"""

from __future__ import annotations

import asyncio
import stat
import sys

import pytest

from openmarquee.vlc_rtsp_consumer import VlcRtspConsumer


def _write_mock_ffmpeg(
    path,
    *,
    frame_size: int,
    n_frames: int,
    trailing_partial: int = 0,
    stderr_text: str = "",
    exit_code: int = 0,
    hang: bool = False,
) -> str:
    """Write an executable mock-ffmpeg script and return its path.

    The mock ignores its argv (the real ffmpeg flags). It writes
    `stderr_text` to stderr, emits `n_frames` full frames of
    `frame_size` bytes (frame i filled with the byte value i % 256),
    optionally emits a `trailing_partial`-byte short final frame,
    then exits `exit_code` -- or hangs forever if `hang` is set, so a
    teardown test can verify close() reaps it.
    """
    body = f"#!{sys.executable}\n"
    body += "import sys, time\n"
    body += f"sys.stderr.write({stderr_text!r})\n"
    body += "sys.stderr.flush()\n"
    body += "out = sys.stdout.buffer\n"
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
    buffers, each width*height*3 bytes, in order. EOF (ffmpeg exit)
    ends the iteration cleanly."""
    frame_size = 8 * 8 * 3
    mock = _write_mock_ffmpeg(
        tmp_path / "ffmpeg", frame_size=frame_size, n_frames=5
    )
    consumer = VlcRtspConsumer("rtsp://host:8554/live", 8, 8, ffmpeg_bin=mock)

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
    frame_size = 8 * 8 * 3
    mock = _write_mock_ffmpeg(
        tmp_path / "ffmpeg",
        frame_size=frame_size,
        n_frames=3,
        trailing_partial=frame_size // 2,
    )
    consumer = VlcRtspConsumer("rtsp://host:8554/live", 8, 8, ffmpeg_bin=mock)

    frames = [f async for f in consumer.frames()]
    await consumer.close()

    assert len(frames) == 3
    assert all(len(f) == frame_size for f in frames)


# --- stderr capture --------------------------------------------------------


@pytest.mark.asyncio
async def test_stderr_is_captured(tmp_path):
    """ffmpeg's stderr is drained into the consumer's bounded tail
    buffer so a connect failure can be diagnosed."""
    frame_size = 4 * 4 * 3
    mock = _write_mock_ffmpeg(
        tmp_path / "ffmpeg",
        frame_size=frame_size,
        n_frames=2,
        stderr_text="rtsp: connection refused\n",
    )
    consumer = VlcRtspConsumer("rtsp://host:8554/live", 4, 4, ffmpeg_bin=mock)

    frames = [f async for f in consumer.frames()]
    await consumer.close()

    assert len(frames) == 2
    assert "connection refused" in consumer.stderr_tail


# --- teardown --------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_reaps_hanging_ffmpeg(tmp_path):
    """close() terminates a still-running ffmpeg: the subprocess is
    reaped (returncode set) and the frames() iterator unblocks."""
    frame_size = 8 * 8 * 3
    mock = _write_mock_ffmpeg(
        tmp_path / "ffmpeg", frame_size=frame_size, n_frames=2, hang=True
    )
    consumer = VlcRtspConsumer("rtsp://host:8554/live", 8, 8, ffmpeg_bin=mock)

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
    frame_size = 8 * 8 * 3
    mock = _write_mock_ffmpeg(
        tmp_path / "ffmpeg", frame_size=frame_size, n_frames=1, hang=True
    )
    consumer = VlcRtspConsumer("rtsp://host:8554/live", 8, 8, ffmpeg_bin=mock)

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
    frame_size = 4 * 4 * 3
    mock = _write_mock_ffmpeg(
        tmp_path / "ffmpeg",
        frame_size=frame_size,
        n_frames=1,
        stderr_text="rtsp: 404 stream not found\n",
        exit_code=3,
    )
    consumer = VlcRtspConsumer("rtsp://host:8554/live", 4, 4, ffmpeg_bin=mock)

    with caplog.at_level("WARNING", logger="openmarquee.vlc_rtsp_consumer"):
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
    consumer = VlcRtspConsumer(
        "rtsp://host:8554/live",
        8,
        8,
        ffmpeg_bin=str(tmp_path / "does-not-exist-ffmpeg"),
    )

    frames = [f async for f in consumer.frames()]
    await consumer.close()

    assert frames == []


@pytest.mark.asyncio
async def test_second_frames_call_yields_nothing(tmp_path):
    """The consumer is single-use: once frames() has run, a second
    call yields nothing rather than spawning a second ffmpeg."""
    frame_size = 4 * 4 * 3
    mock = _write_mock_ffmpeg(
        tmp_path / "ffmpeg", frame_size=frame_size, n_frames=2
    )
    consumer = VlcRtspConsumer("rtsp://host:8554/live", 4, 4, ffmpeg_bin=mock)

    first = [f async for f in consumer.frames()]
    second = [f async for f in consumer.frames()]
    await consumer.close()

    assert len(first) == 2
    assert second == []


# --- filter chain ----------------------------------------------------------


def test_argv_has_cover_fit_filter_at_renderer_dims():
    """The ffmpeg command applies the scale+crop cover-fit filter at
    the renderer's exact dimensions, drops audio, and reads the RTSP
    URL it was given."""
    consumer = VlcRtspConsumer("rtsp://laptop:8554/live", 1920, 1080)
    argv = consumer._build_argv()

    assert argv[0] == "ffmpeg"
    assert "-an" in argv  # audio dropped on ingest
    assert "rtsp://laptop:8554/live" in argv
    vf = argv[argv.index("-vf") + 1]
    assert "scale=1920:1080:force_original_aspect_ratio=increase" in vf
    assert "crop=1920:1080" in vf
    assert "format=rgb24" in vf
    # raw RGB888 out on stdout
    assert argv[-3:] == ["-f", "rawvideo", "-"]
