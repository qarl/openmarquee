# STREAM / VLC — design proposal (scoping pass)

Scoping doc for adding a VLC-driven video stream alongside the
existing Phase 1 phone-camera takeover (`backend/openmarquee/stream.py`,
WebRTC over aiortc). Per qarl 2026-05-19: "stream should allow
streaming a video with playback controls (in addition to camera)
including from VLC."

This is a **scoping pass only** — no code edits in this round. The
goal is to land a transport choice, integration shape, and slice
plan that QA + qarl can review before implementation starts.

## 1. Current state recap

Phase 1 live stream (per `backend/openmarquee/stream.py`,
`api_stream.py`, `ui/src/stream-panel.js`) supports a single phone-
camera publisher over WebRTC. The phone calls `getUserMedia` with
`{video: {facingMode: "environment"}, audio: false}`, builds an SDP
offer, posts to `POST /api/stream/start`, and the Pi-side
`StreamManager` (process-singleton) creates a `StreamSession`
wrapping an `RTCPeerConnection`. Inbound `av.VideoFrame`s flow
through `frame.to_ndarray("rgb24") → PIL.Image → _cover_fit(w, h) →
renderer.render_frame(rgb_bytes)`. The playback loop is `pause()`d
during the session and `resume()`d on close.

Two abstractions already present that the VLC path can ride on:

- **`Renderer.render_frame(frame: bytes)`** in
  `backend/openmarquee/rendering/__init__.py` is the single point
  where any source pushes a row-major RGB888 buffer
  (`renderer.width * renderer.height * 3` bytes — production dims
  are 1920×1080, NOT the tier cap) into the rendering pipeline.
  Mock + Rust renderers both honor it. Renderer dims are mutable
  on HDMI mode change; long-lived sources need to handle a
  width/height re-read or rebuild on mode-renegotiation (out of
  scope for v1 — modes don't change while a stream is up — but
  worth flagging).
- **`PlaybackLoop.pause()` / `resume()`** (in
  `backend/openmarquee/playback.py`, NOT on `StreamManager`) is
  the playback-loop preempt+restore primitive. Phase 1's
  `StreamSession.start()` already calls `self._playback.pause()`
  and `close()` calls `self._playback.resume()`; the same
  affordance is reused by any new source. `PlaybackLoop.renderer`
  is also already exposed for cross-process renderer sharing.

Tier system is a static `_BASIC_TIER` constant
(`api_stream.py:99`, 854×480 @ 30fps). The phone reads `/status` at
mount time and clamps `getUserMedia` constraints. The same struct
extends naturally to other sources.

## 2. Recommended transport — RTSP (pull from VLC's built-in
server)

**Pick: RTSP, with the Pi pulling from the VLC host.** Operator
runs VLC with `--sout '#rtsp{...}'` on their laptop; VLC's
built-in RTSP server publishes a URL like `rtsp://<operator-host>:
8554/live`; the operator pastes that URL into the openMarquee
dashboard; the Pi spawns an ffmpeg subprocess to consume it and
pipe decoded RGB to `renderer.render_frame()`.

Why RTSP wins:

- VLC's RTSP server mode is well-documented and is the canonical
  "VLC publishes a stream" path operators already know. No
  separate RTMP server, no HLS publisher, no manifest plumbing.
- ffmpeg can consume RTSP directly (`-i rtsp://...`); ffmpeg is
  already on the Pi for the video-slide path (per
  `[[project_pi_rust_binary_path]]` + the existing video-decode
  plumbing in `renderer/src/v4l2.rs`).
- LAN- and Tailscale-friendly: Pi initiates the connection out to
  the operator's host, so the operator doesn't need to expose a
  port to the public internet. Tailnet is the WAN story (no cloud
  per project policy).

Alternatives considered:

- **RTMP**: requires running an RTMP ingest server (nginx-rtmp,
  Janus, MediaMTX). Extra moving part. Loses.
- **UDP-TS / multicast**: VLC supports it, but multicast routing
  is finicky on home LANs and not on Tailscale. Loses.
- **HLS**: works in either direction (Pi serves HLS for operator
  to pull, or VLC emits HLS as files and Pi pulls). Adds
  segmenter latency (5-30s typical) and either an HTTP-server
  process on the Pi or a file-watch dance. Loses on latency
  alone — RTSP is sub-second.
- **SRT**: VLC 4.x supports `--sout '#srt'` and ffmpeg consumes
  `srt://...`. Better congestion behavior than RTSP-over-TCP on
  lossy Wi-Fi. Loses to RTSP on operator-familiarity — RTSP shows
  up as a first-class destination in VLC's Stream wizard; SRT is
  hidden behind a custom-sout text field. Revisit if Slice F
  live-fire shows lossy-network frame drops on RTSP-over-TCP.
