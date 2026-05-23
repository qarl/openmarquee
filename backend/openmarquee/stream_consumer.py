"""StreamConsumer — shared stream-via-ffmpeg frame source.

Both STREAM delivery modes (the operator-triggered takeover and the
playlist StreamSlide) pull video the same way: an ffmpeg subprocess
consumes a network stream URL. ffmpeg is protocol-agnostic, so the
URL may be any of the supported transports — RTSP, RTMP/RTMPS, HLS or
plain HTTP(S), SRT, or raw MPEG-TS over UDP.

## HW-decode (2026-05-20)

MEASURED on the dev Pi Zero 2 W (1280x704 30fps H.264, 300 frames):
ffmpeg's CPU `swscale` (the old `-vf scale,crop,format=rgb24` stage,
scaling to a 1080p panel) was the bottleneck at ~7fps. HW H.264
decode (`-c:v h264_v4l2m2m`) outputting raw NV12 runs ~26fps — a
~3.7x speedup that lands at essentially the 30fps source rate (the
decode-only ceiling on this Pi is ~29fps). So the consumer now:

  - HW-decodes the H.264 input (`-c:v h264_v4l2m2m`),
  - drops the `-vf` swscale filter entirely,
  - emits raw SOURCE-resolution NV12 (`-pix_fmt nv12 -f rawvideo`).

The renderer does the scale (cover-fit) + NV12→RGB on the GPU. NV12
is 1.5 bytes/px vs RGB888's 3 → the frame-pipe bandwidth halves.

Because the output is now source-resolution (not renderer-sized),
the consumer ffprobes the stream URL once at start to learn the
source `width`/`height` — that drives the fixed-size frame read. The
`renderer_w`/`renderer_h` passed at construction are retained only
for diagnostics / the RGB888-fallback shape; the cover-fit target is
the renderer's job.

`-rtsp_transport tcp` is an RTSP-demuxer-private option: it is added
to both the ffmpeg and ffprobe argv ONLY when the URL's scheme is
`rtsp`. For every other transport ffmpeg would reject or warn on the
flag, so it is omitted.

## URL scheme allowlist (security)

The stream URL is operator-supplied and passed straight as an
ffmpeg/ffprobe input argument. ffmpeg also honours `file://`,
`concat:`, `pipe:`, `subfile:`, `data:` and friends — a local-file
read / SSRF vector. `validate_stream_url` enforces a scheme allowlist
and is called from `StreamConsumer.__init__`, the hard security
boundary: no subprocess is ever spawned for a rejected URL.

`ffmpeg` + `ffprobe` are baked into the Pi image (the pi-gen package
list at `images/openmarquee/stage-openmarquee/00-install-packages/
00-packages`), so both binaries are expected on PATH at runtime.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from urllib.parse import urlparse

log = logging.getLogger(__name__)

#: Stream URL schemes ffmpeg may be pointed at. Everything outside
#: this set is rejected by `validate_stream_url` — in particular the
#: local-file / pipe / concat vectors (`file`, `pipe`, `concat`,
#: `subfile`, `data`) and a bare path with no scheme at all.
ALLOWED_STREAM_SCHEMES = frozenset({"rtsp", "rtmp", "rtmps", "http", "https", "srt", "udp"})


def validate_stream_url(url: str) -> str:
    """Raise `ValueError` if `url`'s scheme is not an allowed stream
    transport; otherwise return the cleaned (whitespace-stripped) URL.

    The stream URL is operator-supplied and is passed verbatim as an
    ffmpeg/ffprobe input argument. ffmpeg honours protocols well
    beyond network streams — `file://`, `concat:`, `pipe:`,
    `subfile:`, `data:` — which would turn an operator-typed URL into
    a local-file-read / SSRF primitive. This restricts the input to
    the network stream transports in `ALLOWED_STREAM_SCHEMES`.

    Leading/trailing whitespace is stripped up front: `urlparse`
    tolerates a leading space and still finds the scheme, so an
    unstripped URL would pass validation and then fail confusingly
    inside `ffmpeg -i`. The STRIPPED value is returned so callers
    persist (and spawn ffmpeg on) the cleaned form.

    The scheme comparison is case-insensitive. A URL with no scheme
    at all (a bare path like `/etc/passwd`) is rejected."""
    url = url.strip()
    scheme = urlparse(url).scheme.lower()
    if scheme not in ALLOWED_STREAM_SCHEMES:
        allowed = ", ".join(sorted(ALLOWED_STREAM_SCHEMES))
        shown = scheme if scheme else "(none)"
        raise ValueError(
            f"stream URL scheme {shown!r} is not allowed; the URL must use one of: {allowed}"
        )
    return url


# ffmpeg stderr is captured into a tail buffer trimmed to this many
# bytes after each read, so a long-lived consumer can't grow memory
# without bound; the tail is plenty to diagnose a connect failure or
# codec error.
_STDERR_TAIL_BYTES = 8192

# How long to wait for ffmpeg to exit after SIGTERM before escalating
# to SIGKILL.
_TERMINATE_GRACE_SECONDS = 2.0

# How long to wait for ffprobe to report the source stream dimensions
# before giving up. ffprobe connects to the stream URL and reads
# enough of the stream to parse the SPS; a few seconds is ample on a
# LAN, and an unreachable URL is bounded by the caller's
# connect-timeout anyway.
_FFPROBE_TIMEOUT_SECONDS = 8.0

# Renderer-hardening C2 (finding H2, 2026-05-21): the vc4 GPU's
# GL_MAX_TEXTURE_SIZE. The Pi's VideoCore IV caps a single 2D texture
# at 2048 px on either axis. The HW-decode path emits SOURCE-resolution
# NV12 and the renderer uploads each frame as a GL texture — so a 1440p
# or 4K stream would exceed the cap, `glTexImage2D` would fail
# GL_INVALID_VALUE, and the renderer would blit a black/garbage frame.
# When a probed source exceeds this, `_build_argv` re-introduces an
# ffmpeg `scale` filter to downscale the decoded frames to fit (the
# ONLY case swscale is used — a normal <=2048 stream is unfiltered).
_MAX_STREAM_DIM = 2048


def _clamp_to_texture_limit(width: int, height: int) -> tuple[int, int]:
    """Scale `(width, height)` down to fit within `_MAX_STREAM_DIM` on
    both axes, preserving aspect ratio; both results are rounded to even
    (NV12 4:2:0 chroma needs even dims).

    A source already within the cap is returned unchanged (still
    even-rounded — `_probe_source_size` already even-rounds, so this is
    a no-op for that path). A source over the cap is scaled by the
    single ratio that brings the larger over-cap axis to exactly the
    limit, so the result fits within `_MAX_STREAM_DIM` x `_MAX_STREAM_DIM`
    with aspect preserved.
    """
    if width <= _MAX_STREAM_DIM and height <= _MAX_STREAM_DIM:
        return (width + (width & 1), height + (height & 1))
    # Scale by the tighter ratio so BOTH axes land within the cap.
    scale = min(_MAX_STREAM_DIM / width, _MAX_STREAM_DIM / height)
    new_w = max(2, int(width * scale))
    new_h = max(2, int(height * scale))
    # NV12 needs even dims; round DOWN to even so neither axis can be
    # nudged back over the cap by the rounding.
    new_w -= new_w & 1
    new_h -= new_h & 1
    return (new_w, new_h)


class StreamConsumer:
    """Pulls a network video stream via ffmpeg and yields raw NV12
    frames.

    The input may be any ffmpeg-supported stream transport (RTSP,
    RTMP/RTMPS, HLS/HTTP(S), SRT, MPEG-TS over UDP). The URL scheme
    is allowlist-validated in `__init__` — a non-stream scheme
    (`file://`, `pipe:`, …) raises `ValueError` before any subprocess
    is spawned.

    Lifecycle: construct, `async for frame in consumer.frames()`,
    then `await consumer.close()`. `frames()` ffprobes the URL for
    the source resolution, spawns the ffmpeg subprocess on first
    iteration, and yields one `src_w * src_h * 3 // 2`-byte NV12
    buffer per decoded frame until ffmpeg exits (stream EOF /
    disconnect / spawn failure) or `close()` is called. Single-use —
    a second `frames()` call yields nothing.

    The emitted frames are SOURCE-resolution NV12; the renderer
    cover-fit-scales them onto the panel on the GPU. `pixel_format`,
    `source_width`, and `source_height` expose what `frames()`
    produces so the push-frame pumps can tell the renderer how to
    interpret the bytes (the `begin_external_frames` pixel_format +
    source dims).
    """

    #: The pixel format every `frames()` buffer is in. The HW-decode
    #: path always emits NV12; the attribute makes the format explicit
    #: to the push-frame pumps that drive the renderer.
    pixel_format = "nv12"

    def __init__(
        self,
        stream_url: str,
        width: int,
        height: int,
        *,
        ffmpeg_bin: str = "ffmpeg",
        ffprobe_bin: str = "ffprobe",
        source_size: tuple[int, int] | None = None,
    ):
        # Hard security boundary: reject a non-stream URL scheme
        # (file://, pipe:, concat:, …) before anything is spawned.
        # Both the playlist-slide path and the operator-takeover path
        # (FfmpegStreamSource) construct the consumer, so this single
        # check covers every way an operator URL reaches ffmpeg.
        # Spawn ffmpeg on the whitespace-stripped form it returns.
        self._stream_url = validate_stream_url(stream_url)
        # Renderer panel dims — retained for diagnostics only; the
        # cover-fit target is the renderer's job now.
        self._renderer_width = width
        self._renderer_height = height
        self._ffmpeg_bin = ffmpeg_bin
        self._ffprobe_bin = ffprobe_bin
        # Source dims: the CLAMPED `(width, height)` the renderer
        # actually receives — discovered via ffprobe in frames(), or
        # injected at construction (tests / a caller that already
        # probed). None until known. `source_width`/`source_height`/
        # `FfmpegStreamSource.frame_dims()` report this clamped pair.
        self._source_size: tuple[int, int] | None = (
            _clamp_to_texture_limit(*source_size) if source_size is not None else None
        )
        # Renderer-hardening C2 (finding H2): the RAW probed dims, before
        # the >2048 texture-limit clamp. `_build_argv` compares this to
        # `_source_size` to decide whether ffmpeg needs a downscale
        # `scale` filter. None until ffprobe runs (or until injected).
        self._raw_source_size: tuple[int, int] | None = source_size
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

    @property
    def source_width(self) -> int | None:
        """The decoded-frame width in pixels, once ffprobe has run
        (None before `frames()` discovers it). This is the texture-
        limit-CLAMPED width (finding H2) — what ffmpeg emits and the
        renderer receives, equal to the raw source width for a normal
        in-limit stream."""
        return self._source_size[0] if self._source_size else None

    @property
    def source_height(self) -> int | None:
        """The decoded-frame height in pixels, once ffprobe has run —
        the texture-limit-CLAMPED height (see `source_width`)."""
        return self._source_size[1] if self._source_size else None

    def _is_rtsp(self) -> bool:
        """True when the stream URL uses the `rtsp` scheme.

        `-rtsp_transport` is an RTSP-demuxer-private option; ffmpeg
        rejects or warns on it for any other transport. The argv
        builders gate the flag on this."""
        return urlparse(self._stream_url).scheme.lower() == "rtsp"

    def _build_probe_argv(self) -> list[str]:
        """The ffprobe command line that reports the source stream's
        width + height as JSON. `-rtsp_transport tcp` matches the
        ffmpeg ingest (§3) and is added ONLY for an RTSP URL — for any
        other transport the flag is omitted. `-select_streams v:0`
        picks the first video stream."""
        argv = [
            self._ffprobe_bin,
            "-loglevel",
            "error",
        ]
        if self._is_rtsp():
            argv += ["-rtsp_transport", "tcp"]
        argv += [
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            self._stream_url,
        ]
        return argv

    def _build_argv(self) -> list[str]:
        """The ffmpeg command line.

        HW-decode (2026-05-20): `-c:v h264_v4l2m2m` decodes the H.264
        input on the Pi's hardware codec (~26fps measured on the dev
        Pi Zero 2 W, vs ~7fps for the old chain). There is
        deliberately NO `-vf` filter — the old `scale,crop,format=
        rgb24` swscale chain was the measured bottleneck; the
        renderer does the cover-fit scale + NV12→RGB on the GPU
        instead. Output is raw source-resolution NV12.

        `-an` drops audio on ingest. `-rtsp_transport tcp` forces
        RTSP-over-TCP (§3) — UDP stutters badly on a Pi's wifi — and
        is added ONLY for an RTSP URL; it is an RTSP-demuxer-private
        option that ffmpeg rejects or warns on for any other
        transport (rtmp/hls/http/srt/mpegts). There is deliberately
        no ffmpeg-side connect timeout: an unreachable URL makes
        ffmpeg hang, and the caller bounds that (the playlist-slide
        path's connect-timeout, §9 slice 7) by calling close().

        Renderer-hardening C2 (finding H2, 2026-05-21): a `-vf scale`
        filter is added ONLY when the probed source exceeds the vc4
        GPU's 2048-px texture limit — it downscales the decoded frames
        to the clamped `_source_size` so the renderer's per-frame GL
        texture upload cannot fail GL_INVALID_VALUE (a silent black
        blit). A normal <=2048 stream gets NO `-vf` filter and pays no
        swscale cost — the renderer cover-fits on the GPU as before."""
        argv = [
            self._ffmpeg_bin,
            "-loglevel",
            "error",
            "-fflags",
            "nobuffer",
        ]
        if self._is_rtsp():
            argv += ["-rtsp_transport", "tcp"]
        argv += [
            "-c:v",
            "h264_v4l2m2m",
            "-i",
            self._stream_url,
            "-an",
        ]
        # Over-large source -> downscale to the texture-limit-clamped
        # dims. `_source_size` is the clamped pair; `_raw_source_size`
        # the raw probed pair. They differ only when the source exceeds
        # 2048 — otherwise no `-vf` is emitted and the path is unchanged.
        if (
            self._source_size is not None
            and self._raw_source_size is not None
            and self._source_size != self._raw_source_size
        ):
            scale_w, scale_h = self._source_size
            argv += ["-vf", f"scale={scale_w}:{scale_h}"]
        argv += [
            "-pix_fmt",
            "nv12",
            "-f",
            "rawvideo",
            "-",
        ]
        return argv

    async def _probe_source_size(
        self,
    ) -> tuple[tuple[int, int], tuple[int, int]] | None:
        """Run ffprobe to discover the source video's width + height.

        Returns a `(raw, clamped)` pair: `raw` is the source resolution
        ffprobe reported (even-rounded for NV12); `clamped` is `raw`
        scaled down to fit the vc4 GPU's 2048-px texture limit (finding
        H2 — equal to `raw` for a normal in-limit stream). Returns None
        if ffprobe is missing, times out, exits non-zero, or emits
        unparseable output — in which case `frames()` surfaces a clean
        no-frames exit (the caller's on-unreachable handling takes
        over)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._build_probe_argv(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            log.error("stream: failed to spawn ffprobe: %s", exc)
            return None
        # `proc` is now bound (the spawn succeeded). The finally below
        # reaps the ffprobe child on EVERY exit path — normal return,
        # timeout, parse error, AND a CancelledError propagating in
        # from frames() being cancelled mid-probe (a takeover teardown
        # or a playlist frames.aclose() landing in the probe window).
        # ffprobe is a LOCAL here, not tracked by self._proc, so the
        # finally is the only thing that prevents an orphan holding a
        # socket to the stream source.
        try:
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=_FFPROBE_TIMEOUT_SECONDS
                )
            except TimeoutError:
                # The finally below kills + reaps the timed-out ffprobe.
                log.error(
                    "stream: ffprobe timed out after %.1fs probing %s",
                    _FFPROBE_TIMEOUT_SECONDS,
                    self._stream_url,
                )
                return None
            if proc.returncode != 0:
                log.error(
                    "stream: ffprobe exited rc=%s: %s",
                    proc.returncode,
                    stderr.decode("utf-8", errors="replace").strip(),
                )
                return None
            try:
                payload = json.loads(stdout.decode("utf-8", errors="replace"))
                stream = payload["streams"][0]
                w = int(stream["width"])
                h = int(stream["height"])
            except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
                log.error(
                    "stream: ffprobe output unparseable (%s): %r",
                    exc,
                    stdout[:256],
                )
                return None
            if w <= 0 or h <= 0:
                log.error("stream: ffprobe reported non-positive dims %dx%d", w, h)
                return None
            # NV12 chroma is 4:2:0 — both axes must be even. ffmpeg's
            # NV12 output always pads to even dims; round up defensively
            # so the fixed-size frame read can't desync on an odd-dim
            # source.
            w += w & 1
            h += h & 1
            raw = (w, h)
            # Renderer-hardening C2 (finding H2): clamp the raw dims to
            # the vc4 2048-px texture limit. `clamped == raw` for a
            # normal in-limit stream; over-large -> aspect-preserving
            # downscale (and `_build_argv` adds the `scale` filter).
            clamped = _clamp_to_texture_limit(*raw)
            if clamped != raw:
                log.warning(
                    "stream: source %dx%d exceeds the %dpx GPU texture limit; downscaling to %dx%d",
                    raw[0],
                    raw[1],
                    _MAX_STREAM_DIM,
                    clamped[0],
                    clamped[1],
                )
            return (raw, clamped)
        finally:
            # Reap the ffprobe child on every exit path. After a normal
            # communicate() the proc has already exited (returncode
            # set) — the guard skips the kill. On a timeout / error /
            # cancellation it is still alive: kill + wait so no orphan
            # ffprobe (each holds a socket to the stream source) is
            # left behind.
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()

    async def frames(self) -> AsyncIterator[bytes]:
        """Spawn ffmpeg and yield NV12 frames until the stream ends
        or `close()` is called.

        First ffprobes the URL for the source resolution (unless it
        was injected at construction). A probe failure or an ffmpeg
        spawn failure is logged and surfaced as a clean no-frames exit
        — the caller's timeout / on-unreachable handling takes over."""
        if self._closed or self._proc is not None:
            return
        if self._source_size is None:
            probed = await self._probe_source_size()
            if probed is None:
                # ffprobe could not determine the source size — bail
                # cleanly so the caller's on-unreachable path runs.
                return
            # `_probe_source_size` returns (raw, clamped). `_source_size`
            # is the CLAMPED pair the renderer receives; `_build_argv`
            # reads `_raw_source_size` to decide on a downscale filter.
            self._raw_source_size, self._source_size = probed
        if self._closed:
            return
        # The fixed-size frame read tracks the CLAMPED dims — what ffmpeg
        # actually emits (a `scale` filter is in the argv when the source
        # was over the 2048-px texture limit).
        src_w, src_h = self._source_size
        # NV12: Y plane (src_w*src_h) + interleaved UV plane at half
        # res on both axes (src_w*src_h/2) = 1.5 bytes/px total.
        frame_size = src_w * src_h * 3 // 2
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
                log.error("stream: failed to spawn ffmpeg: %s", exc)
                return
            assert self._proc.stdout is not None
            assert self._proc.stderr is not None
            self._stderr_task = asyncio.create_task(self._drain_stderr(self._proc.stderr))
        try:
            while not self._closed:
                # readexactly enforces the NV12 frame size: a short
                # final read (ffmpeg exiting mid-frame) raises
                # IncompleteReadError and the partial frame is dropped.
                frame = await self._proc.stdout.readexactly(frame_size)
                yield frame
        except asyncio.IncompleteReadError:
            if self._closed:
                log.info("stream: stream stopped (consumer closed)")
            else:
                log.info("stream: stream ended (EOF / disconnect)")
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
            log.exception("stream: stderr drain error")

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
                    await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_GRACE_SECONDS)
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
                    "stream: ffmpeg exited rc=%s; stderr tail: %s",
                    rc,
                    self.stderr_tail,
                )

    async def close(self) -> None:
        """Stop the consumer and reap ffmpeg. Idempotent; safe to call
        from a task other than the one iterating `frames()`."""
        self._closed = True
        await self._reap()
