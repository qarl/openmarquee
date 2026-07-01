"""Renderers — how frames get from the playback engine to a physical (or virtual) display.

The active production path is the Rust IPC sidecar (`rust_renderer.py`):
the playback loop drives `openmarquee-render --ipc-sidecar` as a
subprocess, sends typed ops over stdin/stdout JSON-lines, and the
sidecar paints frames via EGL + DRM/KMS on the Pi.

- `RustRenderer` — IPC proxy. The production renderer.
- `MockRenderer` — IPC-shaped stub for tests + the dev-host fallback
  branch of `AutoFallbackRenderer`. No pixels are painted in Python.
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Renderer(Protocol):
    """Common interface all renderers conform to.

    Implementations may expose richer methods (start/stop, brightness, etc.) but
    every renderer must at minimum be able to accept a raw RGB frame of known
    dimensions.
    """

    width: int
    height: int

    def render_frame(
        self,
        frame: bytes,
        *,
        pixel_format: str = "rgb888",
        frame_w: int | None = None,
        frame_h: int | None = None,
    ) -> None:
        """Render one externally-produced frame.

        `pixel_format` is declared once per push run (one producer =
        one format):

        - `"rgb888"` (default): `frame` is row-major RGB888,
          `width * height * 3` bytes at the renderer dims, top-left
          pixel first, channel order R, G, B. `frame_w`/`frame_h` are
          ignored. Omitting the new kwargs keeps existing RGB888
          producers unchanged.
        - `"nv12"`: `frame` is planar NV12, `frame_w * frame_h * 3 //
          2` bytes at the SOURCE video dims; `frame_w`/`frame_h` are
          required. The renderer cover-fit-scales onto its panel.
        """
        ...

    def end_external_frames(self) -> None:
        """End a run of pushed `render_frame` frames.

        A renderer that forwards pushed frames to an out-of-process
        sidecar (`RustRenderer`) uses this to close the frame-pump
        session; in-process renderers (`MockRenderer`) treat it as a
        no-op. Idempotent. Callers that push frames — the stream
        takeover pump and the `StreamSlide` playback pump — MUST
        call this on every exit path of a `render_frame` run.
        """
        ...

    def reopen(self) -> None:
        """Restart the renderer so it picks up open-time configuration.

        FYS bug 5: display rotation is applied at the sidecar's Open;
        a rotation change drives a renderer reopen. `RustRenderer`
        restarts its subprocess + re-Opens; in-process renderers
        (`MockRenderer`) treat it as a no-op. The caller (api_settings)
        STOPS the playback loop around this call, so nothing else is
        driving the renderer; the loop replays per-slide state when it
        restarts.
        """
        ...

    def render_system_card(self, params: dict[str, Any]) -> None:
        """PR3 (2026-06-27) — display a full-screen onboarding system
        card overlay per spec §"The display is the onboarding UI".

        `params` maps 1:1 to the Rust IpcRequest::RenderSystemCard
        variant (see `renderer/src/playback.rs` :: RenderSystemCardParams):
            {
              "kind": "SETUP" | "CONNECTING" | "CONNECTED" | "DEGRADED" | "BOOT",
              "ssid": str | None,
              "pin": str | None,
              "qr_payload": str | None,   # WIFI:T:WPA;S:...;P:...;;
              "address": str | None,      # mDNS name for CONNECTED / BOOT
              "ip": str | None,
              "target_ssid": str | None,  # CONNECTING / DEGRADED
              "variant": "lost" | "auth_fail" | "not_found_or_5ghz" | None,
              "ttl_ms": int | None,       # None / 0 = until-state-change
              "boot_hint": str | None,    # PR4 rapid-boot line
            }

        Overlays the playlist paint until either `ttl_ms` elapses,
        `clear_system_card` is called, or another `render_system_card`
        replaces the active card. Idempotent: calling with the same
        params is a re-paint (renderer resets its ttl).
        """
        ...

    def clear_system_card(self) -> None:
        """PR3 (2026-06-27) — clear any active system card + return to
        normal playlist paint. Supervisor sends this on ONLINE.
        Idempotent — clearing when no card is active is a no-op.
        """
        ...