- **Plain TCP MPEG-TS**: `--sout '#std{access=tcp,...}'` + `-i
  tcp://...`. Simpler wire format (no SDP). Loses because VLC's
  Stream wizard doesn't list it; operators would need to know the
  custom-sout incantation. RTSP wins on UI surface.

## 3. Pi-side consume path — ffmpeg subprocess

**Pick: ffmpeg subprocess with stdout-piped raw RGB24, scaled+
cover-fit to `renderer.width × renderer.height` by ffmpeg's
filter chain so the consumer reads fixed-size frames.** Command
shape, with the renderer dims (production 1920×1080) interpolated
in at session start:

```
ffmpeg -loglevel error -fflags nobuffer \
       -i rtsp://<source>:8554/live \
       -an \
       -vf "scale=<W>:<H>:force_original_aspect_ratio=increase, \
            crop=<W>:<H>, format=rgb24" \
       -f rawvideo -
```

`scale + crop` together implement cover-fit (the same semantics
`_cover_fit` gives the WebRTC path via PIL). Doing it in ffmpeg's
filter graph avoids the Python-side memcpy + PIL roundtrip on
every frame; the consumer just reads `renderer.width *
renderer.height * 3` bytes off stdout and hands them straight to
`renderer.render_frame()`. **The tier cap (854×480, etc.) governs
ffmpeg's network-side decode workload — not the output buffer
size.** Decode at 480p keeps CPU low; the scale-up to renderer
dims happens once per frame in ffmpeg's swscale (cheap).

The backend treats stdout-EOF / subprocess-exit as "stream
ended" and runs the same close-path as Phase 1
(`StreamSession.close()` → `PlaybackLoop.resume()`).

**Where cover-fit lives** (Slice A design call): in the
`RtspStreamSource`, via ffmpeg's filter chain. Alternative: yield
arbitrary-dim buffers and let the consumer cover-fit via PIL like
the WebRTC path does today. Filter-chain wins on per-frame cost
(zero Python work between bytes and renderer) and on coupling
(source owns its scale chain, consumer is a dumb pipe). The
WebRTC path can opt into the same shape later if profiling shows
the PIL roundtrip costs.

Why not aiortc: it's WebRTC-only. RTSP is out of scope for aiortc.
Why not GStreamer: bigger install footprint (~80 MB pulled deps)
and more API surface; ffmpeg is enough.

**ffmpeg-binary availability check before Slice B starts:**
`renderer/src/v4l2.rs` + `renderer/src/mp4_demux.rs` are
Rust-side bcm2835 V4L2 hardware decode for video slides — they do
NOT depend on the system `ffmpeg` binary. The aiortc path uses
PyAV (libav-the-library), also not `ffmpeg`-the-binary. So the
Pi base image may or may not have `ffmpeg` installed. If absent,
add `apt install ffmpeg` to the pi-gen recipe — folds into the
front of Slice B as a one-line packaging change.

**Cost:** Pi 4/5 has comfortable headroom for SW H.264 decode at
1080p/30. Pi Zero 2 W is **the live-fire question Slice F
answers**; if the basic tier doesn't hold at 480p/30 the constant
drops to 360p/30 in one line of `api_stream.py`. Note that the
aiortc path proving 480p/30 works does NOT directly transfer —
aiortc's typical codec answer is VP8 (cheaper than H.264) and
aiortc runs libav in-process (no subprocess-pipe overhead). The
~36 MB/s of stdout-piped raw RGB is new IPC traffic that Slice F
needs to measure on hardware.

RAM per session: one frame buffer (renderer.width × renderer.height
× 3 bytes ≈ 6 MB at 1080p, 1.2 MB at 480p). Disk: zero —
stdout-only, no caching.

## 4. Playback control surface — phase (a) only

**Pick phase (a): VLC-side controls only.** Operator drives play /
pause / seek / volume from the VLC UI on their laptop. Pi is a
dumb consumer — frames stop arriving when the operator hits Pause
in VLC; Pi just renders whatever the latest frame was.

This is the simplest start and matches qarl's framing ("streaming
a video with playback controls" — the controls are VLC's). Disk
state on Pi: zero. UX coupling between operator and Pi: minimal.

**Phase (b)** would be Pi-side remote control: openMarquee
dashboard exposes Play/Pause/Seek buttons that talk to VLC's RC
or HTTP interface. This is a follow-up if qarl asks — most likely
relevant if/when a stream-source needs to coordinate with the
playlist (e.g. "play this stream then resume"). Defer.

## 5. UI shape

A new "VLC stream" source option in the existing
`ui/src/stream-panel.js`, alongside the current "Go Live" (phone
camera takeover) affordance.

