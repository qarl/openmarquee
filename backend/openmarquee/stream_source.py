"""Stream sources — pluggable frame producers for a takeover session.

A *takeover* preempts the playlist and renders frames from some live
source until the operator stops it. Phase 1 had exactly one source
(a phone camera over WebRTC) and `StreamSession` owned the decode
loop directly. The STREAM/VLC work adds a second source (an RTSP
stream pulled from VLC), so the decode mechanics are extracted here
behind a `StreamSource` Protocol that `StreamSession` drives
uniformly.

- `StreamSource` — the Protocol. A source owns one transport and
  yields RGB888 frames already sized to the renderer.
- `WebRtcStreamSource` — the Phase 1 source: an aiortc inbound video
  track, decoded and cover-fit-scaled. (`RtspStreamSource`, the VLC
  source, lands in a later slice and implements the same Protocol.)

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
from openmarquee.vlc_rtsp_consumer import VlcRtspConsumer

log = logging.getLogger(__name__)


@runtime_checkable
class StreamSource(Protocol):
    """A producer of renderer-sized RGB888 frames for a takeover.

    Each implementation owns one transport (a WebRTC video track, an
    RTSP-via-ffmpeg subprocess, ...) and yields frames already cover-
    fit-scaled to `renderer.width * renderer.height * 3` bytes, ready
    to hand straight to `Renderer.render_frame()`.

    `StreamSession` drives a source: it iterates `frames()` and pushes
    each frame to the renderer until the iterator is exhausted (the
    transport reached EOF or disconnected) or `close()` is called.
    """

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
    """

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


class RtspStreamSource:
    """`StreamSource` backed by a VlcRtspConsumer — the VLC takeover.

    Wraps the shared RTSP-via-ffmpeg consumer (the slice-2 module) so
    an operator-triggered VLC stream plugs into StreamSession exactly
    like the phone-camera WebRtcStreamSource. ffmpeg already cover-
    fits to the renderer dimensions, so `frames()` simply relays the
    consumer's output.

    The renderer dimensions are read once, at construction — the
    ffmpeg filter graph is fixed for the life of the subprocess, and
    a display mode does not change while a takeover is up (§3).
    """

    def __init__(self, renderer: Renderer, rtsp_url: str):
        self._consumer = VlcRtspConsumer(
            rtsp_url, renderer.width, renderer.height
        )

    def frames(self) -> AsyncIterator[bytes]:
        return self._consumer.frames()

    async def close(self) -> None:
        """Stop the source + reap ffmpeg. Idempotent."""
        await self._consumer.close()
