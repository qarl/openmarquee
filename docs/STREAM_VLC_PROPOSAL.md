# STREAM / VLC — design proposal (scoping pass)

Scoping doc for adding a VLC-driven video stream alongside the
existing Phase 1 phone-camera takeover (`backend/openmarquee/stream.py`,
WebRTC over aiortc). Per qarl 2026-05-19: "stream should allow
streaming a video with playback controls (in addition to camera)
including from VLC." Followup qarl 2026-05-19: "i think i want
both versions" — two delivery modes (see §2).

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

Two abstractions already present that the VLC paths can ride on:

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
  the playback-loop preempt+restore primitive used by the
  takeover model. The playlist-slide model rides the
  loop's normal slide-iteration code, not pause/resume.
- **Slide types in `backend/openmarquee/content/__init__.py`**
  (`TextSlide`, `ImageSlide`, `VideoSlide`) are the schema /
  storage shape the playlist-slide model will extend with a new
  `VlcStreamSlide` content type.

## 2. Two delivery modes

qarl wants **both**, not either-or.

**Mode A — Takeover** (operator-triggered, real-time). The
operator opens the openMarquee dashboard, picks "VLC stream",
pastes an RTSP URL their VLC is publishing, taps "Start
streaming." The Pi pauses the playlist mid-cycle and starts
rendering VLC's frames immediately. Playback controls (play /
pause / seek) live in VLC on the operator's laptop. When the
operator hits "Stop streaming" or VLC disconnects, the playlist
resumes the slide it was on. Mirrors the existing Phase 1 phone-
camera takeover UX exactly; just a second source type.

**Mode B — Playlist slide** (scheduled, embedded in playlist).
A "VLC stream" slide type sits in the content library alongside
text / image / video slides. Operator creates one in the slide
editor by giving it a name, an RTSP URL, and a duration (or "play
until stream ends"). The slide can be added to playlists and
scheduled like any other slide. When playback hits the slide's
slot, the Pi connects to the configured RTSP URL, renders frames
for the slide's duration, then advances to the next slide per
normal scheduling. If the URL is unreachable when the slot fires,
fallback behavior is configurable (Q12).

The two modes share most of the infrastructure (transport,
ffmpeg, frame pipe to renderer, tier caps, audio defaults) but
have distinct lifecycles, schemas, and UI surfaces. The
abstraction boundary lives below both — see §4.

## 3. Recommended transport — RTSP (pull from VLC's built-in
server)

**Pick: RTSP, with the Pi pulling from the VLC host.** Applies
to both modes. Operator runs VLC with `--sout '#rtsp{...}'` on
their laptop; VLC's built-in RTSP server publishes a URL like
`rtsp://<operator-host>:8554/live`; for Mode A the operator
pastes the URL into the dashboard, for Mode B the operator
pastes it into the slide editor. In both cases the Pi spawns
an ffmpeg subprocess to consume the URL.

Why RTSP wins (applies to both modes):

- VLC's RTSP server mode is well-documented and is the canonical
  "VLC publishes a stream" path operators already know. No
  separate RTMP server, no HLS publisher, no manifest plumbing.
- ffmpeg can consume RTSP directly (`-i rtsp://...`).
- LAN- and Tailscale-friendly: Pi initiates the connection out to
  the operator's host, so the operator doesn't need to expose a
  port to the public internet. Tailnet is the WAN story (no cloud
  per project policy).

Alternatives considered (and rejected for both modes):

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
  hidden behind a custom-sout text field. Revisit if hardware
  live-fire shows lossy-network frame drops on RTSP-over-TCP.
- **Plain TCP MPEG-TS**: `--sout '#std{access=tcp,...}'` + `-i
  tcp://...`. Simpler wire format (no SDP). Loses because VLC's
  Stream wizard doesn't list it; operators would need to know the
  custom-sout incantation. RTSP wins on UI surface.

## 4. Pi-side consume path — `VlcRtspConsumer` shared, two
lifecycle owners

**Architecture call**: extract a low-level `VlcRtspConsumer`
that owns the RTSP-via-ffmpeg mechanics; the two modes wrap it
in their own lifecycle owners.