```
┌─────────────────────────────────────────────────────────────┐
│  Stream                                                     │
│  ────────                                                   │
│                                                             │
│  ◯ Camera (phone)         [ Go Live ]                       │
│                                                             │
│  ● VLC stream                                               │
│      ┌──────────────────────────────────────────┐           │
│      │ rtsp://operator-laptop:8554/live         │           │
│      └──────────────────────────────────────────┘           │
│                            [ Start streaming ]              │
│      (during stream)       [ Stop streaming  ]              │
│                                                             │
│      ▾ How to publish from VLC                              │
│        1. Open VLC → Media → Stream                         │
│        2. Add your video / playlist                         │
│        3. Destination: RTSP, port 8554, path /live          │
│        4. Click Stream                                      │
│        5. Paste the rtsp:// URL above (on this network)     │
│                                                             │
│  Status: idle                                               │
└─────────────────────────────────────────────────────────────┘
```

The operator's flow: paste the RTSP URL their VLC is publishing on
→ click Start streaming → Pi opens an ffmpeg subprocess pointed at
that URL → playback loop pauses → frames render.

**Direction is explicit: Pi pulls from VLC.** The operator's host
runs VLC's RTSP server (built into VLC, no extra software). The
URL the operator types is the VLC host's address as the Pi sees it
(via Tailnet if cross-LAN, via LAN IP if same network).

## 6. Tier caps + audio

Audio: **muted, same as §5.11.** ffmpeg command above includes
`-an` (no audio); even if VLC publishes an audio track the Pi
drops it on ingest. Matches the Phase 1 contract and avoids
managing the speaker pipeline mid-stream.

Tier caps extend the existing `HardwareTier` struct in
`api_stream.py:62`:

| Tier   | Source     | Max resolution | Max fps | Decode |
|--------|------------|----------------|---------|--------|
| basic  | Pi Zero 2 W | 854×480       | 30      | SW H.264 |
| good   | Pi 4 / 5    | 1920×1080     | 30      | SW H.264 |
| future | TBD — Phase 12.3 hardware live-fire (Slice F below) | — | — | — |

The phone (camera path) and the VLC source share the same tier
caps — both serve the same render pipeline. Phase 12.3-style
live-fire validation should bless the Pi 4/5 1080p number before
shipping that tier; the constant lives in one place so a tier
drop is a one-line change.

## 7. Open questions for qarl

1. **🐘 Takeover vs playlist-slide — scope-defining**: this
   proposal treats VLC as an out-of-band takeover (same
   `StreamManager`, preempts the playlist exactly like phone
   takeover does). qarl's framing — "streaming a video with
   playback controls" — also sounds like a slide type that lives
   *inside* the playlist next to image/text/video slides. If it's
   a playlist slide, the architecture is different: source lives
   in `playback.py`'s slide-iteration loop, no `StreamManager`
   exclusion, no `pause/resume` dance, and the operator schedules
   it like any other content. **Which model?** Slices A/C/D/E all
   look different under "playlist slide." Recommended for now:
   takeover, because it composes with the existing Phase 1 phone
   path and ships faster — but explicit confirmation is needed.

2. **Direction confirmation**: Pi-pulls-from-VLC-RTSP (Option C in
   §2, recommended) vs VLC-pushes-to-Pi-hosted-RTSP-server vs
   UDP-TS. The recommended path is operator-friendly but requires
   the operator's host to be reachable from the Pi (LAN or
   Tailnet). Override?

3. **Source coexistence with phone takeover**: today
   `StreamManager` allows at most one `StreamSession`. Should a
   VLC stream share the same exclusion (phone preempts VLC, VLC
   preempts phone, both via `/takeover`) — or should they be
   separate slots? Recommended: same exclusion, simpler model.
   (Moot if Q1 picks "playlist slide.")

4. **Reconnect on VLC disconnect**: if the operator stops VLC
   mid-stream, should the Pi auto-retry the RTSP URL for N
   seconds (waiting for VLC to come back), or end the stream and
   resume playback? Recommended: end the stream, mirroring Phase
   1's behavior when WebRTC drops.

5. **RTSP URL authentication**: open URL (any client on the
   network can connect) vs the operator embeds `rtsp://user:pass@
   host/path` credentials vs Tailscale ACLs are the only fence.
   Recommended: open URL + Tailscale ACL as the network fence —
   matches the existing trust model.

6. **Frame-starvation behavior**: if VLC pauses, the Pi receives
   no new frames. Hold the last frame on glass indefinitely (per
   §4), or auto-resume the playlist after N seconds of starvation?
   Recommended: hold the frame — matches §4's "Pi is a dumb
   consumer" framing and is the simpler default. Operator hits
   Stop streaming when they're done.

