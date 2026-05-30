# Changelog

All notable changes to openMarquee land here. Format follows
[Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer 2.0](https://semver.org/spec/v2.0.0.html)
with pre-release identifiers.

Components share a single version string across the backend
(`backend/openmarquee/__init__.py` + `backend/pyproject.toml`),
renderer (`renderer/Cargo.toml`), and UI (`ui/package.json`).

PEP 440 caveat: pip normalizes pre-release identifiers at install
time with a deprecation-warning print (e.g. `0.5.0-beta` → `0.5.0b0`,
`0.7.0-rc.1` → `0.7.0rc1`). Functional behavior is unchanged; the
literal version string is preserved across all four component
locations for cross-ecosystem readability.

## [0.9.0] - 2026-05-30

Cut at the close of the renderer perf-night arc + the FYS demo reel
polish round. Skips the 0.7.x and 0.8.x version slots that internal
development cycled through — the project went from `v0.6.0-beta` to
`0.7.0-rc.1` (development version, never tagged) and now to `0.9.0` as
a release-candidate-for-1.0 stamp.

HDMI output ships in 0.9.0. HUB75 / WS2812B / composite panel modes
are explicitly deferred to v1.x — their Python drivers were removed in
the v0.6 DELETE-PIL purge and the Rust ports are post-1.0 work. Stream
Mode B network-source playback controls (pause / seek / VLC source)
and the originals/deployed storage split are also deferred.



### Backend

- `77bc965` BACKEND (perf P2 — PlaybackLoop._wait). Single
  `wake_event` race replaces the prior 2-Task `asyncio.wait(
  FIRST_COMPLETED)` pattern. At the 30Hz playback tick the old shape
  cost ~0.45% of one core in handle-allocation + event-loop
  bookkeeping (per QA's perf-resweep v2 P2). New shape uses
  `asyncio.wait_for(self._wake_event.wait(), timeout=seconds)` with
  `stop()` and `pause()` both setting the wake event so any in-flight
  `_wait` returns promptly. Microbench: 2 Tasks/call → 0 Tasks/call
  (CPython 3.11+ `wait_for` uses `asyncio.timeout()` + `loop.call_at`,
  no Task wrap); ~42% net overhead drop (157→91 µs/call on local
  Mac CPython 3.13.5). 7 new test-first equivalence tests in
  `test_playback.py` pin sleep cadence + mid-wait wake (stop and
  pause) + race-on-entry (stop and pause) + zero-second short-
  circuit + cancellation propagation.
- `a1534de` TESTS (close 2 THIN modules in one bundle). Extends
  `test_tailscale_self.py` with 8 logical tests (15 cases with
  parametrize) covering `_query_self_fqdn`'s subprocess + parse
  path (missing binary, timeout, OSError, non-zero exit, non-JSON
  stdout, missing/wrong-shape `Self`, trailing-dot normalization,
  empty/whitespace DNSName). Extends `test_dev.py` with 4 tests
  covering the dev-preview UX contract (polling JS setInterval,
  cache-buster query param, 404 detail message, renderer-bytes
  round-trip). Backend coverage audit tier moves from `0 GAP / 2
  THIN / 35 GOOD / 8 DEEP` to `0 GAP / 0 THIN / 37 GOOD / 8 DEEP`.
- `65414ce` TESTS (storage_recovery — close the single GAP). 8 unit
  tests for `quarantine_corrupt_file()` covering happy path, source-
  missing pre-condition, OSError-during-rename, ISO-UTC-Z filename
  pattern, success-WARN log, byte-identical content preservation
  (binary + invalid-UTF8), prior-quarantine-sibling coexistence,
  UTC-vs-local timestamp regression lock. Closes the only GAP
  module from the 2026-05-24 backend coverage audit (45 modules /
  72-LOC helper). Keeps the existing integration test in
  `test_playlist.py` as wiring coverage (no migration).

### Renderer

- `2ff8479` RENDERER (perf P1 — `draw_text_layer_msdf` VBO cache).
  Replaces the per-text-layer `create_buffer` / `delete_buffer` pair
  at the 4 sub-batches (msdf-ink / tofu / dynamic-msdf / dynamic-
  emoji) with a single thread_local-cached `NativeBuffer` for the
  session. Mirrors the existing `cached_msdf_program` /
  `cached_tofu_program` / `cached_emoji_program` Cell<Option<T>>
  pattern. Per-call GL handle churn drops from 4 creates + 4 deletes
  to 0 (one create at session bring-up, one delete at teardown via
  `clear_msdf_text_vbo_cache`); ~0.2% of one core on Pi Zero 2 W
  (per QA's perf-resweep v2 P1, scales with text-layer count per
  frame). `cargo test --release` 542 passing post-refactor.

### UI

- `cae645e` UI (main.js Path C slice 1 — testability extract).
  Pulls `buildEditRoutes` + `runDeleteCascade` out of `main.js` into
  testable helpers (jimmy:openmarquee-code2).
- `07315d1` UI (slides.js behavioral test suite). 11 tests across 4
  describes covering the slide-browser surface (jimmy:openmarquee-
  code2).
- `4a086a9` UI (playlist-track Sortable race). Ports `editor.js`'s
  Sortable bind-generation guard to `playlist-track` to fix a
  refresh-race leak (jimmy:openmarquee-code2).
- `453d87a` UI (a11y + loading-UX polish bundle 3). Layer-eye
  `aria-pressed` + upload async disable + tailscale polling
  timeout (jimmy:openmarquee-code2).
- `dcc90e5` UI (a11y polish bundle 2). Edit button `aria-label` +
  default-playlist context + upload-input action verbs (jimmy:
  openmarquee-code2).

### Tooling

- `abc1ab5` TOOLING (pin `ruff==0.15.14`). Was floating at
  `ruff>=0.3`, which meant `pip install -e '.[dev]'` resolved to
  whatever ruff version pip's resolver picked at install time on
  each runner. On 2026-05-24 alone, four separate CI-parity
  commits landed solely to absorb that drift (`8f77001 1d2887e
  e7f3425 6126ca3`). Pinning eliminates the class. Pin propagates
  to CI automatically via the existing dev-extras install (both
  backend and e2e jobs). Includes a date-anchored rationale comment
  + bump-protocol ("run `ruff format .` locally with the new
  version first").

## [0.7.0-rc.1] — 2026-05-24

Release-candidate cut covering the post-`v0.6.0-beta` arc. Mix of
operator-visible fixes (D1-redux black-not-black, Live Mode A pause /
resume, hostname-aware camera-permission banner), system-level
mitigations for the FYS Pi's intermittent wifi loss (a seven-commit
watchdog stack), HTTPS-on-Tailscale Phase 1, an overnight CSP audit,
plus the CI green-up sweep needed to get all 1389 backend tests
passing for the first time in this arc.

### Backend

- `0e10058` BACKEND+UI (M1 — Content-Security-Policy middleware).
  Single source of truth `DEFAULT_CSP_POLICY` + `CSPMiddleware` ASGI
  class stamps `content-security-policy` on every HTTP response.
  Env-gated report-only mode via `OPENMARQUEE_CSP_REPORT_ONLY=1`.
  Two pre-commit inline-script extracts (parity-harness.html +
  fake-camera.html, 835 LOC of inline JS lifted to `.entry.js`
  bundles) so the policy doesn't break existing pages.
- `f72c49f` + `6307d4a` CSP follow-up: allow `blob:` in `script-src`
  + `connect-src` for the e2e ffmpeg.wasm dynamic-module path
  (browser-side fixture-video generation).
- `bbd64c5` BACKEND (D1-redux v1) + `6c5de9a` (widen). Seed-data
  rewrite of `#050608` → `#000000` plus an env-gated content
  migration (`OPENMARQUEE_MIGRATE_050608_BG=1`) that walks all three
  bg-carrying fields: `slide.background_color`,
  `background_pattern.color_a`, `background_pattern.color_b`. The
  v1 commit only touched `background_color`; live-fire showed
  intermittent lifted-black on pattern-using slides, fixed in the
  widen. Pre-Bug-7 (Broadcast RGB Limited) the `#050608` and
  `#000000` were visually identical; post-Bug-7's full-range fix
  the `(5,6,8)` lift became visible. **True black confirmed on FYS
  glass post-deploy** via `/api/playback/current-frame` corner-pixel
  probe (`(0,0,0)` everywhere, `(5,6,8)` count = 0).
- `ac47242` LIVE (Fork B — operator pause / resume on Mode A
  takeover). New `LiveSession._paused` flag + `pause()` / `resume()`
  methods; the pump loop drains the source but skips render while
  paused (DRM scanout holds the last frame; upstream stays alive).
  `_first_frame_event.set()` moved to the top of the pump loop so
  the watchdog sees proof-of-life even if the operator pauses
  pre-first-frame. New `POST /api/live/{pause,resume}` endpoints +
  `LiveStatus.paused` field + UI Pause button with optimistic flip
  + in-flight debounce.
- `9bc63cd` BACKEND+WIFI-WATCHDOG carry-forwards. PerfMiddleware
  re-ordered to be outermost so the ring records auth-rejected
  401s (previously the Auth middleware short-circuited before Perf
  saw the request); watchdog gains a tick-start `iw get power_save`
  warning check so re-introduced power-save shows up in the log.
- `18f585b` BACKEND+UI (M5 closure). Live-panel Cancel no-backend-
  call contract lock + decision-record comment at `live-panel.js:
  1101`. Subagent review caught a real silent-contract regression
  where the initial assertion list used import names rather than
  the `api*`-aliased in-scope names — would have shipped useless
  green.
- `5c2cea2` BACKEND (web_render). Restore chromium process-group-
  kill (originally `88398f8`) from a half-merge that landed the
  tests but dropped the source-side `Popen` + `start_new_session`
  + `killpg` triple. Group A — 28 CI failures.
- `e7c0b50` BACKEND (3 fixes). Migration's detection-then-update
  split simplified per subagent nit; `test_migration_is_idempotent`
  extended to cover both bg + pattern paths; `_NOW` in
  `test_flock_sync.py` switched from hardcoded `datetime(2026, 4,
  24, ...)` to `datetime.now(UTC).replace(microsecond=0)` (the
  hardcoded date had aged past the 30-day tombstone TTL, causing
  two CI failures every day). **First all-green CI of the arc.**
- `6e226e9` renderer: BT.601 → BT.709 comment correction. The
  matrix coefficients were updated 2026-05-14 but the header
  comment was missed; pure docs.
- `be7dbcb` BACKEND: `pip-compile` lock refresh — fixes idna +
  starlette CVEs plus 6 routine bumps.

### System / Ops

- **Wifi-watchdog seven-commit stack** addressing FYS-Pi wifi loss
  that was causing ~7-minute reboot cycles. From baseline to ~12-22
  minute stable windows (~30-50% reboot reduction in the morning
  Path 1 arc; further loosened post-move in the evening when the
  sign was still bouncing); underlying RF / hardware root cause
  remains qarl's investigation:
  - `f9ca5f9` burst-ping (replace single-ping with 5-ping burst,
    pass on ≥3 of 5) — stops the false-positive reboot loop.
  - `e25e787` Path 1 widen-envelope: `REBOOT_AFTER_N_RESTARTS` 3→5,
    `REBOOT_WINDOW_SECONDS` 600→1800 — absorb sustained-but-
    transient bad-RF pockets.
  - `f4bcac8` Path 2 modprobe-cycle tier: `rmmod + modprobe
    brcmfmac` escalation between NM-restart and reboot — cheaper
    recovery than a full reboot when it works.
  - `8a04c28` flock wrap: `/usr/bin/flock -n /var/lock/wifi-
    watchdog.lock` on both cron invocations — serializes concurrent
    cron firings to fix the back-to-back NM-restart race.
  - `1b46639` wcc fix: `modprobe -r brcmfmac` instead of `rmmod
    brcmfmac` — handles the `brcmfmac_wcc` sub-module reverse-dep.
  - `d22addf` Option D doc lock: 17-line `KNOWN LIMITATION`
    docblock fencing the dual-mode (STA + AP for captive-portal)
    no-op. On FYS, `hostapd` on `ap0` holds a `brcmfmac` reference
    `modprobe -r` can't break; mitigation falls through to reboot
    tier. Documented to prevent future "fix" attempts.
  - `e2beb57` Second-widen tune (post-move bounce-back): envelope
    further loosened from 5-in-1800s to 8-in-3600s; burst-loss
    threshold tightened from 60% (3-of-5 OK) to 80% (2-of-5 OK).
    Auto-reboot stays enabled — qarl wants the path to fire for
    genuinely-wedged chips — but the trigger surface is now much
    rarer. New `test_burst_threshold_is_80_percent` semantically
    locks the OK_MIN/COUNT pair into producing a ≥80% loss FIRE
    threshold.
- `97d36fc` BACKEND+SYSTEM (HTTPS Phase 1). `tailscale serve --bg
  --https=443` plus a `FqdnRedirectMiddleware` that 301s non-FQDN
  hostnames to the canonical Let's-Encrypt-issued FQDN. New
  `settings.tailscale_https_enabled` (default `True`); awaiting
  per-device admin-console toggle by the operator.
- `56767a4` OPS (network mitigation 2). Wifi-watchdog gains an AP-
  deauth-detection path — was previously a no-op.
- `69cd5e9` OPS (network mitigation 3). Backend skips Web-slide
  render when system memory pressure exceeds the threshold (avoids
  chromium OOM cascading into a reboot).
- `d3ede6b` OPS (network mitigation 4). Auto-reboot on watchdog
  escalation (now superseded by the Path 1+2 stack above but ships
  in this cut as part of the iterative arc).
- `7159bab` OPS (network mitigation 5). Strip
  `cgroup_disable=memory` from `cmdline.txt` so memory pressure
  signals actually fire.
- `387d71c` SYSTEM (TZ drift postmortem #8). Standardize Pi log
  timestamps on UTC via `date -u` at six log-emitter sites + an
  auto-catch test. **The Pi system TZ remains local** — the
  schedule.py rule evaluator uses naive `datetime.now()` and
  changing `timedatectl set-timezone UTC` would silently shift
  every operator-configured schedule window.

### UI

- `4a948fb` + `0afd012` LIVE PANEL camera-permission banner. First
  commit surfaces the missing-`navigator.mediaDevices` Safari /
  HTTP-context case as an actionable banner instead of a console
  error; second makes the message hostname-aware via injectable
  `getHostname` (HTTPS-redirect copy vs HTTP-context copy).
- `1b00db8` UI (H4 closure). Parity-harness gains 7 transitions
  (iris, scanline, glitch, push, flip, marquee, shutter) as per-
  pixel JS translations of the Rust SP fragment shaders. Closes
  the 7 BROWSER-SKIP fixtures.
- `cbcb376` UI (Bug 2). Playlist add / delete / rename now propagate
  to the schedule UI; `listPlaylists` cache-busted.
- `08d3ca3` UI (Bug 3). Tag-qualify the bare `.live` selector
  (`section.live`) to break the `.om-pill.live` collision a class-
  rename had silently created.
- `03d588c` + `6c5d79a` UI (Bug 5 — web-slide inline preview). Two-
  commit fix: editor renders the saved slide's `asset.png` inline,
  then `@font-face` load + cache-invalidate so thumbnails refresh
  post-font-load.
- **Dead-CSS sweep waves** (~819 LOC, ~81 selector groups). The
  97-candidate orphan list landed earlier this arc is now
  exhausted: `7948bd2` (497 LOC / ~55 classes), `a52b141` (298 LOC
  / ~23 classes), `11cb751` (5 `.bg-*` theme-exploration classes),
  `9d1e211` (`.om-slide-text` + `.font-*` + `@keyframes om-scroll`,
  24 LOC).
- `4c65de2` UI E2E (settings-remount). Drop dead `.settings-save`
  clicks — settings now auto-save on input / change.
- `1200cfb` UI E2E (fonts). Bless 23 chromium-linux font-snapshot
  goldens (post-MSDF baseline).
- `bca642c` UI E2E (change-secret-flow). Drop the obsolete
  tailscale-auth-key Change → Cancel test (UX flow superseded).
- `d9d982f` UI E2E (auto-slide). Click the segmented Time button
  instead of the stale `selectOption` (UI refactored to chip pills).

### Tooling / CI

- `d2d322c` CI: bump Python 3.11 → 3.13 + pip-tools 7.5.3 +
  surface lock-drift errors (the previous `2>/dev/null` was
  swallowing the real failure into a phantom 103-line diff).
- `ccea63b` CI: add `build-wasm` job + artifact handoff to unblock
  `ui/e2e` which depends on the gitignored `renderer-wasm/pkg`.
  Cache key is wasm-pack version-checked.
- `9f6ec95` + `499e206` CI: install ffmpeg on backend + e2e jobs
  (fixes ffprobe-missing test failure + fixture-video generation).
- `f24c056` CI + playwright: HTML reporter on CI + upload
  `test-results/` actuals (lets us inspect failures without a
  full local Playwright environment).
- `0768886` SCRIPTS (`build_wasm_renderer`). Emit
  `pkg/package.json` with `"type": "module"` so Node treats the
  wasm-pack `--target web` output as ESM (default is CJS via
  parent-`package.json` walk; wasm-pack doesn't emit one).
- `095ea71` SCRIPTS (`sweep_orphan_chunks`). Swap
  `find -regex '.*\.\{N\}'` for `find -name 'GLOB' | grep -E`
  — the GNU `find` on Ubuntu CI returns empty where BSD `find`
  on macOS matches.
- `1fad8ea` TESTS: `chmod +x` 3 scripts + skip-gate `test_bake`
  on wasm-pkg presence.
- **Ruff cleanup arc** for the strict-CI gate:
  - `fe29851` (lint --fix + F821 UUID import).
  - `4777cc9` (`ruff format .` whole-suite formatter sweep, 77
    files).
  - `9687d22` (residual 20 cleared — defer-and-document wasn't
    viable under the strict gate).
  - `7ad29d7 8a5e884 87b44e7 8f77001 e58a7b8 c1fef9f 1d2887e
    e7f3425 6126ca3` (formatter version drift residuals + post-
    merge-conflict tidies).

### Notes

- **CI status at cut**: 1389 passed, 3 skipped, 0 failed.
- **Process invariants validated this arc**: pre-commit subagent
  review (caught the Fork B watchdog-pre-first-frame bug, the CSP
  inline-script blockers, and the M5 import-name vs in-scope-name
  assertion gap), pre-commit `git diff --cached --stat` (instituted
  after `9bc63cd` accidentally bundled 18 unrelated stash phantoms),
  surface-first scope before editing (caught a dispatch referencing
  a non-existent memory file before any edits hit disk).
- **Deferred to next arc** (not blockers for `rc.1`): HTTPS short-
  name daylight (mkcert + per-device root-CA scope, ~3-4h, deferred
  until qarl asks); systemd `StartLimitBurst` 5-in-10s hazard on
  `openmarquee-backend.service` (recommend
  `StartLimitIntervalSec=60` + `StartLimitBurst=10`, surfaced in N1
  baseline, not RC-blocking).

## [0.6.0-beta] — 2026-05-23

### Overnight stability + Mode A push (2026-05-23)

Mode A (Live takeover) end-to-end on FYS hardware, chromium subprocess
reap-leak fix, parity harness coverage expansion, and seven additional
regression-locks that close standing audit deltas. Cross-pair commits:

- `88398f8` BACKEND (web_render): kill chromium process group on render
  exit. Fixes the FYS swap-thrash leak where each Newsmoji refresh
  accumulated ~8 chromium helper subprocesses; `subprocess.Popen` with
  `start_new_session=True` + `finally: os.killpg(SIGTERM)` reaps the
  whole group. New `chromium_render_no_orphan_procs` regression test
  (skip-gated to Linux + chromium-headless-shell available).
- `3a4fd22` / `860275d` Live takeover panel renamed from "Stream" back
  to "Live" (Mode A). StreamSlide (Mode B, `type: "stream"`) is
  explicitly unchanged. API route prefix flipped `/api/stream/*` →
  `/api/live/*`; error code `stream_already_active` → `live_already_active`.
- `dbfe0d4` Live Mode A test harness — `ui/test/fake-camera.html`
  single-file fake-camera publisher (captureStream of a bundled
  fixture.mp4) + auth-whitelist regression-lock for `/test/` paths.
- `8c2b127` Live signaling fix — `AF_NETLINK` added to systemd unit's
  `RestrictAddressFamilies=` (aiortc/aioice needs netlink for ICE
  candidate gathering; OSError errno 97 EAFNOSUPPORT was 400'ing
  /api/live/start until this landed). `/api/live/{start,takeover}`
  400 detail now structured `{error, error_class}` so wire-side
  diagnosers see the failing exception class. **Mode A confirmed
  end-to-end working on FYS** via fake-camera harness against real
  hardware (70 frames painted to HDMI through the Rust sidecar's
  external-frame pump, 0 skipped, avg 35ms paint).
- `6b6f4e8` / `8d5c006` Live Slice 4 regression tests — static
  config assertion (`test_systemd_unit_whitelists_af_netlink`) +
  real aiortc client SDP round-trip lock against `httpx.AsyncClient`
  + `ASGITransport(app)`.
- `84d8d6a` RENDERER+BACKEND (reconfigure IPC): partial op
  (brightness + gamma in-place via shader uniforms; rotation typed
  `UnsupportedField` error). Replaces the long-standing "not yet
  implemented (slice e)" stub at `ipc_main.rs:1705`. Closes
  v1-spec-delta CRITICAL/MAJOR open item.
- `b9e6b67` RENDERER (reel): H2+M2 dispatch parity. Standalone
  `--play-reel` path lifts video-decode + image-involving-transition
  dispatch out of `ipc_main.rs` into new shared `video_decode.rs`
  module; reel arm routes Video through V4L2 (graceful black-hold
  fallback on /dev/video10 absent) and image-bg transitions through
  the existing any-endpoint mix path. Text-Text transitions stay on
  the QA-mandated SP/SB fast-path.
- `be3d10b` UI (D2 closure): chip-pill universal-application
  regression lock. The 2026-05-19 `.om-pulldown` migration already
  shipped Option X; this commit codifies that state with a
  static-parse test (every `<select>` in `ui/src/*.js` either wears
  `.om-pulldown` or is the documented hidden font-family pattern).
- `18f585b` UI (M5 closure): Live Cancel TODO replaced with
  decision-record comment + no-backend-call contract lock. Subagent
  review caught a real silent-contract regression (the lock's
  initial `_BACKEND_CALL_NAMES` list used import names instead of
  the `api*`-aliased in-scope names — would have shipped a useless
  test green).
- `6230321` RENDERER (D1 closure): `FS_BRIGHT_GAMMA` blacks-not-black
  regression-lock. Probe ran on vc4 + confirmed `pow(0.0, 1/2.2) ==
  0.0` exact-zero (no epsilon needed). Four invariants pinned:
  pre-pow clamp present, clamp ordering, audit-anchor comment,
  Option-B anti-pattern (asserts the `step(rgb, vec3(1e-6))`
  signature is NOT present).
- `1b00db8` UI (H4): parity-harness gains 7 missing transitions
  (iris, scanline, glitch, push, flip, marquee, shutter). Per-pixel
  JS translations of the Rust SP fragment shaders; subagent review
  verified line-by-line algorithm equivalence for each. Unlocks
  the cross-renderer parity gold-bless gate for the previously
  BROWSER-SKIP'd 7 fixtures.

**Six audit items closed as audit-stale + already-locked** (H3, M1,
D1, M3, D2 work-already-done, M5 work-already-wired): existing
regression-lock tests fence the invariants; verify-against-HEAD-blob
discipline caught the audit-doc lag.

### SDF arc — text rendering refactor (2026-05-17 → 2026-05-18)

Replaced the per-frame AlphaBitmap font raster with build-time-baked
MSDF atlases + Noto Color Emoji CBDT pages. Resolves the font-clamp
bug (renderer/src/hdmi.rs's `MAX_RASTERIZED_BITMAP_DIM=2048` ceiling
that capped large text on vc4) and unlocks per-codepoint color
emoji rendering. Cross-pair commits:

- `a362a75` parity: re-bless 23 FYS goldens post-wrap (12 fixtures
  improved) — preparatory work before the SDF arc proper.
- `ce4457c` SDF A (atlas generation): `build.rs` bakes per-font
  MSDF atlases at build time.
- `ae39fe6` SDF B.1 (infrastructure): `AaMode` CLI flag + MSDF
  shaders + `sdf_atlas` module.
- `85982aa` SDF B.2 (cutover): MSDF text path wired through
  `paint_slide`. vc4-aware default = `FIXED` (no
  `GL_OES_standard_derivatives`).
- `c09bdad` SDF B.3 (AlphaBitmap retirement + SP-text gate-off
  + tofu): the AlphaBitmap renderer path is gone; missing-codepoint
  glyphs fall through to `FS_TOFU`.
- `9b3c772` SDF C.1 (emoji atlas bake): `build.rs` extracts Noto
  CBDT into multi-page RGBA8 atlases. Covers `U+1F000-1FFFF` plus
  `U+2600-27BF` per `ui/styles.css`'s @font-face unicode-range.
- `d6fe65d` SDF C.2 (emoji atlas runtime): GL upload + `FS_EMOJI`
  shader. 4 atlas pages × 2048×2048 RGBA8 (~64 MB on GPU,
  PNG-compressed in-binary).
- `b8443b2` SDF C.3 (emoji layout segmentation): `GlyphKind` enum
  per-codepoint dispatch + 3-batch draw with per-page emoji
  batching via `BTreeMap`. Visible-behavior slice.
- `0fd1435` SDF C.4 (parity baseline): `parity_text_emoji_basic`
  cross-renderer fixture with `Hi! 🌟` exercising mixed MSDF +
  emoji. Outer-repo companion: `920bc70` SYSTEM_SPEC §5.10a
  documenting the text rendering pipeline.
- `abf6092` SDF D.0 (smoke): brightness reactivity threshold
  `0.85 → 0.90` (architectural baseline per bisect) + bash 3.2
  portability fix in `renderer_pi_soak.sh`.
- `a5d7759` SDF D.0b (smoke): soak gate skipped in dev
  (`SMOKE_SOAK_DURATION_SECS=0` default; release-candidate-only)
  per `feedback_no_soak_during_dev`.
- `5c750b7` SDF D (parity re-bless): 46 fixtures re-blessed against
  the MSDF binary. Drift profile 2.6–53% across all text-bearing
  fixtures; 3 (image_slide, video_slide, text_emoji_basic) were
  bit-identical pre/post.

DEPLOYED to FYS Pi (qarl@192.168.1.67) 2026-05-18 in slice E.
Binary md5 `af0ca88f32bfba4de38fe418b9d0ee1b` (42 895 376 bytes)
deployed to both `/usr/local/bin/openmarquee-render` and
`/opt/openmarquee/bin/openmarquee-render`. The 37-slide FYS reel
runs cleanly post-deploy; 36 of 37 cross-renderer `parity_fys_*`
goldens re-blessed against the SDF binary (1 deferred:
`parity_fys_t14` uses the `glitch` transition kind, which the
SP-portable set doesn't support via `--capture-sb-mid`).

THE FONT-CLAMP BUG IS RESOLVED IN PRODUCTION as of this deploy.

### DELETE-PIL purge (2026-05-17)

The Python rendering subsystem has been deleted. The Rust IPC
sidecar (`renderer/`, binary `openmarquee-render`) is now the only
production rendering path. Eight slice-commits landed in this purge:

- `67cea75` DELETE-PIL 10a: HUB75 + WS2812B LED renderers
  (`backend/openmarquee/rendering/{hub75,ws2812b}.py`, ~500 LOC).
  LED output OFFLINE on HEAD until Rust LED ports land.
- `70a4865` DELETE-PIL 10b: shader compositor + snapshot cache
  (`shader_compositor.py` 1560 LOC + `snapshot.py` 446 LOC +
  playback.py shader/snapshot path, ~3200 LOC total). Shader
  transitions OFFLINE; PIL fallback transitions still render the
  same visual at ~10 fps instead of 30.
- `b320dfd` DELETE-PIL 10c: multi-plane GPU compositor
  (`gpu_compositor.py` 901 LOC + playback.py GPU dispatch path,
  ~2200 LOC total). DRMRenderer slides fall through to PIL
  software path.
- `53b5c30` DELETE-PIL 10d: DRMRenderer + DRM/KMS Python primitives
  (`drm_kms.py` 2073 LOC + dependencies.py rework, ~2350 LOC).
  Production now routes through the Rust IPC sidecar by default.
- `6b3ee6a` DELETE-PIL 10e: HDMI framebuffer + composite renderers
  (`hdmi.py` 268 LOC + `composite.py` 81 LOC + tests, 797 LOC total).
- `fb47da5` DELETE-PIL 10f: PIL strip from MockRenderer via
  stdlib zlib PNG encoder. Live-preview dev path preserved.
- `b3bf9c6` DELETE-PIL 10g: blend.py + motion.py blend code path
  (198 + 168 LOC). Non-normal blend modes degrade to
  `alpha_composite` on the dev/CI software path; Rust IPC sidecar
  handles them natively on production.
- `adea339` DELETE-PIL 10h: settings.py / api_system.py / flock.py
  LED-mode scrub. `OutputMode` Literal collapsed to `["hdmi"]`;
  legacy on-disk `output_mode in {"hub75", "ws281x", "composite"}`
  values coerce silently to "hdmi" on settings load.

**Cumulative deletion:** ~10000 LOC of Python rendering subsystem
removed. The PyOpenGL dependency is gone.

### Deferred to next arc

- playback.py PIL fallback paths (`_play_dynamic_slide_software`,
  `_safe_load_image`, `_render_image`, transition methods).
- MockRenderer IPC-shape conversion.
- bake.py `compose_motion_frame` PIL usage.
- These three lift together once MockRenderer adopts the IPC ops
  contract; doing the playback teardown without it would break
  the dev/CI fallback path.

### Docs

- `docs/historical/` directory created. Moved superseded design
  docs (shader compositor, multi-plane GPU compositor, original
  Phase 1 plan + spike data + status log) under it.
- README, docs/README, renderer-rewrite-plan-rust, renderer-rewrite-
  requirements, phase-7-as-built-2026-05-14 updated with DELETE-PIL
  banners pointing at the historical archive.

## [0.5.0-beta] — 2026-05-17

First tagged release. Tag fires after slice 4 (README rewrite) +
slice 5 (`git tag` + GitHub release artifacts) per the Phase E recon
at `qa/captures/phase-e-release-prep-recon-2026-05-17.md` (commit
0734416).

### Major arcs shipping in this version

Cumulative scope across pre-tonight work plus the 2026-05-16/17
session. Spec-of-record:
[`SYSTEM_SPEC.md`](../SYSTEM_SPEC.md),
[`docs/renderer-rewrite-requirements.md`](docs/renderer-rewrite-requirements.md).

- **Phase A — Auth gate.** First-run welcome → set-password →
  login flow. Bearer-token middleware with route allowlist for
  peer / media / health paths. argon2id password storage tuned for
  Pi Zero 2 W (OWASP-floor params, see
  `project_pi_argon2_params`). Stale-token vs not-configured 401
  detail distinction so the UI can route correctly.
- **Phase B — Captive-portal SD bundle.** End-to-end flashable
  image: pi-gen config + cloud-init seed + `install.sh` + firstboot
  oneshot generating a per-device AP password. `scripts/build-
  image.sh` / `scripts/flash-sd.sh` for the burn workflow.
- **Phase C — WiFi-during-flash prefill.** Optional credentials
  shipped into the image at flash time so the device boots straight
  onto the operator's home network without the captive-portal
  detour. NM-keyfile bypass path per
  `qa/captures/cloud-init-wifis-investigation-2026-05-15.md`.
- **Phase D — Strict-30 fps ship gate.** IPC sidecar
  `paint_us_p99` instrumentation + soak parser gate at p99 ≤
  33333µs (= the literal 30 fps frame budget). Rolling-10min window;
  also gates OOM + crash signals. Spec hook:
  `docs/renderer-rewrite-requirements.md` §11 + §8.2.
- **Renderer rewrite — Rust shader compositor.** Phases 4 – 9
  delivered the EGL+GLES2+dmabuf single-pass shader pipeline
  replacing the Python software compositor for HDMI mode. Multi-
  plane DRM remains the within-slide layer compositor. Motion
  through transitions canonical on all 5 paths (IPC PaintTransition,
  legacy 3-pass, single-pass, scissored-bake, Canvas2D inline
  preview). Python `rendering/` tree on its way out per the
  DELETE-PIL phases visible in recent commits.

### Added

- (commit 8a2e043) Renderer IPC sidecar emits `paint_us_p99` in the
  `ipc.soak` 30s summary line. Backward-compatible additive field;
  pre-Phase-D parsers ignore it via regex-by-key.
- (commit f03ee91) `scripts/renderer_pi_soak_ipc_parse.py` gains
  `--max-p99-paint-us` (default 33333µs) + `rolling_max_p99`
  helper. Backward-compat: captures without `paint_us_p99` skip the
  p99 gate with a warning rather than failing.
- (commit 3226e7a) `ui/welcome.html` adds a `.welcome-continue`
  Continue CTA on fresh-device welcome flow → /set-password.html.
- (commit 6b13db1) `backend/openmarquee/auth_middleware.py` gains
  signed-URL fallback for media routes; UI gains `mediaSrc(path)`
  helper for query-param token passing.
- (commit 1a8a40b) Flock panel self-tile thumbnails route through
  `apiFetch`; peer cross-origin thumbnail reads land in the
  auth middleware whitelist.
- (commit 4e2187d) First-run 401 from `/api/*` calls routes the UI
  to `/welcome.html` (not `/login.html`).

### Changed

- (commit 831f471) Phase 4w: legacy 3-pass transition path
  documentation corrected. Per-frame live re-bake of animated
  layers has been canonical since 2b0cbef (2026-05-07); the prior
  audit doc at
  `qa/captures/motion-through-transitions-audit-2026-05-16.md`
  incorrectly flagged Path #2 as a static-snapshot freeze. Added
  comment-only documentation fixes + regression test
  `legacy_3pass_transition_re_bakes_animated_layers_per_frame` +
  audit-doc correction note. No functional code change.
- (commit a4a6c5b) Renderer slide cache invalidates entries when
  `item.json` mtime drifts on disk. Closes Bug 1 (Slide edits
  didn't propagate to HDMI until the playback cycle restarted).
- (commit 6fb417b) `ui/vitest.config.js` extends `configDefaults.
  exclude` with `**/._*` + `**/.AppleDouble/**` patterns so macOS
  NFS/SMB resource-fork files in `ui/src/` no longer appear as
  failed test suites.
- (commit da49549) `ui/src/editor.test.js` jsdom canvas mocks
  gain a `measureText` stub. Closes 5 pre-existing test-environment
  failures (4× direct + 1× cascading via uncaught throw in the
  textarea-input rAF path).

### Fixed

- (commit 447f049) **Bug 6 — stream-panel cancelTakeover race.**
  A deferred rAF-driven `mountInit()` could resume *after*
  `cancelTakeover()` set `state.phase = "idle"`, walk through
  `requesting-camera` → `preview`, and silently revive the panel —
  violating the explicit "operator chose Cancel = I want out"
  contract at `stream-panel.js:817-823`. Fix: per-handle
  `mountInitCancelled` flag latched in `cancelTakeover()` before
  the phase flip; propagated to all three mountInit gates
  (post-status, post-camera success, camera-fail catch). Determin-
  istic regression test forces the race window open via explicit
  Promise controllers.
- (commit a4a6c5b) **Bug 1 — slide edits don't propagate.** Item
  cache returned stale entries when `item.json` was edited mid-
  playlist. Now invalidates on mtime drift.
- (commits 3226e7a + 4e2187d) **Bug 2 — welcome.html dead-end.**
  Welcome page lacked a forward CTA on fresh devices. Continue
  button now routes to set-password; first-run 401 routes to
  welcome instead of login.
- (commit 6b13db1) **Bugs 3 + 4 — media thumbnails 401.** Bearer-
  token header didn't survive `<img src=...>` GET requests; added
  query-param signed-URL fallback for `/api/content/*/asset` +
  `/api/content/*/thumbnail` routes.
- (commit 1a8a40b) **Bug 5 — flock panel thumbnails 401.** Self-
  tile reads now use `apiFetch`; peer cross-origin reads land in
  the auth middleware whitelist.

### Security

- All `/api/*` routes (except an explicit allowlist of peer-
  callable + media-signed-URL + health paths) require a valid
  bearer token via the `AuthMiddleware`. Tokens are minted at login
  with a version prefix; bumping the version invalidates all
  outstanding tokens.
- Per-device AP password generated at firstboot (Phase B). No
  shared default across flashed images.

### Documentation

- (commit 2f2924a) Phase D recon at `qa/captures/phase-d-strict-
  30fps-recon-2026-05-17.md`. Maps spec §11 + §8.3 against the
  shipping `paint_us_p99` instrumentation.
- (commit ed43a63) Phase D shipped-note appended to the Phase D
  recon doc.
- (commit 0734416) Phase E recon at `qa/captures/phase-e-release-
  prep-recon-2026-05-17.md`. Cumulative coverage audit + slice plan
  for this version's release-prep arc.

<!--
Compare/release URLs assume the canonical GitHub remote at
github.com/openmarquee/openmarquee. The remote is not yet
configured at the time of this entry (slice 3); these links go
live with slice 5 (git tag + GitHub release artifacts). Until
then they will 404 — treat as placeholders.
-->
[Unreleased]: https://github.com/openmarquee/openmarquee/compare/v0.7.0-rc.1...HEAD
[0.7.0-rc.1]: https://github.com/openmarquee/openmarquee/compare/v0.6.0-beta...v0.7.0-rc.1
[0.6.0-beta]: https://github.com/openmarquee/openmarquee/compare/v0.5.0-beta...v0.6.0-beta
[0.5.0-beta]: https://github.com/openmarquee/openmarquee/releases/tag/v0.5.0-beta