```
                  ┌─────────────────────────────────┐
                  │       VlcRtspConsumer           │   (shared)
                  │  spawn ffmpeg → yield RGB888    │
                  │  cleanup on close / EOF / exit  │
                  └────────────────┬────────────────┘
                                   │
                ┌──────────────────┴──────────────────┐
                ▼                                     ▼
      ┌───────────────────┐                ┌────────────────────┐
      │ RtspStreamSource  │                │ VlcStreamSlide     │
      │   (Mode A)        │                │   playback (Mode B)│
      │                   │                │                    │
      │ implements        │                │ invoked from       │
      │ StreamSource      │                │ playback.py's      │
      │ Protocol;         │                │ slide-iteration    │
      │ StreamSession     │                │ loop for the       │
      │ wraps it          │                │ slide's duration   │
      └───────────────────┘                └────────────────────┘
                │                                     │
                ▼                                     ▼
            renderer.render_frame(bytes)          (same renderer)
```

**Shared in `VlcRtspConsumer`:**
- ffmpeg subprocess spawn + teardown
- Filter chain (`scale=W:H:force_original_aspect_ratio=increase,
  crop=W:H, format=rgb24`) with renderer-dim interpolation
- Fixed-size frame reading off stdout
- stderr drainer for ffmpeg log capture
- Process-exit detection
- Async iterator interface yielding `bytes` of
  `renderer.width × renderer.height × 3`

**Distinct in Mode A (`RtspStreamSource` + `StreamSession`):**
- Implements the `StreamSource` Protocol (same shape
  `WebRtcStreamSource` will use after the Slice 1 refactor).
- Owned by `StreamManager`'s single takeover slot.
- Lifecycle: `start()` triggers pause-of-playlist; iterator
  exits → `close()` triggers resume.
- Operator hits dashboard "Stop streaming" → session closes.

**Distinct in Mode B (`VlcStreamSlide` + playback integration):**
- New Pydantic content type next to `TextSlide` / `ImageSlide` /
  `VideoSlide` in `backend/openmarquee/content/__init__.py`.
- Owned by the playlist; storage + JSON envelope round-trip
  same as other slide types.
- Lifecycle: when the playback loop's slide iteration reaches a
  `VlcStreamSlide`, the loop spawns a `VlcRtspConsumer`, pumps
  its yielded bytes to `renderer.render_frame()` for
  `slide.duration_ms`, then closes the consumer and advances.
- Stream end before duration: hold-last-frame for remainder
  (default — per the recommended-default for VLC-paused
  behavior; matches the takeover model's "Pi is dumb"
  semantics). Q12 below has the alternative options.
- **Cross-mode teardown beat**: the per-frame pump's pause-check
  triggers the `finally: await consumer.close()`, so a takeover
  starting mid-`VlcStreamSlide` reaps the ffmpeg subprocess
  before the takeover spawns its own consumer. Two ffmpegs
  consuming the same (or different) RTSP streams concurrently
  would burn CPU and is the bug this teardown prevents.

**Integration-sequencing note (BLOCKER caught in re-review):**
`backend/openmarquee/playback.py` today does NOT dispatch by
`slide.type` for rendering. The loop calls
`begin_slide(item.id, t0_ms, duration_ms)` and the Rust sidecar
reads the on-disk envelope and dispatches there
(`renderer/src/content.rs` ContentItem enum). Sending the Rust
sidecar a `vlc_stream_slide` envelope it doesn't know about
would be rejected.

Mode B's integration therefore **intercepts `vlc_stream_slide`
items in `playback.py` BEFORE `_play_via_rust_ipc` is called**
and runs a parallel pump that calls `renderer.render_frame(rgb)`
directly. This is structurally identical to Mode A's path; both
modes inherit the existing constraint that
`RustRenderer.render_frame()` is `NotImplementedError` today
(`backend/openmarquee/rendering/rust_renderer.py:591-600`).
Production rollout of either mode requires a sidecar push-frames
IPC op (`paint_external_frame(rgb)` or similar) — see Slice 2.5
in §9. On `MockRenderer` (dev / CI) `render_frame` works, so
Slices 1-8 stay testable; only Slice 9 (hardware live-fire) is
gated on the push-frames op.

**ffmpeg filter chain (shared)**, with renderer dims interpolated
at consumer-start time:

