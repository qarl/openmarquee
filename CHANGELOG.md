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

## [1.0.1] - 2026-06-DD — DRAFT (not yet tagged)

**DRAFT release notes.** The 1.0.1 tag + RELEASE commit are
qarl's call. These notes cover everything between `v1.0.0`
(`57d95db`, 2026-05-31) and origin/main HEAD `5c5ff39` as of
2026-06-03 (r50 landed during r54 draft; r54 follow-up
commit updates this section to cite it) — 39 commits across
a SUBSTANTIVE feature ship
(text-over-video composite), the operator-visible UI exposure
batch (outline + drop shadow + transition timing + HTTPS toggle),
two workflow-driven adversarial-refute audits (r47 spec-vs-code
+ r49 UI-vs-model), the cumulative renderer allocator defense-
in-depth arc, CMA observability stopgaps, dual-radio topology,
and build/deploy hygiene. **Not a pure maintenance ship — text-
over-video and 4 UI feature exposures are real operator-visible
changes.** Spec coordination is documented per-section below;
SYSTEM_SPEC rewrites pending admin-Jimmy per r47 §F.

### Renderer feat — text-over-video composite (the headline)

End-to-end implementation of SYSTEM_SPEC §5.10's text-over-video
slide type. r47's spec-vs-code audit had flagged the §5.10 claim
as PARTIAL (NV12 dmabuf sampling worked; text-layer compositing
in the video-frame path didn't exist); r46 closed it.

The implementation iterated through several rounds. Documented
honestly because the iteration pattern matters for v1.0.1's
story:

- `f5254af` r46 — initial implementation. Adds
  `TextLayer.background_video_slide_id: Option<Uuid>` in
  `renderer/src/content.rs` (was previously serde-dropped
  silently); adds `paint_and_present_one_text_over_video_slide_frame`
  in `hdmi.rs` (mirrors the image-bg-on-text path with a V4L2
  decode + dmabuf-blit bg step); adds `ensure_bg_video_for_text_slide`
  cache method in `ipc_main.rs`. Per-frame text paint (not the
  dispatch's literal cached-PNG design); rationale in audit
  `qa/r46-text-over-video-impl-2026-06-02.md` §C.
- `b2b225f` r46.1 — `resolve_slide_bg` warn cleanup. Hot-path
  log spam removed.
- `12cafba` r46.2 — text-over-video freeze fix attempt (CMA
  re-prime memoization). Memoized the per-slide V4L2 decoder
  init to avoid re-creation cost on slide repeat. *Side effect:
  broke the scanout chain on the second play of a memoized
  slide.*
- `dd8fa74` r46.3 — text-over-video scanout regression fix
  (`capture_drained` reset). Restored scanout correctness from
  r46.2. *Side effect: broke video sample wrap (clip shorter
  than slot stopped looping).*
- `b3f932d` r46.4 — V4L2 wrap fix via
  `VIDIOC_DECODER_CMD V4L2_DEC_CMD_START`. Targeted the wrap
  symptom by adding a kernel-level decoder restart between
  loops. *Side effect: didn't help — wrong surface.*
- `c2edea1` r48 — V4L2 OUTPUT buffer rotation (the perf-night-
  r5 free-list refactor). **The actual fix.** Pre-r48
  `Decoder::feed()` always used `buf_idx=0` (single OUTPUT
  buffer); on bcm2835-codec, back-to-back feeds raced
  `drain_output_quiet` against the kernel's decode pipeline —
  the kernel still owned slot 0 when the next feed tried to
  QBUF it again, returning `VIDIOC_QBUF OUTPUT: EINVAL`. The
  perf-night r5 comment at `video_decode.rs:170-178`
  (2026-05-26) explicitly anticipated this refactor; r48 ships
  it. After r48, the text-over-video freeze + wrap symptoms
  both resolve at the root cause.
- `5c5ff39` r50 — text-over-video in transitions (closes r46
  §F.new). Wires text-over-video INTO the playlist-level
  transition rendering path so a fade/wipe/etc. into a text-
  over-video slide presents both layers correctly. Landed
  after r54's initial draft; §5.10's transition-into-text-
  over-video case is now structurally complete. **Tag-cut
  blocker reduces to: r50 live-fire verification on FYS** —
  visual confirmation that the transition path composites
  correctly under real load (no shader regressions or scanout
  glitches under transition stress).

The symptom-chase pattern (r46.2 → r46.3 → r46.4 each fixed a
symptom + introduced the next, until r48 found the right
surface) is the v1.0.1 lesson on staying disciplined under
symptom pressure. r48's commit message explicitly cites the
2026-05-26 perf-night r5 comment that anticipated the free-list
refactor — the right fix was already named in a code comment;
the chase happened because the symptoms looked V4L2-spec-flow
shaped, not free-list shaped.

### UI exposure — text effects, transition timing, HTTPS toggle

r49's UI-vs-model audit (see below) surfaced 14 fields where the
Pydantic model + Rust renderer honored an effect but the UI
editor had no surface. v1.0.1 closes 4 of them; the rest deferred
to v1.1 or by-design per r49.

- `ef3bc95` r51 — `TextLayer.outline` (CRITICAL F013 from r49)
  + `TextLayer.drop_shadow` (NEW field) across all 4 surfaces:
  Pydantic model + Rust renderer (MSDF shadow pre-pass at the
  static + dynamic glyph batches) + Canvas2D (native
  shadowOffsetX/Y/Blur/Color API + strokeText) + UI editor (new
  Text effects row in the per-layer accordion). Outline shader
  was previously dead in production because nothing in the UI
  ever set the flag; r51 unblocks it. drop_shadow is a fresh
  bool toggle with baked-in v1.0.1 defaults (small bottom-right
  offset, slight blur on Canvas, ~70% black). Parity gap
  documented in `qa/r51-outline-dropshadow-2026-06-03.md` §F
  (Canvas gaussian blur vs Rust sharp SDF); parity fixture
  staged at `renderer/tests/fixtures/f0000000-0000-4000-8000-00000000005a/`
  pending the FYS-side `scripts/render_tests.sh --bless` golden
  generation as an r51b follow-up.
- `a222973` r52 — `PlaylistItem.transition_ms` operator-editable
  via the Option B click-to-open popover. Pre-r52 the playlist
  track UI hard-coded 500 ms at 3 sites (HIGH F081 from r49).
  Now: clicking the transition chip opens a popover with BOTH
  kind and duration controls. Cut transitions structurally have
  length=0 — both UI (disables ms input on kind=cut, auto-
  restores `lastNonZeroMs` on kind=other, auto-switches to
  `lastNonCutKind` when operator types ms while kind=cut) AND
  backend (`PlaylistItem._clamp_cut_to_zero` model_validator
  clamps rather than rejects so legacy on-disk JSON with
  `{transition:"cut", transition_ms:500}` loads cleanly).
  Migration: v2 storage migration assertion updated 500 → 0 in
  the existing `test_v2_on_disk_migrates_with_default_transitions`
  test.
- `f4d5994` r53 — `SystemSettings.tailscale_https_enabled` UI
  checkbox in the Tailscale fieldset. Closes 9-day-old backlog
  from `project_https_phase_1_shipped_2026_05_24` (HIGH F078
  from r49). The field was wired backend-side
  (`fqdn_redirect_middleware.py:120` + `install.sh`) since
  2026-05-24 but had zero UI surface. r53's hydration uses
  `settings.tailscale_https_enabled !== false` (not `=== true`)
  so a legacy `settings.json` missing the key renders as
  checked, matching the model default (`True`) and the device's
  at-rest HTTPS-on behavior.

### Workflow-driven audit work — r47 + r49

Two adversarial-refute audits drove the dispatch routing for
v1.0.1's later half. Both used the same workflow harness pattern:
enumerate candidates → parallel verifiers → adversarial
refuters with distinct skeptic lenses → majority-refutes (2+ of
3) kills the finding. Workflow scripts live under
`.claude/workflows/` (not committed per the standing rule;
methodology described in each audit doc).

- `c57e109` r47 — SYSTEM_SPEC.md vs code audit. 38 agents /
  1.5M subagent tokens / 16 min. 74 capability claims; 53
  VERIFIED, 12 PARTIAL, 9 NOT_IMPLEMENTED (7 CRITICAL). Zero
  adversarial flips — all 9 NOT_IMPLEMENTED findings survived
  3-lens (alternate field names / indirect implementation /
  wire-format translation) skeptic refutation. Top findings:
  C074 sign-side WiFi welcome screen (never shipped; pending
  qarl decision doc-rewrite vs build-the-impl), C036
  text-over-video (closed by r46+r48 above), C034 TextSlide
  rerender on display-dim change (removed in DELETE-PIL; spec
  pending rewrite), C007 output_mode collapsed to HDMI-only,
  C014 USB-WiFi-dongle udev rule (proposed in r31/r34 audits
  but never landed), C030/C031/C032 Welcome+Freedom seed
  collapsed into FREE YOUR SIGN demo reel. Full table in
  `qa/r47-spec-vs-code-audit-2026-06-02.md`. **§F outer-repo
  recommendations queued for admin-Jimmy** — 7 SYSTEM_SPEC.md
  rewrite candidates documented; F.5 + F.7 require qarl product
  judgment (doc-rewrite vs ship-the-impl).
- `12a986e` r49 — UI vs Pydantic-model audit. 55 agents / 1.88M
  subagent tokens / 12.3 min. 92 operator-facing fields; 74
  VERIFIED (80%), 18 non-VERIFIED. 5 high-impact (1 CRITICAL +
  3 HIGH + 1 MEDIUM); 13 by-design LOW (mostly v3+ playlist-
  owns-transitions legacy fields). Zero adversarial flips
  matching r47's gold-standard rigor. Top-5: F013 outline
  (closed by r51), F008 font_size_px READ_ONLY (likely-
  intentional responsive design), F078 tailscale_https_enabled
  (closed by r53), F081 PlaylistItem.transition_ms (closed by
  r52), F010 TextLayer.weight (deferred v1.1, ~80-100 LOC
  across model+UI+renderer). Full table in
  `qa/r49-ui-vs-model-audit-2026-06-03.md`.

The workflow pattern is now part of the toolkit and will be
reused for any future "enumerate + verify + adversarially refute"
audit.

### Renderer perf — allocator defense-in-depth arc

The CMA-pool "leak" hypothesis from the post-v1.0.0 D.2 / r35
FPS investigation drove a four-round audit + fix arc on the
renderer's GLES / EGL / V4L2 allocator paths. **The HIGH-
priority "scanout BO/FB leak in a SUSPECT path" hypothesis from
r37's audit was REFUTED** by deep-read
(`qa/r38b-hdmi-cma-deep-read-2026-06-02.md`) — all 13
`lock_front_buffer` callsites in `hdmi.rs` already match the
canonical 11-step release contract.

The cumulative arc's allocator-leak hypothesis space is **fully
audited** — zero new SUSPECT or CONFIRMED-LEAK sites remain
across `renderer/src/` per r42's §F. But the FYS leak hypothesis
that DROVE the arc turned out to be wrong: post-r38c QA time-
series traces showed `cma_used` swinging 229-254 MB on per-
minute intervals — a noisy band, not a stable steady state.
The "187 → 255.8 MB over 6h drift" in D.2 was largely variance +
cache fill, not a 70 MB leak. **Honest read: the renderer perf
arc shipped real defense-in-depth for non-FYS deployments
(VideoSlide + DMABUF + VLC stream paths), but the FYS leak
hypothesis was wrong; the symptom was cache + noise.** Future
perf-ceiling audits should follow the standing rule (see
`feedback_perf_audit_enumerate_allocators` memory): inventory
allocator surfaces alongside compute/sync candidates from
round zero, not after a multi-round chase.

- `5ac3ca2` r38b — transition-closure scanout cleanup. 3
  `?`-bubble sites in `paint_and_present_one_transition_frame`
  would leak ~16 MB of bake-target GLES storage on
  `ensure_scene_fbo` / `get_attrib_location` failure.
- `f14c3b1` r40 — 3 non-FYS allocator fixes:
  `bake_external_nv12_to_current_fbo` y_tex orphan,
  `run_nv12_dmabuf_blit_pass` EGLImage + dma_buf ref leak,
  `bake_video_slide_to_current_fbo` MMAP y_tex orphan.
- `30039bd` r41 — `capture_fullres_transition_mid_to_png`
  cap_tex create-fail leaked fbo_a/tex_a/fbo_b/tex_b (~16 MB)
  + `sdf_atlas_gl.rs:upload_all` `cleanup_partial` closure.
- `14adb16` r42 — V4L2 EXPBUF fd-leak. `allocate_buffers`
  EXPBUF loop leaked any fds already pushed to local
  `fds: Vec<RawFd>` on mid-loop failure;
  `cleanup_partial_fds` closure with `libc::close`.

### CMA observability + stopgaps

- `2369815` r38c — CMA-pressure watchdog. systemd timer +
  oneshot service polls `/proc/meminfo` every 60s; if
  `CmaUsed >= 220 MB` AND outside the 30-min cooldown, runs
  `systemctl restart --no-block openmarquee-backend.service`.
  Paired with a daily 03:00 cron restart as the hard floor.
  **Threshold mistuned** — see Known Issues below.
- `84629f0` cron enable-guard follow-up — defense for the
  daily-restart drop-in. `install.sh §3d` checks
  `systemctl is-enabled cron.service` and enables if disabled.
- `95b150a` r38d — SIGUSR1 cache-dump handler. Operators can
  `sudo pkill -USR1 -f openmarquee-render` to dump
  `image_bg_cache.len()`, `image_slide_tex_cache.len()`, and
  CMA/vm_rss snapshot to journald in a single TAB-separated
  `[cache-dump]` line. Confirms whether observed CMA growth is
  cache fill (expected; capped) or pipeline churn.

### Topology — dual-radio dongle + Option B SHIP verdict

- `b5b9919` r34 — dual-radio USB-WiFi-dongle shipping topology
  (feat). Adds the `wlan-dongle` management-WiFi role: any
  rt2x00usb-family USB dongle, udev-renamed via
  `system/99-openmarquee-usb-wlan.rules`, with a NetworkManager
  keyfile pinned `interface-name=wlan-dongle` and
  `route-metric=50`. (r47's C014 NOT_IMPLEMENTED finding was a
  code2-vs-main worktree divergence — the rule landed on main
  with r34's `b5b9919` but had not yet propagated to the code2
  worktree at the time r47's audit ran; on main HEAD the file
  is present and the C014 gap is closed.)
- `2486073` r43 — brcmfmac AP-only audit. Static + community-
  evidence research audit closing code2 r33's "critical
  premise A.6" (does hostapd directly on `wlan0` AP-only work
  reliably on BCM43438?). Verdict: **SHIP Option B** with
  conditions. **Implementation pending qarl's ship-now/hold
  call** — not in v1.0.1.
- `4a0ba6d` r44 — `wifi_station.py` post-dongle/nmcli comment
  sweep.

### Build / deploy hygiene

- `3de2a3f` — `/healthz` probe budget bumped 30s → 75s.
- `5d2a9e9` r29 — `install.sh` reorder. Sections §3 + §3a +
  §3b run BEFORE §2. Renderer binary install also atomic now
  (cp to `.new` then rename).
- `687485d` r31 — `deploy.sh` refreshes aarch64 wheels per
  deploy + adds FFmpeg dev headers (Candidate A+B per the r30
  install.sh pip-failure audit).
- `621b4f3` r32 — `deploy.sh` ensures emoji fonts survive
  `rsync --delete`.
- `3cee501` r33 — manifest-based regression test for `deploy.sh`
  runtime assets.
- `ce47ffe` r37a — `scripts/build_sd_bundle.sh` hygiene:
  emoji-font pre-fetch, manifest preflight, SHA256 sidecar,
  fail-loud preflight asserts.
- **Operator deploy pattern (Path D)** — emerged from tonight's
  reboot cycle pain. Standard sequence:
  1. `systemctl stop openmarquee-wifi-watchdog` (so it doesn't
     reboot the box mid-deploy)
  2. `systemctl stop openmarquee-backend` (releases mmap'd
     `/usr/local/bin/openmarquee-render`)
  3. `rsync` the new binary unthrottled (no ETXTBSY)
  4. `mv -f` atomic rename (same-filesystem dirent swap)
  5. `systemctl start openmarquee-backend` (picks up new
     binary)
  6. `systemctl start openmarquee-wifi-watchdog`
  Path D is the new standard for any renderer-binary deploy
  outside of the standard `scripts/deploy.sh` flow.

### Doc hygiene

- `d06c506` r32 + `0584f14` r33 — BT.709 colorspace doc-comment
  ride-alongs (code2). Pure renderer-comment updates in
  `v4l2.rs:colorspace_block` and `hdmi.rs:3520`.
- `af6001e` r39 — `VideoSlide.duration_ms` docstring rewrite.
  The pre-r39 "duration_ms is informational; the playback
  engine reads the actual runtime from the file" claim was
  WRONG.
- `4a0ba6d` r44 — see Topology above.
- 11+ QA recommendation + audit docs shipped under `qa/` over
  the v1.0.1 window: r30 install pip, r31 dongle topology, r33
  Option B captive portal, r34 outer-repo edits, r35 FPS
  ceiling, r36 SD bundle, r37 CMA allocator, r38b CMA deep-
  read, r38c CMA watchdog, r38d SIGUSR1, r39 video duration,
  r40-r42 allocator fixes, r43 brcmfmac AP-only, r44 wifi
  comments, r46+r48 text-over-video, r47 spec-vs-code, r49
  UI-vs-model, r51 outline+drop_shadow, r52 transition_ms
  popover, r53 HTTPS toggle, plus this r54 release-notes
  refresh.

### Notable operator-visible behavior changes

1. **Text over video** (r46+r48). Operators can now select a
   VideoSlide as the background for a TextSlide via the existing
   editor bg-source picker; the renderer composites text on the
   live-decoded video frames at 30 fps. Per the §5.10 contract.
2. **TextLayer outline + drop shadow** (r51). New "Text effects"
   row in the per-layer accordion editor. Both defaults off;
   operators opt in. Outline color hardcoded black,
   width ~5% of font height; drop shadow offset 0.04 em, blur
   0.06 em on Canvas / sharp on Rust, ~70% black.
3. **Playlist transition duration knob** (r52). Clicking a
   playlist track block's transition chip opens a popover with
   BOTH kind and duration. Cut transitions structurally have
   length=0 (UI + backend both enforce). Pre-r52 every entry
   was hard-coded to 500 ms.
4. **Tailscale HTTPS toggle** (r53). New checkbox in Settings
   → Tailscale section. Default checked (matches model + at-
   rest device behavior); operator can disable to serve plain
   HTTP on port 80.
5. **CMA-pressure watchdog** (r38c). New systemd timer
   service. Default threshold: 220 MB. Restarts
   `openmarquee-backend.service` on threshold trip. Daily
   03:00 cron restart as the hard floor. **Threshold mistuned
   — see Known Issues.**
6. **SIGUSR1 cache-dump** (r38d). Renderer-side forensic
   surface (`sudo pkill -USR1 -f openmarquee-render`).
7. **Dual-radio USB-WiFi dongle SUPPORT** (r34). Additive —
   no behavior change for installs without a dongle.
8. **install.sh + deploy.sh resilience** — Pip-failure no
   longer blocks renderer binary install; aarch64 wheels
   refresh per deploy; emoji fonts protected against
   `rsync --delete` propagation; SD bundle build has fail-loud
   preflight asserts; Path D pattern for renderer-binary-only
   redeploy.

### Known issues

- **CMA watchdog threshold of 220 MB is too aggressive — bump
  to 254-260 MB before v1.0.1 tag.** Per QA's post-r38c FYS
  time-series trace, `cma_used` swings **229-254 MB on per-
  minute intervals** — a ~25 MB wide noisy band. The current
  220 MB default trips well inside that band, GUARANTEEING
  spurious restart loops even with no true leak. Operators
  experiencing unwanted restarts should:
  ```
  sudo mkdir -p /etc/default
  echo 'THRESHOLD_MB=254' | sudo tee /etc/default/openmarquee-cma-watchdog
  sudo systemctl restart openmarquee-cma-watchdog.timer
  ```
  **Ship decision pending qarl**: bump the default in
  `system/openmarquee-cma-watchdog.sh` to ~254 MB OR implement
  the smarter "sustained N polls above threshold" detector
  before tag-cut. r38d's cache-dump data would distinguish
  cache-driven swings from pipeline churn; full data collection
  in flight from QA SSH lane.
- **r47 NOT_IMPLEMENTED Cluster A — 5 SYSTEM_SPEC.md rewrites
  pending admin-Jimmy.** C007 output_mode (HDMI-only, not 4-
  way), C010 SSID derivation (random alphanumeric, not MAC),
  C030/C031/C032 Welcome+Freedom seed (collapsed into FREE
  YOUR SIGN demo reel), C034 TextSlide rerender (removed in
  DELETE-PIL phase 3), C046 CBDT emoji bake (replaced by COLRv1
  vector raster). Doc-rewrite class; no code change required.
- **r47 NOT_IMPLEMENTED Cluster B — pending implementation.**
  C074 sign-side WiFi welcome screen with SSID/password/QR
  (~180 LOC if shipped, OR a doc-rewrite if the captive-portal-
  only welcome flow is the intended design — **qarl decision
  pending**). (Note: C014 USB-WiFi-dongle udev rule, also
  originally in Cluster B, was already shipped on main as
  `system/99-openmarquee-usb-wlan.rules` per r34's commit
  `b5b9919`; r47's NOT_IMPLEMENTED verdict was against the
  code2 worktree which had not propagated the rule yet.)
- **r49 deferred UI exposure findings** — F010 `TextLayer.weight`
  (variable-font Light/Bold per-layer override, ~80-100 LOC
  across model + UI + renderer; v1.1) and F008
  `TextLayer.font_size_px` (READ_ONLY pixel-precise sizing;
  likely-intentional responsive design but confirm with qarl).
  Other 13 LOW findings are by-design v3+ playlist-owns-
  transitions legacy fields + future-reserved (Schedule.tz,
  TextLayer.anchor/locked) — candidates for MODEL CLEANUP
  in a v1.1 dispatch.
- **r51 parity gap** — Canvas `shadowBlur` is gaussian; Rust
  drop-shadow pass is sharp SDF (no blur). Visible difference:
  Canvas side has soft fading shadow edges; Rust side has hard
  glyph-silhouette shadow. Threshold relaxed to ssim≥0.85 +
  mean_delta≤24 in the staged parity fixture. Mitigation
  (multi-sample SDF blur OR offscreen FBO + gaussian) deferred
  to r51b.
- **r51 WASM-squish path** — WASM-rasterized text in the
  yScale!=1 squish path doesn't apply outline + drop_shadow
  (the helper isn't called from the WASM `drawImage` branch).
  Uncommon path (only triggers when text overflows the box
  vertically AND fontdue WASM is available). r51b doc note.
- **Option B captive-portal topology approved but NOT yet
  shipped.** r43's SHIP verdict (retire ap0; hostapd-on-wlan0-
  AP-only) is approved with conditions but the implementation
  dispatch is pending qarl's ship-now/hold call. v1.0.1
  continues on Option A.
- **CVE-2025-40321 / iOS 18.6+ ANQP NULL-deref crash class.**
  Real risk for any AP-mode deployment on brcmfmac; the
  upstream fix (`3776c685ebe5`) is already in rpi-6.12.y as
  `3f8ad41f42b6` per r43 §A.1. Bookworm-base SD images
  shipping the current rpi kernel pick this up automatically.
- **Home-router AP rejecting wlan-dongle auth → wifi-watchdog
  reboots Pi every ~10 min.** Deployment-environment issue
  surfaced during tonight's Path D deploy cycle. Operators on
  a dongle topology should verify their home AP isn't filtering
  MAC + that the WPA2-PSK is correct in `/etc/NetworkManager/
  system-connections/wlan-dongle.nmconnection`. wifi-watchdog
  treats sustained outbound connectivity loss as
  "rebooting-might-help"; on a misconfigured AP this becomes a
  reboot loop. Workaround: `systemctl disable
  openmarquee-wifi-watchdog` if your network's AP needs
  troubleshooting and you don't want the box rebooting under
  you.

### Spec / outer-repo coordination

The v1.0.1 ship moves SYSTEM_SPEC.md from "matches code" (the
v1.0.0 baseline) to "needs catch-up rewrites in §F-listed
sections" per r47's audit. The §5.10 text-over-video claim is
NOW actually shipped (r46+r48); §5.10a text effects partial
(outline + drop_shadow shipped, motion+blend still future);
other §F items remain pending admin-Jimmy.

Admin-Jimmy spec-rewrite work queue:

- **r47 §F.1** §3.4 line 156 `output_mode` reality (collapsed
  to HDMI-only)
- **r47 §F.2** §3.4 line 164 SSID derivation (random
  alphanumeric, not MAC-derived)
- **r47 §F.3** §4.1.1 line 191 USB-WiFi-dongle udev rule —
  CLOSED on main per r34 `b5b9919`; spec text may still need a
  pass to verify the wording matches the shipped rule
  (`system/99-openmarquee-usb-wlan.rules`)
- **r47 §F.4** §5.7 lines 294-296 FREE YOUR SIGN demo reel
  (replaces Welcome+Freedom)
- **r47 §F.5** §5.8 line 312 TextSlide rerender on display-
  dim change — qarl decision: doc-rewrite vs ship-the-fix
- **r47 §F.6** §5.10a line 352 COLRv1 vector (replaces CBDT
  bake)
- **r47 §F.7** §8 line 565 sign-side welcome screen — qarl
  decision: doc-rewrite vs ship-the-impl
- **r49 §F.1** §5.10a text effects updated to reflect outline
  + drop_shadow shipped per r51 (the §F.1 from r49 was a
  "spec may over-promise" flag; r51 closed the gap so the
  spec text is now consistent with reality)
- **r49 §F.3** §5.4 / §7.1 transition_ms doc text (verify
  consistency with r52's operator-editable shape)

### Tag posture

When qarl directs the v1.0.1 tag cut:

1. **r50 verification on FYS** — `5c5ff39` landed; live-fire
   confirm transitions into text-over-video slides composite
   correctly under load. With r50 landed, §5.10's transition-
   into-text-over-video case is structurally complete; the
   remaining gate is visual / behavioral verification on the
   real hardware, not additional code.
2. **CMA watchdog default decision** — either:
   (a) bump `THRESHOLD_MB` in
       `system/openmarquee-cma-watchdog.sh` to ~254 MB, or
   (b) ship the sustained-N-polls detector instead (~15 LOC +
       state file extension), or
   (c) accept the 220 MB default + document the override
       workaround as the operator's responsibility.
3. Update `[1.0.1] - 2026-06-DD — DRAFT (not yet tagged)`
   above to the actual cut date + drop the DRAFT marker.
4. Bump version strings in `backend/openmarquee/__init__.py`,
   `backend/pyproject.toml`, `renderer/Cargo.toml`, and
   `ui/package.json` (all currently at 1.0.0). All four
   components share the single version string per the file
   header at top of this changelog.
5. RELEASE commit + `git tag v1.0.1` + push tag.
6. FYS deploy via the standard
   `bash scripts/deploy.sh openmarquee@fireplacesign` flow,
   OR the Path D sequence for renderer-binary-only redeploy.

## [1.0.0] - 2026-05-31

Spec-complete v1.0 ship. Closes the v0.9.0 → v1.0 arc: text-layer
chrome triad (`c38e64d`), 4 audit retractions (`0c4039c` + `930954a`
+ `c1a5e0a` + r26 doc closure `51e719f`), pre-push hook chmod
hygiene (`8d07409` + `2605a53`), and the r25 glyph rasterization
prewarm with drain-to-zero gate (`ab047a5` + capture `8209f41`).

### Renderer perf — r25 (paint_bake_text MAX −56%)

`ab047a5` — Glyph rasterization prewarm at sidecar startup with
drain-to-zero gate: 855 ASCII glyphs (printable U+0020..U+007E)
across 9 fonts (anton, alfa-slab-one, bowlby-one-sc,
playfair-display, vt323, permanent-marker, caveat-brush,
jetbrains-mono, dejavu-sans-fallback). Playback loop blocks
until the worker queue fully drains — prevents the
`slide_caches` invalidation cascade that regressed r20's first
ship. Sidecar boot wall-time grows ~33s on Pi Zero 2 W
(~26 g/s msdfgen rate); watchdog 120s still has 3-4x margin.
Captures at `qa/captures/r25-glyph-prewarm-{baseline,after}-2026-05-31.json`.

### Text-layer chrome (3 P1 closures via r26 `c38e64d`)

- `anchor` (vertical alignment top/center/bottom) honored at
  paint via new `parse_v_align` in `hdmi_logic.rs`. Operator
  choice no longer silently dropped to center.
- `visible` honored at SAVE time (was already honored at playback)
  — drawTextOnly inline-preview path guards on `layer.visible`.
- `weight` wire-accepted, render-deferred to v1.x: fontdue
  doesn't support variable-axis selection + bundled font system
  is single-TTF-per-family. Field survives save/load round-trip.

### Spec-delta audit retractions

The v1 spec-delta audit doc (`qa/v1-spec-delta-2026-05-30.md`)
carries 4 errata blocks documenting audit errors caught + fixed:

- fontdue weight (r26): "variable-axis supported" claim was wrong
- P2 #4 / §5 row 4 / §2 row 7 (r22 + r24): "validator split
  needed" + stale line citations — the split was already done,
  only comment hygiene remained
- §6 FqdnRedirectMiddleware (r28): "should not exist" claim was
  wrong — the middleware exists by design for Chrome
  secure-context (`navigator.mediaDevices` HTTPS gating)

P2 #6 `locked` server-enforcement closed by-design per
SYSTEM_SPEC line 320 (editor-UX gating is the spec contract).

### Hygiene

- `8d07409` + `2605a53` — `.githooks/pre-push` + 9 other
  `scripts/*.sh` files tracked as 100755 (was 100644; fresh
  clones silently bypassed the pre-push validation gate).
- `5940046` — outer-repo recommended-edits doc shipped for admin
  Jimmy `openmarquee` to apply mechanically.
- deploy.sh `/healthz` probe budget bumped 30s → 75s to
  accommodate the ~46s post-r25 sidecar boot.

### Notes

HUB75 / WS2812B / composite panel modes remain deferred to v1.x
(Python drivers removed in v0.6 DELETE-PIL; Rust ports pending).
Stream Mode B network-source controls + originals/deployed
storage split also deferred. FYS production is the first device
to receive v1.0.

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
