# Phase 7 as-built — 2026-05-14

Snapshot of the Rust IPC sidecar architecture **as it actually shipped**
through the night of 2026-05-13 → 2026-05-14. Complements (does not
replace) `docs/renderer-rewrite-plan-rust.md`, which is the
forward-looking spec. Where the two disagree, this doc reflects the
code; the spec has drifted in places that are now load-bearing
(see §3).

Audience: a future maintainer (or qarl, when picking up the slice 4
design call) who needs the current state without reverse-engineering
~40 commits.

**Refresh history:** Initial snapshot `ce440ed`. Refreshed
2026-05-14 23:xx (this commit) to fold in slice 4 followups + V4L2
piece 1-3 + SD-burn flow + Mac-side single-command burn.

## 1. State of Phase 7

| Slice | Status | What it is | Anchor commit |
|-------|--------|------------|---------------|
| 1 | **Shipped** | Python `RustRenderer` IPC proxy (`backend/openmarquee/rendering/rust_renderer.py`) | `8a2a4a0` |
| 2 | **Shipped** | `dependencies.py` factory branch (`OPENMARQUEE_RENDERER=rust-sidecar`) | `9693517` |
| 3 | **Shipped** | systemd unit + `install.sh` staging for the binary at `/usr/local/bin/openmarquee-render` | `cc66a5e` |
| 4 | **Shipped** | `playback.py` drives the IPC ops directly via `_play_via_rust_ipc`; `UnsupportedSlideError` skip path keeps Video on PIL fallback (until V4L2 piece 3) | `71079bb` |
| 4-followup | **Shipped** | `begin_transition` wired through the Rust IPC route (TextSlide-to-TextSlide; non-text endpoints still fall back) | `f481794` |
| 5 | **Pending qarl** | Flip default to `OPENMARQUEE_RENDERER=rust-sidecar` after qarl visual eyeball pass on dev Pi | — |

**Adjacent arcs that shipped this session (separate workstreams,
same flight):**

| Arc | Status | What it is | Anchor commits |
|-----|--------|------------|----------------|
| V4L2 H.264 decode (piece 1) | **Shipped** | Dev Pi V4L2 state inventory + `docs/v4l2-decode.md` + `v4l-utils` install | `3b6c3bf` |
| V4L2 (piece 2a) | **Shipped** | `renderer/src/v4l2.rs` Decoder client scaffold + cap query | `343fe15` |
| V4L2 (piece 2b) | **Shipped** | Decode loop + mmap buffer pool + Frame lifetime via `Arc<Mutex<DecoderInner>>` | `5f67ea5` |
| V4L2 (piece 3a) | **Shipped** | Hand-rolled `mp4_demux.rs` + 7 tests + 320×240 + 720p fixtures | `2dbe775` |
| V4L2 (piece 3b) | **Shipped** | `SlideCache.video_demuxers` populated on `BeginSlide(Video)` | `c56793b` |
| V4L2 (piece 3c) | **Shipped** | Linux-only `prime_video_decoder` opens `/dev/video10` + format-set + REQBUFS + STREAMON + SPS+PPS+IDR feed | `89f9591` |
| V4L2 (piece 3d) | **Shipped** | `FS_NV12_TO_RGB` BT.601 limited-range shader + `CachedNv12Program` + `run_nv12_blit_pass` | `6ffcb33` |
| V4L2 (piece 3e) | **Shipped** | `paint_and_present_one_video_slide_frame` end-to-end; `validate_paint_slide_inputs` accepts Video; Python proxy classifier docstring updated | `e7be17f` |
| V4L2 (piece 3f) | **Live-Pi verified** | 720p smoke on dev Pi: 150/150 PaintSlide responses, mean 28.55 ms, p99 46.48 ms, max 292 ms (first-frame spike), 70 MB RSS, no EAGAIN stalls | (no commit; data captured) |
| V4L2 (piece 4) | **Pending** | DMA-BUF zero-copy via EGLImage (perf tune; current per-frame `glTexImage2D` works but causes the p99 over budget) | — |
| SD-burn flow | **Shipped** | `build_sd_bundle.sh` + `stage_sd_card.sh` + cloud-init + `docs/sd-burn.md` | `1aa6dfe` |
| SD-burn from Mac | **Shipped** | `scripts/burn_sd_card.sh` single-command flasher + `scripts/tests/test_burn_sd_card.sh` (17 PASS validation gauntlet) | `6291b49` |