```
ffmpeg -loglevel error -fflags nobuffer \
       -i rtsp://<source>:8554/live \
       -an \
       -vf "scale=<W>:<H>:force_original_aspect_ratio=increase, \
            crop=<W>:<H>, format=rgb24" \
       -f rawvideo -
```

`scale + crop` together implement cover-fit. Doing it in
ffmpeg's filter graph avoids the Python-side memcpy + PIL
roundtrip on every frame; the consumer reads
`renderer.width * renderer.height * 3` bytes off stdout and
hands them straight to `renderer.render_frame()`. **The tier
cap (854×480, etc.) governs ffmpeg's network-side decode
workload — not the output buffer size.**

**Why not aiortc**: WebRTC-only.
**Why not GStreamer**: bigger install footprint and more API
surface; ffmpeg suffices.

**ffmpeg-binary availability check** before any shared-layer
slice starts: `renderer/src/v4l2.rs` + `renderer/src/mp4_demux.rs`
are Rust-side bcm2835 V4L2 hardware decode for video slides —
they do NOT depend on the system `ffmpeg` binary. The aiortc
path uses PyAV (libav-the-library), also not `ffmpeg`-the-
binary. So the Pi base image may or may not have `ffmpeg`
installed. If absent, add `apt install ffmpeg` to the pi-gen
recipe — folds into the shared-layer slice as a one-line
packaging change.

**Cost**: Pi 4/5 has comfortable headroom for SW H.264 decode at
1080p/30. Pi Zero 2 W is **the live-fire question** the final
slice answers; if the basic tier doesn't hold at 480p/30 the
constant drops to 360p/30 in one line of `api_stream.py`.

RAM per active consumer: one frame buffer
(`renderer.width × renderer.height × 3` bytes ≈ 6 MB at 1080p).
Disk: zero — stdout-only, no caching.

## 5. Mode A specifics — takeover lifecycle + UI

**Playback control surface**: VLC-side only (phase a). Operator
drives play / pause / seek / volume from the VLC UI. Pi is a
dumb consumer; frames stop arriving when the operator hits Pause
in VLC; Pi holds the last frame. Phase (b) Pi-side remote
control via VLC's RC/HTTP interface is a follow-up if/when
needed.

**UI shape** — extend the existing `ui/src/stream-panel.js`:

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

Operator flow: paste URL → Start → Pi opens ffmpeg → playback
pauses → frames render. Stop or VLC-disconnect → playback
resumes.

## 6. Mode B specifics — playlist-slide schema + editor

**Content type**: `VlcStreamSlide` (Pydantic, next to
`TextSlide`/`ImageSlide`/`VideoSlide`). Fields:

```python
class VlcStreamSlide(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    # Q17 below: type literal naming —
    # "vlc_stream" (matches ImageSlide="image" + VideoSlide="video")
    # vs "vlc_stream_slide" (matches TextSlide="text_slide"). Pick.
    type: Literal["vlc_stream"] = "vlc_stream"
    name: str
    rtsp_url: str  # operator pastes their VLC RTSP URL here
    duration_ms: int = Field(default=10_000, ge=100, le=24*60*60*1000)
    # Q12 below: how to handle URL unreachable when slot fires?
    on_unreachable: Literal["hold_last_frame", "black", "skip"] = "hold_last_frame"
    # Transitions in/out of the slot — same union as other slides.
    transition: Literal["cut", "fade", "wipe", ...] = "cut"
    transition_ms: int = Field(default=500, ge=0, le=5000)
    created_at: datetime = Field(default_factory=_utcnow)
    # Output-only mirror populated by ContentStorage.load() —
    # mirrors ImageSlide / VideoSlide.
    updated_at: datetime | None = None
```

`ContentItem` discriminated union in `content/__init__.py:467`
gains `| VlcStreamSlide`; `_CONTENT_ADAPTER` (`content/storage.
py:56`) rebinds automatically. Tombstones key on UUID
(`tombstone.py:91`, no slide-type awareness) so delete-
propagation works for free. `flock_sync.py:223` has a
`VideoSlide` isinstance branch for `asset.mp4` GET;
`VlcStreamSlide` has no second payload so it falls through to
the default branch automatically — no flock_sync change needed.
Scheduling (`schedule_storage` in `seed.py:144`) is playlist-
scoped, not slide-type-scoped, so a `VlcStreamSlide`
participates for free.

