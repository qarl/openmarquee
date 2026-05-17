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
