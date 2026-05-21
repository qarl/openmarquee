"""Stream sources — pluggable frame producers for a takeover session.

A *takeover* preempts the playlist and renders frames from some live
source until the operator stops it. Phase 1 had exactly one source
(a phone camera over WebRTC) and `StreamSession` owned the decode
loop directly. The STREAM/VLC work adds a second source (an ffmpeg-
pulled network stream), so the decode mechanics are extracted here
behind a `StreamSource` Protocol that `StreamSession` drives
uniformly.

- `StreamSource` — the Protocol. A source owns one transport and
  yields RGB888 frames already sized to the renderer.
- `WebRtcStreamSource` — the Phase 1 source: an aiortc inbound video
  track, decoded and cover-fit-scaled. (`FfmpegStreamSource`, the
  ffmpeg-backed source, implements the same Protocol.)

`_cover_fit` lives here too — it is a stream-only helper (the slide
render path does its cover-fit browser-side; `seed.py` keeps its own
copy for thumbnail generation).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from PIL import Image

from openmarquee.rendering import Renderer
from openmarquee.stream_consumer import StreamConsumer

log = logging.getLogger(__name__)


@runtime_checkable
class StreamSource(Protocol):
    """A producer of frames for a takeover.

    Each implementation owns one transport (a WebRTC video track, an
    ffmpeg subprocess pulling a network stream, ...) and yields frames
    ready to hand to `Renderer.render_frame()`.

    HW-decode (2026-05-20): a source declares its `pixel_format`. An
    RGB888 source (`WebRtcStreamSource`) yields renderer-sized RGB888
    frames; an NV12 source (`FfmpegStreamSource`) yields source-
    resolution NV12 and exposes `frame_dims()` so the pump can tell
    the renderer the source size for its GPU cover-fit. `StreamSession`
    threads the format + dims into `render_frame()`.

    `StreamSession` drives a source: it iterates `frames()` and pushes
    each frame to the renderer until the iterator is exhausted (the
    transport reached EOF or disconnected) or `close()` is called.
    """

    #: Pixel format of every `frames()` buffer — "rgb888" or "nv12".
    pixel_format: str

    def frames(self) -> AsyncIterator[bytes]:
        """Yield RGB888 frames until the transport ends or `close()`
        is called. Per-frame decode errors are swallowed (one bad
        frame must not end the stream); the iterator simply stops
        when the transport is done."""
        ...

    async def close(self) -> None:
        """Tear down the transport. Idempotent; safe to call from a
        task other than the one iterating `frames()`."""
        ...


def _cover_fit(image: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Scale `image` to cover (`target_w`, `target_h`) and center-crop.

    Preserves the source aspect — the larger dimension is resized up or
    down to exactly match the target, and the overflow on the other axis
    is cropped evenly on both sides. Mirrors the browser-side editor
    previews so what the operator sees IS what the device renders.

    Used by the stream takeover path to fit incoming frames onto the
    renderer's dimensions.
    """
    src_w, src_h = image.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w = max(1, round(src_w * scale))
    new_h = max(1, round(src_h * scale))
    # LANCZOS is the slower-but-sharper resample; for a one-shot render
    # the ~10-15ms cost on a Pi Zero 2 W is invisible behind the
    # transition.
    resized = image.resize((new_w, new_h), resample=Image.Resampling.LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


class WebRtcStreamSource:
    """`StreamSource` backed by an aiortc inbound video track.

    The track is not available at construction — it arrives via the
    RTCPeerConnection's `on_track` event after SDP negotiation. The
    owner calls `set_track()` from that callback; `frames()` blocks
    until the track is set, then decodes + cover-fits each frame.

    HW-decode (2026-05-20): the WebRTC path decodes + cover-fits
    Python-side and yields renderer-sized RGB888 — `pixel_format` is
    "rgb888", the `render_frame()` default. (The HW-decode arc only
    moves the ffmpeg stream path to GPU-side NV12; WebRTC is unchanged.)
    """

    #: WebRTC frames are decoded + cover-fit Python-side to RGB888.
    pixel_format = "rgb888"

    def __init__(self, renderer: Renderer):
        self._renderer = renderer
        self._track = None  # aiortc MediaStreamTrack, set via set_track
        self._track_ready = asyncio.Event()
        self._closed = False

    def set_track(self, track) -> None:  # noqa: ANN001 — aiortc track
        """Hand the inbound video track to the source. Called once,
        from the peer connection's `on_track` callback."""
        self._track = track
        self._track_ready.set()

    async def frames(self) -> AsyncIterator[bytes]:
        # Wait for on_track to deliver the track. If the session is
        # closed before any track arrives, close() sets _track_ready
        # so this wait unblocks and the generator exits cleanly.
        await self._track_ready.wait()
        if self._closed or self._track is None:
            return
        track = self._track
        renderer = self._renderer
        try:
            while not self._closed:
                frame = await track.recv()  # av.VideoFrame
                # Renderer dims are re-read each frame: a mode change
                # mid-stream is rare but must not desync the cover-fit.
                target_w = renderer.width
                target_h = renderer.height
                try:
                    rgb = frame.to_ndarray(format="rgb24")
                    pil = Image.fromarray(rgb)
                    if pil.size != (target_w, target_h):
                        pil = _cover_fit(pil, target_w, target_h)
                    frame_bytes = pil.tobytes()
                except Exception:
                    log.exception("stream: dropped frame")
                    continue
                yield frame_bytes
        except asyncio.CancelledError:
            raise
        except Exception:
            # Track ended (MediaStreamError) or aiortc raised on recv.
            # The generator just returns here; actual teardown (PC
            # close + playback resume) happens when StreamSession.
            # close() is called by the operator / a takeover /
            # shutdown -- track-end alone does not trigger it (same
            # as the pre-refactor _consume_video path).
            log.info("stream: video track consumer exiting")

    async def close(self) -> None:
        """Stop the source. Idempotent."""
        self._closed = True
        # Unblock frames() if it is still waiting for a track that
        # will now never arrive.
        self._track_ready.set()


class FfmpegStreamSource:
    """`StreamSource` backed by a StreamConsumer — the ffmpeg takeover.

    Wraps the shared ffmpeg stream consumer (the slice-2 module) so
    an operator-triggered network stream plugs into StreamSession
    exactly like the phone-camera WebRtcStreamSource.

    HW-decode (2026-05-20): the consumer HW-decodes H.264 and emits
    SOURCE-resolution NV12 (no ffmpeg swscale — the GPU does the
    cover-fit). So this source yields NV12 frames, NOT renderer-sized
    RGB888. `pixel_format` advertises "nv12"; `frame_dims()` reports
    the source resolution the consumer discovered via ffprobe — the
    stream pump threads both into `renderer.render_frame()` so the
    sidecar knows how to interpret the bytes. (WebRtcStreamSource
    stays RGB888 — the format tag per-source is the whole point.)

    The renderer dimensions are read once, at construction; the
    consumer retains them for diagnostics.
    """

    #: This source produces NV12 frames (the consumer's HW-decode
    #: output). The stream pump reads this to set begin_external_
    #: frames' pixel_format.
    pixel_format = "nv12"

    def __init__(self, renderer: Renderer, stream_url: str):
        self._consumer = StreamConsumer(
            stream_url, renderer.width, renderer.height
        )

    def frames(self) -> AsyncIterator[bytes]:
        return self._consumer.frames()

    def frame_dims(self) -> tuple[int, int] | None:
        """The decoded-frame `(width, height)` once the consumer's
        ffprobe has run (None before the first `frames()` frame).

        For an NV12 source these are the dims the renderer needs in
        `begin_external_frames` — the renderer cover-fit-scales the
        source onto its panel.

        Renderer-hardening C2 (finding H2, 2026-05-21): these are the
        CLAMPED dims — what ffmpeg actually emits. A source exceeding
        the vc4 GPU's 2048-px texture limit is downscaled by an ffmpeg
        `scale` filter; `frame_dims` then reports the downscaled size so
        the renderer's per-frame GL texture upload never exceeds the
        cap. A normal in-limit stream reports the raw source dims."""
        w = self._consumer.source_width
        h = self._consumer.source_height
        if w is None or h is None:
            return None
        return (w, h)

    async def close(self) -> None:
        """Stop the source + reap ffmpeg. Idempotent."""
        await self._consumer.close()