**Storage / envelope** (`content/storage.py:99-137`): today
`save()` writes both the envelope AND an `asset.png` for every
slide. `VlcStreamSlide` has no source PNG. Slice 6 picks one:
either (a) `save()` accepts `png: bytes | None` and skips the
asset write when None, or (b) Slice 6 generates a synthetic
placeholder PNG at save-time (a "VLC stream" thumbnail card the
slide-tile renders). **Recommended (b)** — preserves editor-
tile parity with other slides and dedupes the placeholder code
path with the §6 editor preview. The renderer never paints this
PNG on glass; it's only for the editor's tile view.

**Playback integration**: `playback.py`'s loop today does NOT
dispatch by `slide.type` for rendering — it calls
`begin_slide(item.id, t0_ms, duration_ms)` and the **Rust
sidecar** dispatches on the envelope kind
(`renderer/src/content.rs` ContentItem enum). Slice 7 adds a
**pre-dispatch interception** in Python: before
`_play_via_rust_ipc` is called, if `item.type == "vlc_stream"`
the loop runs the deadline-bounded pump below directly via
`renderer.render_frame()`, bypassing the sidecar. Once the pump
exits, the loop advances per normal scheduling.

Pseudo-code for the interception branch:

```python
# inside playback.py's loop, BEFORE the _play_via_rust_ipc call:
if item.type == "vlc_stream":
    consumer = VlcRtspConsumer(item.rtsp_url, renderer.width, renderer.height)
    deadline = monotonic() + item.duration_ms / 1000
    try:
        async for rgb in consumer.frames():
            if monotonic() >= deadline:
                break
            if self._paused:  # takeover preemption
                break
            renderer.render_frame(rgb)
        # On EOF-before-deadline → on_unreachable for the remainder.
    finally:
        await consumer.close()  # reaps ffmpeg subprocess
    continue  # skip _play_via_rust_ipc for this item
```

The consumer's `frames()` async-generator yields until either
(a) the deadline hits, (b) ffmpeg exits / RTSP disconnects, or
(c) a takeover preempts via `pause()`. On (b)-before-deadline,
the slide falls through to the `on_unreachable` behavior for
the remaining time. Slot bookkeeping (`self._slot_t0`,
`self._current_id`, `self._current_type` — see §`/api/playback/
state` consumers) must be preserved across the interception so
the dashboard reports the right slide.

**Slide editor UI** — new slide type in
`ui/src/editor.js` (or wherever slide creation lives):

```
┌─────────────────────────────────────────────────────────────┐
│  New slide                                                  │
│  ──────────                                                 │
│                                                             │
│  Type: [Text] [Image] [Video] [VLC stream]                  │
│         ─────                  ───────────                  │
│                                                             │
│  Name:        ┌──────────────────────────────────┐          │
│               │ Q3 Live Stream                   │          │
│               └──────────────────────────────────┘          │
│                                                             │
│  RTSP URL:    ┌──────────────────────────────────┐          │
│               │ rtsp://laptop.tail-net:8554/live │          │
│               └──────────────────────────────────┘          │
│                                                             │
│  Duration:    [ 10  ] seconds                               │
│                                                             │
│  If stream is not running:                                  │
│    ● Hold last frame      ○ Show black     ○ Skip slide     │
│                                                             │
│  Preview:                                                   │
│  ┌──────────────────────────────────────────┐               │
│  │  ▶ Live VLC stream                       │               │
│  │  rtsp://laptop.tail-net:8554/live        │               │
│  │  (not rendered in editor — live on glass)│               │
│  └──────────────────────────────────────────┘               │
│                                                             │
│                                    [ Cancel ] [ Save ]      │
└─────────────────────────────────────────────────────────────┘
```

Preview is intentionally a placeholder card — actually opening
an RTSP stream in the browser would require WebRTC re-publish
or an MSE bridge, which is way out of scope. The card shows the
configured URL + a "live on glass" disclosure.

## 7. Tier caps + audio

