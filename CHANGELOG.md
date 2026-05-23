# Changelog

All notable changes to openMarquee land here. Format follows
[Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer 2.0](https://semver.org/spec/v2.0.0.html)
with pre-release identifiers.

Components share a single version string across the backend
(`backend/openmarquee/__init__.py` + `backend/pyproject.toml`),
renderer (`renderer/Cargo.toml`), and UI (`ui/package.json`).

PEP 440 caveat: pip normalizes `0.5.0-beta` to `0.5.0b0` at install
time with a deprecation-warning print. Functional behavior is
unchanged; the literal version string is preserved across all four
component locations for cross-ecosystem readability.

## [Unreleased]

(empty — next changes land here.)

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
[Unreleased]: https://github.com/openmarquee/openmarquee/compare/v0.5.0-beta...HEAD
[0.5.0-beta]: https://github.com/openmarquee/openmarquee/releases/tag/v0.5.0-beta