7. **Pi-side Stop button**: the §5 UI mock shows a Stop button
   for the VLC source even though playback controls are VLC-side
   only. Stop tears down the RTSP consumer + resumes the playlist
   on the Pi side. Confirm this is wanted (recommended) vs
   "stop only when VLC's RTSP server goes away" (more minimal,
   but leaves an awkward stuck-frame UX if the operator quits
   VLC without an orderly shutdown).

## 8. Implementation slicing

Six commit-sized slices. Each has a clear gate + verify shape.

1. **Slice A — `StreamSource` abstraction refactor (renderer-side
   no-op)**. Extract a `StreamSource` Protocol (`backend/
   openmarquee/stream_source.py` new file) with one method
   `frames() -> AsyncIterator[bytes]` yielding RGB888 buffers
   pre-sized to `renderer.width × renderer.height × 3` (source
   owns cover-fit). Refactor `StreamSession._consume_video` into
   `WebRtcStreamSource` implementing the protocol; the existing
   `_cover_fit` PIL call moves *inside* the WebRTC source so the
   protocol contract is "source yields renderer-sized frames."
   `StreamSession` now consumes whichever source it was given.
   **Gate:** existing `test_stream.py` tests stay green (zero
   behavior change for the WebRTC path) + new unit test confirms
   `WebRtcStreamSource` honors `renderer.width/height` from
   inside the source.

2. **Slice B — `RtspStreamSource` via ffmpeg subprocess**. New
   class consuming an RTSP URL, spawning ffmpeg with the
   scale+crop+rgb24 filter chain interpolated against
   `renderer.width/height` at session start. Reads raw RGB frames
   off stdout in fixed-size chunks. Includes a stderr drainer
   that logs ffmpeg errors and a process-exit watchdog mirroring
   the Phase 1 phantom-session timeout. **Slice B step 0:** check
   `which ffmpeg` on the dev Pi + pi-gen image; if absent, add
   `apt install ffmpeg` to `system/openmarquee-pi-image/` recipe.
   Subprocess teardown is wired into the existing
   `StreamManager.stop_all()` path (no new lifecycle hook). **Gate:**
   new unit tests with a mock-ffmpeg subprocess script that emits
   N RGB frames; verify EOF, stderr capture, dimensional
   correctness, and clean subprocess teardown on stop_all.

3. **Slice C — `/api/stream/start` payload extension**. Replace
   `StreamStartRequest.sdp_offer: str` with a discriminated union
   `{ "kind": "webrtc", "sdp_offer": str } | { "kind": "rtsp",
   "url": str }`. `StreamManager.start()` dispatches by kind to
   build the right source. Existing phone clients keep working
   (default to webrtc on missing kind). **Gate:** end-to-end test
   `test_stream.py::test_start_rtsp_session_pulls_frames` using a
   fixture ffmpeg-mock script that emits N RGB frames; verify
   they reach the mock renderer.

4. **Slice D — UI Stream-source picker**. Add the radio +
   RTSP-URL field shown in §5 to `ui/src/stream-panel.js`. New API
   call `startRtspStream(url)` in `api.js`. The
   instructions-disclosure block. Status reporting includes the
   source kind. **Gate:** Playwright e2e
   `stream-vlc.spec.js` against a running uvicorn that confirms
   the API call shape; visual smoke on the dev Pi
   (`http://openmarqueedev/`).

5. **Slice E — Tier extension**. Add the `good` tier to
   `HardwareTier` literal + lift `_BASIC_TIER` to a per-source
   table so RTSP and WebRTC can have distinct caps if needed.
   Today they share the same numbers; the abstraction is the
   place to override later. **Gate:** unit tests on
   `HardwareTier` round-trip + `/status` response shape.
   *Folding consideration:* if Slice C touches `/status`'s
   payload shape anyway (adding the source-kind discriminator),
   it may be cleaner to ship the tier-table lift in the same
   commit so `/status` settles in one bump. Defer this micro-
   decision to the Slice C implementer.

6. **Slice F — Hardware live-fire on dev Pi + Pi Zero 2 W +
   Pi 4**. Stream a 1-hour movie from a laptop VLC into each Pi;
   measure dropped-frame count, CPU%, RAM, and frame-time jitter.
   Bless the Pi Zero 2 W tier (480p/30) and the Pi 4/5 tier
   (1080p/30) or override per measurements. **Gate:** QA
   captures saved under `qa/captures/stream-vlc-livefire-
   YYYY-MM-DD.md` with the numbers.

Related memory:
- `[[project_dev_pi_provisioned]]` — dev target at
  `http://openmarqueedev/` (Tailscale magic-DNS).
- `[[project_pi_rust_binary_path]]` — Pi runtime layout.
- `[[feedback_md5_verify_after_fleet_deploy]]` — applies to any
  binary touch in Slice F live-fire.