Audio: **muted, same as §5.11** — both modes. ffmpeg filter
includes `-an`; even if VLC publishes audio the Pi drops it on
ingest.

Tier caps extend the existing `HardwareTier` struct in
`api_stream.py:62`:

| Tier   | Source     | Max resolution | Max fps | Decode |
|--------|------------|----------------|---------|--------|
| basic  | Pi Zero 2 W | 854×480       | 30      | SW H.264 |
| good   | Pi 4 / 5    | 1920×1080     | 30      | SW H.264 |
| future | TBD — Phase 12.3 hardware live-fire (final slice below) | — | — | — |

Both modes share tier caps — they share `VlcRtspConsumer`. The
phone-camera path (WebRTC) also shares the same numbers today;
the abstraction allows distinct caps later if profiling motivates.

## 8. Open questions for qarl

Recommended defaults from prior dispatch (Q1-Q7) carry over;
new playlist-slide-specific questions added below.

**Shared / takeover-mode (already answered)**:

1. ~~Takeover vs playlist-slide~~ → **BOTH** (qarl 2026-05-19).
2. **Direction**: Pi pulls from VLC RTSP URL. (confirmed default)
3. **Source collision**: second stream boots first out. Per
   qarl 2026-05-19 this applies in the **takeover slot only** —
   the playlist's natural slide rotation is unaffected by a
   takeover, and a takeover ending puts the playlist back where
   it was. (default)
4. **VLC drops mid-takeover**: end stream + resume playlist.
   (default)
5. **URL access**: open URL + Tailscale ACL fence. (default)
6. **VLC paused mid-takeover**: hold last frame on glass.
   (default)
7. **Dashboard Stop button** (takeover mode): keep it. (default)

**Playlist-slide-mode (new — need qarl input)**:

8. **Duration model**: fixed-seconds duration only (current
   proposal: `duration_ms`), OR also support a "play until
   stream ends" option that advances when ffmpeg sees EOF /
   RTSP disconnect? Recommended: ship fixed-seconds first (it's
   the playlist-slide shape every other slide type has); add
   "play until end" later if asked.

9. **Slide-editor preview**: a placeholder card showing the URL
   (current proposal, recommended) vs an actual live preview
   via a browser-side WebRTC/HLS re-publish (way more scope) vs
   omit preview entirely. Recommended: placeholder.

10. **Multiple VLC slides in one playlist**: allowed by default
    (different URLs in different slots), no limit. Confirm? If
    capped at 1, the editor needs a guard.

11. **Same URL as both takeover and playlist-slide
    simultaneously**: the playlist-slide hits its slot while
    a takeover is paused — playlist is paused so playlist-slide
    doesn't fire. After takeover ends, playlist resumes and
    eventually hits the playlist-slide. No conflict. Confirm
    this is the intended interaction.

12. **URL unreachable at slot-fire time** (no VLC publishing,
    bad URL, network down): which fallback?
    - **Hold previous slide's final frame** for this slide's
      duration (no painting during this slot). The DRM
      framebuffer keeps the previous slide's last pixels, so
      visually it's "frozen on what was there." (current
      proposal, simplest)
    - **Black** — clear to black for the slide's duration.
    - **Skip slide** — advance to next slide immediately.
    Recommended: per-slide configurable (`on_unreachable`
    field on `VlcStreamSlide`); default `hold_last_frame`.

