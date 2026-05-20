"""VlcRtspConsumer — shared RTSP-via-ffmpeg frame source.

Both STREAM/VLC delivery modes (the operator-triggered takeover and
the playlist VlcStreamSlide) pull video the same way: an ffmpeg
subprocess consumes an RTSP URL that VLC is publishing and emits raw
RGB888 frames already cover-fit-scaled to the renderer. This module
owns exactly that mechanism — subprocess spawn, the filter chain,
fixed-size frame reads, stderr capture, teardown. It has no awareness
of takeover-vs-slide lifecycle; the two modes wrap it (slice 4 and
slice 7 of docs/STREAM_VLC_PROPOSAL.md §9).

`ffmpeg` is baked into the Pi image (the pi-gen package list at
`images/openmarquee/stage-openmarquee/00-install-packages/00-packages`),
so the binary is expected on PATH at runtime.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

log = logging.getLogger(__name__)

# ffmpeg stderr is captured into a tail buffer trimmed to this many
# bytes after each read, so a long-lived consumer can't grow memory
# without bound; the tail is plenty to diagnose a connect failure or
# codec error.
_STDERR_TAIL_BYTES = 8192

# How long to wait for ffmpeg to exit after SIGTERM before escalating
# to SIGKILL.
_TERMINATE_GRACE_SECONDS = 2.0


class VlcRtspConsumer:
    """Pulls an RTSP stream via ffmpeg and yields renderer-sized
    RGB888 frames.

    Lifecycle: construct, `async for frame in consumer.frames()`,
    then `await consumer.close()`. `frames()` spawns the ffmpeg
    subprocess on first iteration and yields one
    `width * height * 3`-byte buffer per decoded frame until ffmpeg
    exits (RTSP EOF / disconnect / spawn failure) or `close()` is
    called. Single-use — a second `frames()` call yields nothing.
    """

    def __init__(
        self,
        rtsp_url: str,
        width: int,
        height: int,
        *,
        ffmpeg_bin: str = "ffmpeg",
    ):
        self._rtsp_url = rtsp_url
        self._width = width
        self._height = height
        self._ffmpeg_bin = ffmpeg_bin
        self._frame_size = width * height * 3
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail = bytearray()
        self._reap_lock = asyncio.Lock()
        self._reaped = False
        self._closed = False

    @property
    def stderr_tail(self) -> str:
        """The tail of ffmpeg's stderr, decoded — for diagnostics."""
        return self._stderr_tail.decode("utf-8", errors="replace")

    def _build_argv(self) -> list[str]:
        """The ffmpeg command line. `scale` (with
        force_original_aspect_ratio=increase) + `crop` together
        implement cover-fit inside ffmpeg's filter graph, so there is
        no Python-side PIL roundtrip per frame. `-an` drops audio on
        ingest. `-rtsp_transport tcp` forces RTSP-over-TCP (§3) —
        UDP stutters badly on a Pi's wifi. There is deliberately no
        ffmpeg-side connect timeout: an unreachable URL makes ffmpeg
        hang, and the caller bounds that (the playlist-slide path's
        connect-timeout, §9 slice 7) by calling close()."""
        vf = (
            f"scale={self._width}:{self._height}"
            ":force_original_aspect_ratio=increase,"
            f"crop={self._width}:{self._height},"
            "format=rgb24"
        )
        return [
            self._ffmpeg_bin,
            "-loglevel", "error",
            "-fflags", "nobuffer",
            "-rtsp_transport", "tcp",
            "-i", self._rtsp_url,
            "-an",
            "-vf", vf,
            "-f", "rawvideo",
            "-",
        ]

    async def frames(self) -> AsyncIterator[bytes]:
        """Spawn ffmpeg and yield RGB888 frames until the stream ends
        or `close()` is called. A spawn failure (ffmpeg missing) is
        logged and surfaced as a clean no-frames exit — the caller's
        timeout / on-unreachable handling takes over."""
        if self._closed or self._proc is not None:
            return
        # The spawn-and-assign runs under _reap_lock so a close()
        # racing in during the create_subprocess_exec await cannot
        # reap-then-miss the not-yet-assigned subprocess (which would
        # orphan ffmpeg). close()'s _reap() blocks on the lock until
        # self._proc is set, then terminates it.
        async with self._reap_lock:
            if self._closed:
                return
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *self._build_argv(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except (OSError, ValueError) as exc:
                log.error("vlc_rtsp: failed to spawn ffmpeg: %s", exc)
                return
            assert self._proc.stdout is not None
            assert self._proc.stderr is not None
            self._stderr_task = asyncio.create_task(
                self._drain_stderr(self._proc.stderr)
            )
        try:
            while not self._closed:
                # readexactly enforces the RGB888 frame size: a short
                # final read (ffmpeg exiting mid-frame) raises
                # IncompleteReadError and the partial frame is dropped.
                frame = await self._proc.stdout.readexactly(self._frame_size)
                yield frame
        except asyncio.IncompleteReadError:
            if self._closed:
                log.info("vlc_rtsp: stream stopped (consumer closed)")
            else:
                log.info("vlc_rtsp: stream ended (RTSP EOF / disconnect)")
        except asyncio.CancelledError:
            raise
        finally:
            await self._reap()

    async def _drain_stderr(self, stderr: asyncio.StreamReader) -> None:
        """Pump ffmpeg's stderr into the bounded tail buffer."""
        try:
            while True:
                chunk = await stderr.read(4096)
                if not chunk:
                    break
                self._stderr_tail.extend(chunk)
                if len(self._stderr_tail) > _STDERR_TAIL_BYTES:
                    del self._stderr_tail[:-_STDERR_TAIL_BYTES]
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("vlc_rtsp: stderr drain error")

    async def _reap(self) -> None:
        """Terminate + await the ffmpeg subprocess and the stderr
        drainer. Idempotent — safe to call from both frames()'s
        finally and close()."""
        async with self._reap_lock:
            if self._reaped:
                return
            self._reaped = True
            proc = self._proc
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                try:
                    await asyncio.wait_for(
                        proc.wait(), timeout=_TERMINATE_GRACE_SECONDS
                    )
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    await proc.wait()
            # ffmpeg has exited (or was never spawned); its stderr
            # pipe is EOF'd, so the drainer finishes near-instantly.
            # Await it (bounded) so the final stderr is captured
            # before we log any failure tail — cancelling it outright
            # could drop the very bytes that explain the failure.
            if self._stderr_task is not None and not self._stderr_task.done():
                # wait_for cancels the drain task on timeout; suppress
                # only that. A CancelledError targeting _reap() itself
                # must still propagate (structured cancellation).
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stderr_task, timeout=1.0)
            rc = proc.returncode if proc is not None else None
            if rc not in (None, 0) and self._stderr_tail:
                log.warning(
                    "vlc_rtsp: ffmpeg exited rc=%s; stderr tail: %s",
                    rc,
                    self.stderr_tail,
                )

    async def close(self) -> None:
        """Stop the consumer and reap ffmpeg. Idempotent; safe to call
        from a task other than the one iterating `frames()`."""
        self._closed = True
        await self._reap()