Slices 1-4 are in tree. Production paths unchanged until an
operator sets `OPENMARQUEE_RENDERER=rust-sidecar` AND slice 5
flips the default; the env-switch path is the explicit opt-in.

The robustness layer (reconnect + watchdog + health-probe +
AutoFallbackRenderer) landed AFTER slices 1-3 and is wired into the
slice-2 factory branch. It's slice-2-shaped (factory-level) even
though it landed on a different night.

## 2. Architecture

```
                     playback.py PlaybackLoop
                              │
                              ▼
                  dependencies._real_renderer_singleton()
                              │
                              │  OPENMARQUEE_RENDERER env-switch:
                              │    mock       → MockRenderer
                              │    drm/auto   → DRMRenderer (Phase 6)
                              │    rust-sidecar → ↓ Phase 7
                              ▼
                  _rust_sidecar_renderer_or_fallback()
                              │
                              ▼
                  AutoFallbackRenderer (wrapper)
                       │              │
                       │              └─ MockRenderer (lazy, on fallback)
                       ▼
                  RustRenderer (proxy)
                       │ stdin/stdout JSON-lines
                       ▼
          openmarquee-render --ipc-sidecar  ← subprocess
            │
            ├── DRM/KMS + GBM + EGL + GLES2  (vc4)
            └── HDMI scanout via multi-plane atomic compositor
```

Key contracts:

- **`AutoFallbackRenderer`** wraps `RustRenderer` at the factory
  boundary. Forwards Renderer-Protocol surface (`width`, `height`,
  `render_frame`) and the 5 IPC ops (`begin_slide`, `advance`,
  `begin_transition`, `capture`, `reconfigure`). On
  `RustRendererSubprocessError` (reconnect-exhausted) at any op:
  tears down the dead proxy, lazy-constructs `MockRenderer` via the
  factory, replays `render_frame` against Mock or raises
  `AutoFallbackInMockError` for IPC ops. One-way permanent swap.
- **`RustRendererRespawnedError`** is a subclass of
  `RustRendererSubprocessError` but the wrapper catches it FIRST
  and re-raises **unwrapped**. A successful auto-reconnect inside
  the proxy is NOT a fallback condition — caller should replay
  session state on the healthy proxy.
- **Frame bytes never cross the process boundary.** The sidecar
  owns GPU-side composition. `render_frame(bytes)` raises
  `NotImplementedError`; real callers (slice 4's `playback.py`
  bypass) will use the IPC ops directly. The wrapper preserves
  this surface only for nominal Renderer-Protocol conformance.
- **The 7-op IPC contract** (Open / BeginSlide / Advance /
  BeginTransition / Capture / Reconfigure / Close) is
  externally-tagged JSON: `{"op": "...", "params": {...}}` →
  `{"ok": {"result": {"command": "...", ...}}}` or
  `{"err": {"error": "..."}}`.

## 3. Wire format reality vs `renderer-rewrite-plan-rust.md` §7

`docs/renderer-rewrite-plan-rust.md` §7 describes a richer IPC
contract than the sidecar actually implements. The spec has drifted
on six axes; all six were documented in the `8a2a4a0` proxy
docstring. Pinning here so a future maintainer doesn't try to
implement the spec by accident:

| Spec §7 says | Actual implementation |
|--------------|----------------------|
| UDS socket transport | stdin/stdout JSON-lines |
| Length-prefixed bincode framing | Newline-delimited JSON |
| `Ready` message on connect | None — Open is the first request |
| Periodic `Health` heartbeat (sidecar-driven) | None — liveness is `subprocess.poll()` from the proxy side |
| `BackendState` enum surfaced via Health | No backing op exists |
| Reconfigure semantics applied without restart | Sidecar returns `"Reconfigure not yet implemented (slice e)"` for any reconfigure call |

The proxy `RustRenderer` (`backend/openmarquee/rendering/rust_renderer.py`)
matches what the code actually emits. The `HealthState` it returns
from `health_probe()` derives liveness from `subprocess.poll()` and
the proxy's own reconnect bookkeeping — there is no server-driven
heartbeat.

If/when a server-side `Health` op lands (separate Rust-side
dispatch, not currently scoped), `RustRenderer.health_probe()`
should be extended to issue it and surface the `BackendState`
value. The Python side is wire-format-ready for that addition.