13. **Connect-timeout for playlist-slide**: if ffmpeg can't
    connect to the RTSP URL within N seconds, trigger the
    `on_unreachable` behavior. Recommended N: 3 seconds (short
    enough that a dead URL doesn't dominate a 10-second slide).

14. **Editor slide-tile thumbnail for VlcStreamSlide**: ship a
    synthetic placeholder PNG generated at save-time (a "live
    VLC stream" card showing the configured URL — matches the
    §6 editor preview, dedupes asset-PNG handling). Confirm?
    Alternative: leave the asset slot empty and special-case
    the editor tile renderer.

15. **Scheduling participation**: `VlcStreamSlide` rides
    `schedule_storage` (playlist-scoped) like any other slide
    type — no code changes needed. Confirm intended.

16. **Tombstone / flock-sync**: tombstones key on UUID only and
    flock_sync's `VideoSlide` isinstance branch doesn't match
    `VlcStreamSlide` (no second payload), so both work for free.
    Confirm intended.

17. **`type` literal naming**: `"vlc_stream"` (matches
    `ImageSlide="image"`, `VideoSlide="video"`) vs
    `"vlc_stream_slide"` (matches `TextSlide="text_slide"`).
    Current proposal: `"vlc_stream"`. Override?

## 9. Implementation slicing

Nine slices. Three shared-layer (1-3), two takeover (4-5),
three playlist-slide (6-8), one cross-mode validation (9). Each
has a clear gate + verify shape.

**Shared layer:**

1. **Slice 1 — `StreamSource` Protocol + `WebRtcStreamSource`
   refactor (no behavior change)**. Extract the takeover-side
   abstraction. New file
   `backend/openmarquee/stream_source.py` with the Protocol;
   refactor `StreamSession._consume_video` into a
   `WebRtcStreamSource` implementing it. `_cover_fit` moves
   inside the source. **Gate:** `test_stream.py` stays green;
   phone takeover semantics byte-identical.

2. **Slice 2 — `VlcRtspConsumer` (shared RTSP+ffmpeg
   mechanics)**. New module
   `backend/openmarquee/vlc_rtsp_consumer.py`. Spawns ffmpeg with
   the renderer-dim-interpolated filter chain, reads RGB chunks
   off stdout, drains stderr, handles teardown. Pure
   transport/decoder — no awareness of takeover-vs-playlist-
   slide. **Slice 2 step 0:** `which ffmpeg` on dev Pi + pi-gen
   image check; if absent, add `apt install ffmpeg` to
   `system/openmarquee-pi-image/` recipe. **Gate:** unit tests
   with mock-ffmpeg subprocess emitting N RGB frames; verify
   EOF, stderr capture, dim correctness, clean teardown. If
   future Mode C (UDP / SRT / TS) is on the roadmap, consider
   renaming `VlcRtspConsumer` → `RtspFrameConsumer` here; defer
   if speculative.

2.5. **Slice 2.5 — Sidecar `paint_external_frame` IPC op (BLOCKER
   for Slice 9)**. `RustRenderer.render_frame()` raises
   `NotImplementedError` today
   (`backend/openmarquee/rendering/rust_renderer.py:591-600`).
   Both Mode A and Mode B need a sidecar IPC op that takes an
   RGB888 buffer and paints it. This unblocks live-fire on real
   Pi hardware; without it, Slices 1-8 are still testable on
   `MockRenderer` (dev / CI) but Slice 9 dies on the first frame.
   May be a separate render-arc slice owned outside this stream
   arc; flag explicitly here so Slice 9 doesn't ship blind.

3. **Slice 3 — Tier-table lift + `good` tier**. Add `good` to
   `HardwareTier` literal + lift `_BASIC_TIER` to a per-source
   table (today both sources share basic). **Gate:** unit tests
   on `HardwareTier` round-trip + `/status` response shape.

**Takeover mode (rides Slices 1 + 2):**

4. **Slice 4 — `RtspStreamSource` (takeover) + API discriminated
   union**. New class wrapping `VlcRtspConsumer` and implementing
   `StreamSource`. `StreamStartRequest` becomes
   `{ kind: "webrtc", sdp_offer: str } | { kind: "rtsp",
   url: str }`. `StreamManager.start()` dispatches by kind.
   **Gate:** end-to-end test
   `test_start_rtsp_session_pulls_frames` using a mock-ffmpeg
   fixture.

5. **Slice 5 — Dashboard UI (takeover-VLC)**. Radio +
   RTSP-URL field in `ui/src/stream-panel.js`, instructions
   disclosure, `startRtspStream(url)` API call, Stop button.
   **Gate:** Playwright `stream-vlc.spec.js` against running
   uvicorn; visual smoke on dev Pi (`http://openmarqueedev/`).

**Playlist-slide mode (rides Slice 2):**

6. **Slice 6 — `VlcStreamSlide` Pydantic model + storage +
   envelope**. Schema in
   `backend/openmarquee/content/__init__.py`, storage round-trip
   in `content/storage.py`, JSON envelope serialization mirroring
   the other slide types. Sub-tasks:
   - Pydantic class with all fields including `transition` +
     `transition_ms` + `updated_at` mirror.
   - Extend `ContentItem` discriminated union; `_CONTENT_ADAPTER`
     rebinds automatically.
   - Asset-PNG strategy: synthetic placeholder generated at
     save-time (recommended (b) per §6) — needs a small PIL
     drawing helper to render the "VLC stream" thumbnail card.
   - `_validate_unreachable_enum` if Pydantic doesn't catch.
   **Gate:** unit tests for create / load / round-trip /
   validation errors / placeholder-thumbnail-on-save.

7. **Slice 7 — Playback-loop integration (HIGHEST RISK)**.
   Pre-dispatch interception in `playback.py` for
   `item.type == "vlc_stream"` items, BEFORE `_play_via_rust_ipc`.
   Sub-tasks (each could blow scope; watch closely):
   - The deadline-bounded pump per §6 pseudo-code.
   - `on_unreachable` handler: ffmpeg-connect-timeout (3s default
     from Q13) → branch on `hold_last_frame` / `black` / `skip`.
   - Pause-respect: `self._paused` check + `finally:
     consumer.close()` triggers on takeover.
   - Slot bookkeeping: preserve `self._slot_t0`,
     `self._current_id`, `self._current_type` so
     `/api/playback/state` reports correctly during the slot.
   - Capture-current-frame compatibility: confirm the existing
     capture path can read the rendered RGB (or document that
     it can't, and Slice 9 lives with the limitation).
   **Gate:** integration test that drives a playlist containing
   a `VlcStreamSlide` against a mock-ffmpeg fixture; verify
   correct duration, correct fallback on unreachable URL,
   correct preemption.

8. **Slice 8 — Slide-editor UI (playlist-slide)**. New "VLC
   stream" tab in the slide-create dialog. URL field, duration
   field, `on_unreachable` radio. Placeholder preview card
   (no live RTSP playback in the browser). **Gate:** Playwright
   spec confirms create / edit / delete round-trip + the
   playlist editor accepts the new slide type.

**Validation:**

9. **Slice 9 — Hardware live-fire (both modes, both Pi tiers)**.
   **GATED ON Slice 2.5** (push-frames IPC op). Stream a 1-hour
   movie from a laptop VLC into each Pi in both takeover and
   playlist-slide modes; measure dropped-frame count, CPU%, RAM,
   frame-time jitter. Bless Pi Zero 2 W (480p/30 basic) and
   Pi 4/5 (1080p/30 good) tiers; override per measurements.
   **Gate:** QA captures in
   `qa/captures/stream-vlc-livefire-YYYY-MM-DD.md`.

Related memory:
- `[[project_dev_pi_provisioned]]` — dev target at
  `http://openmarqueedev/` (Tailscale magic-DNS).
- `[[project_pi_rust_binary_path]]` — Pi runtime layout.
- `[[feedback_md5_verify_after_fleet_deploy]]` — applies to any
  binary touch in Slice 9 live-fire.

## 10. Future: webpage slide type (not in this arc)

qarl (2026-05-20) wants a future slide type that displays a
**webpage** on the sign — a headless browser renders a URL and
its frames go to the screen. It is **not** part of this 9-slice
arc and must not expand or slow it; recorded here only so the
arc leaves the right seams.

Structurally a webpage slide is the *same shape* as the VLC
video path: an external frame producer feeding RGB frames to the
renderer. It reuses, unchanged, two things this arc builds:

- the **slice-2.5 push-frames transport** — that sidecar op is
  defined as "paint an RGB888 frame from an external producer";
  it is deliberately source-agnostic and does not know or care
  whether the producer is ffmpeg/RTSP or a headless browser.
- the **`StreamSource` Protocol** (slice 1, `stream_source.py`)
  — `frames()` yields RGB bytes; a future `BrowserSource`
  (headless Chromium → frames) implements it exactly like
  `WebRtcStreamSource` / `RtspStreamSource`, no rewrite.

Constraint qarl has already accepted: **low-CPU pages only** —
a headless browser on a Pi Zero 2 W (512 MB) cannot sustain
heavy animated pages; the feature will be scoped to static
dashboards / simple pages. Sequencing: a follow-up after the
VLC arc completes.
