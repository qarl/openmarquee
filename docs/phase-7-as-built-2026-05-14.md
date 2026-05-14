# Phase 7 as-built — 2026-05-14

Snapshot of the Rust IPC sidecar architecture **as it actually shipped**
through the night of 2026-05-13 → 2026-05-14. Complements (does not
replace) `docs/renderer-rewrite-plan-rust.md`, which is the
forward-looking spec. Where the two disagree, this doc reflects the
code; the spec has drifted in places that are now load-bearing
(see §3).

Audience: a future maintainer (or qarl, when picking up the slice 4
design call) who needs the current state without reverse-engineering
~26 commits.

## 1. State of Phase 7

| Slice | Status | What it is | Anchor commit |
|-------|--------|------------|---------------|
| 1 | **Shipped** | Python `RustRenderer` IPC proxy (`backend/openmarquee/rendering/rust_renderer.py`) | `8a2a4a0` |
| 2 | **Shipped** | `dependencies.py` factory branch (`OPENMARQUEE_RENDERER=rust-sidecar`) | `9693517` |
| 3 | **Shipped** | systemd unit + `install.sh` staging for the binary at `/usr/local/bin/openmarquee-render` | `cc66a5e` |
| 4 | **Pending qarl** | `playback.py` bypass: stop pushing frames via `render_frame()`, drive IPC ops directly. Blocked on VideoSlide handling design call (task #75). | — |
| 5+ | **Pending qarl** | Flip default to rust-sidecar; remove embedded reel-driver fallback. | — |

Slices 1-3 are in tree but **OFF by default**. Production paths
unchanged until an operator sets `OPENMARQUEE_RENDERER=rust-sidecar`.
Slice 4 turns it on; until then the proxy refuses push-frame rendering
via `NotImplementedError` (by design — the proxy doesn't accept frame
bytes, the sidecar owns GPU composition).

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

## 7. Open questions (NOT decided here)

These are qarl-direct items pending design calls. Listed so the
maintainer can scope around them but not so they get answered
without qarl input:

- **Slice 4 — `playback.py` bypass shape for VideoSlide** (task
  #75). The proxy refuses push-frame rendering. The sidecar's
  current 7-op contract covers TextSlide + ImageSlide. VideoSlide
  routes through `paint_video_slide_to_png` (capture path only,
  per `d6b4f6a`); the per-frame video decode path is TBD.
- **V4L2 M2M decoder arc** (task #76). The sidecar will need a
  GPU-side decode path for VideoSlide if slice 4 wants to bypass
  push-frame rendering for video too. Currently scoped as a
  separate multi-day arc.
- **1080p re-test** — HDMI EDID restore on dev Pi (office-glass-
  gated). Cache wire is resolution-independent so gates should
  hold at 1080p, but the empirical baseline is pinned at 1024×768.
- **Marquee 29.5 vc4 ceiling** (task #279). The Atlas SB
  sanity-capture work concluded that SB bake is NOT the
  bottleneck; the vc4 ceiling lives elsewhere. Decision still
  owed.

## 8. Cited commits

All SHAs verified present on `main` at write time
(2026-05-14 04:30 UTC):

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