The byte-stability of error strings is pinned by cargo tests at
`renderer/src/ipc_main.rs` (commit `601820f`). The proxy's
`RustRendererOpError.message` callers can match against verbatim
strings like `"paint_slide: image_slide requires content_root
(--content-root)"`.

### VideoSlide wire path (V4L2 pieces 3a-3e, 2026-05-14)

`BeginSlide(Video)` now exercises a real decode pipeline. On
Linux, `cache.load` for a Video item does:

1. `find_video_slide` parses `<content_root>/<uuid>/item.json`
   into a `VideoSlide`.
2. `Mp4Demuxer::open(<content_root>/<uuid>/asset.mp4)` parses
   the MP4 box tree, extracts SPS+PPS from `avcC`, walks
   `stsz`/`stco`/`stsc` to enumerate samples, converts
   AVCC-length-prefixed NALs to Annex-B start-codes. Hand-rolled,
   ~250 LOC, zero external unsafe to audit, every length read
   bounds-checked. Stored in `SlideCache.video_demuxers`.
3. `prime_video_decoder(&dem)` opens `/dev/video10`
   (bcm2835-codec) via the `v4l2::Decoder` from piece 2b: S_FMT
   OUTPUT(H264, w, h) → S_FMT CAPTURE(NV12, w, h) → REQBUFS(4+4)
   → STREAMON → feeds SPS+PPS+IDR as a SINGLE concatenated buffer
   (single-shot-safe `feed()` constraint). Stored in
   `SlideCache.video_decoders` (Linux-gated field).

Per-advance paint (`paint_and_present_one_video_slide_frame`):
feed next sample → drain `next_frame()` with 5×2 ms EAGAIN
budget → upload Y as `GL_LUMINANCE` (TEXTURE0) + UV as
`GL_LUMINANCE_ALPHA` (TEXTURE1) → blit through
`FS_NV12_TO_RGB` BT.601 limited-range shader (piece 3d) →
swap + commit. Frame drops BEFORE swap so its
`Arc<Mutex<DecoderInner>>`-backed re-QBUF runs synchronously.

`validate_paint_slide_inputs` accepts Video as of piece 3e
(was: returns `"paint_slide: video slides TBD"`). Per-slide
memory: ~10-15 MB at 720p (~20-25 MB at 1080p) for the 4+4
buffer pool; CMA usage ~196 MB during decode.

**Known doc-vs-code gap (surface for follow-up):** the
`_UNSUPPORTED_SLIDE_WIRE_MARKERS` tuple in `rust_renderer.py`
still contains `"video slides TBD"`. Reason: the Capture-side
validator still emits `"Capture: video slides TBD (image +
text both supported)"` for VideoSlide screenshots, and the
substring match catches both. Once VideoSlide capture lands
(separate piece), this marker should be split or removed.

## 4. Perf characteristics

Sustained-smoke baselines on the dev Pi at 1024×768 HDMI, 50 loops
× 19 FYS slides (~31 min wall-clock, ~50-58k IPC ops per run):

| Baseline | Frame mean | p50 | p99 | over_33 | over_50 | mem trajectory |
|----------|------------|-----|-----|---------|---------|----------------|
| `bdc7303` (pre-cache) | 26.0 ms | 18.6 ms | 118.4 ms | 22.8% | 15.1% | sawtooth −36 MB (eviction storms) |
| `12ce420` (post hold-cache, `9e776e7` wire) | 12.81 ms | 1.97 ms | 118.3 ms | 9.9% | 6.1% | stable plateau, −0.96 MB delta |
| `a3da434` (post transition-cache, `e6f914e` wire) | **7.47 ms** | **2.30 ms** | **29.1 ms** | **0.24%** | **0.018%** | flat, **0.00 MB** delta |

Headline: **41× improvement** in over_33 rate (9.9% → 0.24%) on
the transition-cache delta alone — pre→post hold-cache also gave
a separate 56% drop. p99 lands **under the 33 ms budget**.

### What drove the perf wins

Both wins are the same kind of fix — wire
`EglSession::slide_caches` through `paint_slide` so the IPC sidecar
reuses per-slide rasterized glyph bitmaps + GL textures across calls.

- `9e776e7` wired the cache through `paint_and_present_one_frame_for_slide`
  (the hold path). Mid-slide frames went 9× faster (p50 18.6 → 1.97 ms).
- `e6f914e` wired the cache through both `make_slide_fbo` bake sites
  in `paint_and_present_one_transition_frame`. The remaining transition
  frames (~19k per 50-loop run) joined the cache; over_33 dropped from
  9.9% to 0.24%.

The bug class was identified by `34e952d`'s `paint_slide` internal
profile, which pinned `raster_us` at 85.9% of `paint_us` for the
4 heaviest FYS slides — the IPC sidecar was passing
`glyph_cache: None` / `tex_cache: None` and re-rasterizing every
layer every frame. The cache infrastructure already existed
(`EglSession::slide_caches`), just wasn't wired into the IPC entry
points.

### Where the 0.24% floor comes from

It is **not** the ticker/glitch slides the prior dispatch (`12ce420`)
estimated. p99 at 29.1 ms says the architectural floor on this
build is ~30 ms — first-frame-of-slide cache warm-up + occasional
boundary outliers. The 5 FYS slides with glitch/ticker motion DO
miss the `(text, size_px)` cache key by design (mutated text per
frame), but only the mutated layer re-rasterizes; the rest of the
slide stays warm, so per-frame cost stays under the budget.

### 1080p outlook

The dev Pi has HDMI EDID stuck at 0 bytes, forcing 1024×768. The
cache wire is resolution-independent (cache key is `(text,
size_px)`, not output res), so the same gates should hold at
1920×1080 with similar margins. Verification deferred until office-
glass time (see `project_phase7_pending_at_office`).

### VideoSlide live-Pi smoke (piece 3f, 2026-05-14)

5-second smoke against `test_720p.mp4` (10 sec H.264 baseline @
1280×720, 30 fps, libx264) driven through the cross-built sidecar
in IPC mode. Driver: `/tmp/v4l2_video_smoke.py` (Pi-side, ad-hoc;
not committed since it's a one-off Mac-side artifact). Backend was
stopped to release DRM master for the smoke.

| Metric | Value |
|--------|-------|
| Advance round-trip mean | 28.55 ms |
| Advance round-trip p99 | 46.48 ms |
| Advance round-trip max | 292 ms (advance #1, first-frame texture-alloc + codec warmup) |
| PaintSlide responses | 150 / 150 |
| EAGAIN stalls | 0 |
| Errors / idle / slide_complete misses | 0 / 0 / 0 |
| First frame produced at | sample_idx = 3 (2-3 sample codec pipeline warmup) |
| RSS at smoke end | 69.9 MB (was 60.8 MB at Open; +9 MB during decode) |
| CMA in-use during decode | 193-196 MB (V4L2 buffer pool) |

**Sub-33 ms target: mean YES (28.55 ms), p99 NO (46.48 ms over
the 30 fps budget).** Steady-state from advance #2 onward sits at
~26-29 ms; the p99 over budget is a small minority of frames
likely solved by piece 4's DMA-BUF zero-copy (which removes the
per-frame `glTexImage2D` upload of Y + UV planes).

**HDMI EDID note (`project_phase7_pending_at_office`):** the dev
Pi's EDID is restoring to 1024×768 instead of the connected TV's
1280×720 / 1920×1080. The smoke painted 1280×720 NV12 textures
scaled down to a 1024×768 framebuffer via the BT.601 shader's
bilinear filter. Full-resolution 720p / 1080p numbers are
qarl-direct pending until office-glass time recovers the EDID.

## 5. Robustness layer

The proxy's failure model evolved across two commits:

### `1796584` — reconnect + watchdog + health-probe

- **Bounded auto-reconnect** on subprocess death. Detected via
  `subprocess.poll()`, broken-pipe write, or empty-stdout-readline.
  Default policy: 3 retries within a 60s rolling window (matches
  systemd's `Restart=on-failure / StartLimitBurst=3 /
  StartLimitIntervalSec=60s` defaults).
- On reconnect success, the failing op raises
  `RustRendererRespawnedError` (subclass of `SubprocessError`) so
  callers know to replay session state on the new subprocess.
  On exhaustion, plain `RustRendererSubprocessError` is raised
  with the reconnect trail in the message.
- `reconnect_max_retries=0` disables auto-reconnect entirely
  (preserves slice-1 fail-loud semantics).
- **1Hz watchdog thread** polls liveness between ops. On detected
  death, attempts reconnect under the main lock using
  `acquire(blocking=False)` so a slow op never deadlocks the
  watchdog tick. `close()` stops the watchdog FIRST, then tears
  down the subprocess.
- **`_lock` is `threading.RLock`** so the reconnect path can call
  `_send_op` recursively (the re-Open during reconnect) without
  deadlocking. Watchdog is a different thread and uses
  `blocking=False`, so RLock still blocks it from barging in
  mid-op.
- **`health_probe()`** returns a `HealthState` snapshot
  (`is_alive`, `exit_code`, `reconnect_attempts_in_window`,
  `reconnect_history`). On-demand only — no sidecar-driven
  heartbeat exists (§3 drift). The probe takes `_lock` so the
  prune+len+tuple snapshot is atomic vs concurrent reconnect.

### `0a81a2c` — `AutoFallbackRenderer`

- Wraps the proxy at the factory boundary. Catches
  `RustRendererSubprocessError` from any op and swaps to
  `MockRenderer` for the rest of the session.
- One-way permanent swap. Process restart is the recovery path.
  Operators see a single `log.error("RustRenderer exhausted; falling
  back to MockRenderer: ...")` line and the renderer-monitor can
  detect the swap via the `is_in_fallback` property.
- **Critical**: catches `RustRendererRespawnedError` FIRST and
  re-raises unwrapped (subagent-caught bug before commit) —
  Respawned indicates the proxy is **alive** after a transient
  blip; the caller replays state, we don't throw the healthy proxy
  away.

Both layers stay opt-in: the factory's `_rust_sidecar_renderer_or_fallback`
returns the wrapped proxy only when `OPENMARQUEE_RENDERER=rust-sidecar`.
Production Pi behavior is unchanged until slice 4 + slice 5 flip
the default.

## 6. Gates

### `scripts/render_cache_gate.sh` (`c067332`)

Fast cache-regression gate that runs in ~10s. Drives the IPC
sidecar through ~50 frames of FYS-01 FREE
(`3964c302-311f-44f2-a6c9-efd24a16cfc0`) with
`OPENMARQUEE_BOUNDARY_TRACE=1`, asserts max post-warmup
`total_us` ≤ 33 ms. Gates on **max**, not mean — the failure
mode is a few catastrophic frames, not slow average.

Warmup defaults to 3 frames: frame[0] is cache-cold + DRM mode-set
(~138 ms), frame[1] is post-init GBM (~9 ms), frame[2] occasionally
has a DRM-resched outlier (~12 ms). frame[3+] is steady ~6 ms.

Fail-verified before commit: flipping any of the 5
`Some(&mut cache.glyph)` callsites back to `None` makes the
gate trip with all post-warmup frames over budget and paint_us
dominating the breakdown. On HEAD the gate passes at max 6.18 ms
(paint 1.02 ms).

### `renderer/src/lru.rs` (pre-existing, 8 tests)

The 6-cap LRU that backs `image_bg_cache: ImageBgCache =
LruMap<PathBuf, (NativeTexture, u32, u32)>` has comprehensive
cargo tests at `renderer/src/lru.rs:132-242` covering cap
enforcement, LRU victim selection, touch-via-get semantics,
sustained cycling, etc. (Note: `slide_caches` is a plain
`HashMap`, NOT an LRU — doc at `hdmi.rs:341` is explicit on
this. The "6-slide LRU" reference in `9e776e7`'s commit body was
a misreading of task #280's title `6 slide_caches eviction sites`,
which counts call sites in code, not a data-structure cap.)

### `backend/tests/rendering/test_rust_renderer.py` (43 tests + 1 skipped)

Covers the proxy's wire format, error-class dispatch, lifecycle,
reconnect bookkeeping, watchdog join, health-probe semantics. Uses
a Python-impersonator `fake_sidecar.py` subprocess that speaks the
same JSON-lines protocol — exercises the actual pipe + serde paths
without depending on a Rust binary build. One real-binary E2E test
gated on `OPENMARQUEE_RUST_BINARY_E2E` env var (dev Pi only).

### `backend/tests/test_dependencies.py` (24 tests)

Factory dispatch matrix + `TestAutoFallbackRenderer` (11 tests
covering render_frame swap, IPC op swap, lazy Mock construction,
close idempotency, Respawned-doesn't-trigger-fallback regression
tests, etc.).

### `scripts/sidecar_smoke_driver.py` (Pi-side, ad-hoc)

30-min sustained smoke driver. **Per
`feedback_no_soak_during_dev`: run only at release-candidate
gating, not per-commit.** Logs to `/tmp/sidecar-*.jsonl` for
post-analysis. The 4 sustained-smoke reports in `qa/sidecar-*.md`
are the empirical baseline for the perf claims in §4.

### SD-burn workflow (`1aa6dfe` + `6291b49`)

End-to-end Mac-side SD card provisioning. Two layers:

- **`scripts/build_sd_bundle.sh`** (`1aa6dfe`): produces
  `dist/openmarquee-sd-bundle.tar.zst` with `backend/`, pre-built
  `ui/`, cross-built `bin/openmarquee-render` (if present),
  vendored aarch64 wheels, systemd units, hostapd/dnsmasq
  config, `install.sh`. Hard-refuses to bundle anything with
  secret-shape (`.env`, `.pem`, `id_rsa`, etc).
- **`scripts/stage_sd_card.sh`** (`1aa6dfe`): drops the bundle
  + cloud-init `user-data` / `meta-data` / `network-config`
  onto a mounted bootfs partition. Refuses if the mount path
  doesn't look like a Pi bootfs (no `cmdline.txt` / `config.txt`).
- **`scripts/burn_sd_card.sh`** (`6291b49`, new this session):
  collapses the prior two-step flow (Pi Imager GUI → stage) into
  one CLI command. Validates target via `diskutil info -plist`
  (rejects internal + non-removable + partition paths). Requires
  the operator to type the EXACT `diskN` identifier — no
  `--force` flag. Caches Pi OS Lite arm64 image at
  `$OPENMARQUEE_BUILD_DIR/cache/` (or `~/Library/Caches/
  openmarquee/`) with SHA256 verify + 30-day staleness gate.
  Flashes via `xz -dc | sudo dd of=/dev/rdiskN bs=4m` (raw
  device for ~5× throughput). Waits for `bootfs` auto-mount
  with 60s timeout + explicit `mountDisk` fallback. Calls
  `stage_sd_card.sh` (deliberately NOT under sudo, to preserve
  file ownership). `SIGINT` trap re-ejects + warns. Wall time
  estimate: ~5-8 min on USB 3.

### `scripts/tests/test_burn_sd_card.sh` (`6291b49`, 17 PASS)

Validation gauntlet for `burn_sd_card.sh`. Mocks `diskutil`
via a shim script on a tmpdir `$PATH`; real `plutil` parses
fixture plists so internal-vs-external classification runs
against real parser behavior. 8 test cases × 17 assertions
covering: missing target, `--help`, partition path rejection,
random-path rejection, **internal disk refusal** (the
wipes-mac-ssd guard), external+removable acceptance in
dry-run, missing-bundle bail, unknown flag rejection.

## 7. Open questions (NOT decided here)

These are qarl-direct items pending design calls. Listed so the
maintainer can scope around them but not so they get answered
without qarl input:

- **qarl visual eyeball pass on dev Pi `rust-sidecar`.** Code
  for TextSlide + ImageSlide + transitions + VideoSlide is all
  in tree + cross-build verified + 720p smoke captured (§4).
  The remaining gate before flipping `OPENMARQUEE_RENDERER=
  rust-sidecar` as the production default is qarl-on-glass:
  does video look right? do transitions look right? any visual
  regression vs the PIL `GPUSlideCompositor` baseline? Live-Pi
  smoke can verify numerics but not "looks right."
- **V4L2 piece 4 — DMA-BUF zero-copy.** Piece 3e uses
  `glTexImage2D` per-frame for both Y + UV planes (~3 MB upload
  per frame at 720p). Piece 4 imports the V4L2 CAPTURE buffer
  as an `EGLImage` via `EGL_EXT_image_dma_buf_import`,
  eliminating the upload. Likely fixes the p99-over-33 ms result
  from piece 3f's smoke. Multi-day arc; deferred until after
  the qarl visual pass since the current path is correct, just
  slow on the tail.
- **Default-flip decision** (slice 5). Once qarl signs off
  visually, flip `OPENMARQUEE_RENDERER=rust-sidecar` as the
  production default. The MockRenderer fallback via
  `AutoFallbackRenderer` covers process-exhaustion edge cases;
  the PIL `GPUSlideCompositor` path becomes legacy.
- **1080p re-test + HDMI EDID restore** — dev Pi EDID currently
  restores to 1024×768; the connected TV's native 1280×720 /
  1920×1080 modes aren't being negotiated. Office-glass-gated;
  per `project_phase7_pending_at_office`. The piece 3f smoke
  numbers above were captured at 1024×768 with the video
  texture scaled down via the GLES shader.
- **VideoSlide Capture / thumbnail path.** Currently still
  returns `"Capture: video slides TBD"`. Separate piece; would
  need to drive the decoder, capture one frame, readback as PNG.
  Drives the doc-vs-code gap noted in §3 (the
  `_UNSUPPORTED_SLIDE_WIRE_MARKERS` substring `"video slides
  TBD"` stays in the marker tuple until that lands).
- **Marquee 29.5 vc4 ceiling** (task #279). The Atlas SB
  sanity-capture work concluded that SB bake is NOT the
  bottleneck; the vc4 ceiling lives elsewhere. Decision still
  owed.

## 8. Cited commits

All SHAs verified present on `main` at refresh time
(2026-05-14 16:48 UTC, this refresh commit):

| SHA | Anchor |
|-----|--------|
| `8a2a4a0` | Phase 7 slice 1 — Python RustRenderer IPC proxy |
| `9693517` | Phase 7 slice 2 — factory branch |
| `cc66a5e` | Phase 7 slice 3 — install Rust IPC sidecar binary |
| `601820f` | renderer cargo unit tests pinning IPC sidecar wire-format errors |
| `d6b4f6a` | renderer IPC sidecar ImageSlide support |
| `0eba124` | scripts pkill stale renderer binaries before bring-up |
| `413efca` | Bug-1 motion-tick fix extended to IPC sidecar PaintSlide path |
| `f549e85` | Bug-1 motion-tick basis EglSession helper |
| `bdc7303` | qa sidecar sustained smoke 2026-05-13 (pre-cache baseline) |
| `125653d` | renderer per-phase boundary trace (`OPENMARQUEE_BOUNDARY_TRACE`) |
| `381fa49` | qa slide-boundary characterization 2026-05-14 |
| `b9cd44d` | renderer paint_slide_with_viewport sub-phase trace |
| `34e952d` | qa paint_slide internal profile — raster_us dominates |
| `9e776e7` | renderer hold-path slide_caches wire (IPC sidecar) |
| `12ce420` | qa post-cache-fix sustained smoke |
| `e6f914e` | renderer transition-path slide_caches wire |
| `a3da434` | qa post-transition-cache sustained smoke |
| `1796584` | backend RustRenderer reconnect + watchdog + health-probe |
| `0a81a2c` | backend AutoFallbackRenderer wrapper |
| `c067332` | scripts cache-regression gate |
| `555e6b9` | scripts pkill-self-kill fix (4 scripts) |
| `ce440ed` | docs Phase 7 as-built initial snapshot (this doc) |
| `6b86fcf` | docs renderer-rewrite-plan-rust.md → as-built pointer thread |
| `71079bb` | backend Phase 7 slice 4 wire playback.py to Rust IPC sidecar |
| `1aa6dfe` | scripts SD-burn flow (build_sd_bundle / stage_sd_card) + install.sh fix |
| `f481794` | backend slice-4 followup — begin_transition on the Rust IPC route |
| `3b6c3bf` | V4L2 piece 1 — dev Pi state inventory + decoder device-path doc |
| `343fe15` | V4L2 piece 2a — Decoder client scaffold + cap query |
| `5f67ea5` | V4L2 piece 2b — decode loop + buffer pool + Frame lifetime |
| `2dbe775` | V4L2 piece 3a — MP4 demuxer for H.264-in-MP4 |
| `c56793b` | V4L2 piece 3b — SlideCache.video_demuxers + BeginSlide(Video) wire |
| `89f9591` | V4L2 piece 3c — prime v4l2::Decoder on BeginSlide(Video) |
| `6ffcb33` | V4L2 piece 3d — NV12 → RGB BT.601 shader + program cache + blit pass |
| `e7be17f` | V4L2 piece 3e — paint_and_present_one_video_slide_frame end-to-end |
| `6291b49` | scripts burn_sd_card.sh — single-command Mac-side SD flasher |
