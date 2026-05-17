# Phase E recon: release prep

Date: 2026-05-17
Author: Jimmy-openmarquee-code (recon only — no implementation in this
commit)
Dispatch: QA, "Phase E recon — release prep" (post-Bug-6 close, ship
plan A-E ready to land)
Prior recon precedent: `qa/captures/phase-d-strict-30fps-recon-
2026-05-17.md` (commit 2f2924a).

Scope: map what "release prep" means in the openMarquee context and
what gaps exist between HEAD and a first tagged GitHub release. Folds
in a cumulative-coverage audit per
`feedback_qa_audit_cumulative_coverage` (Phase D completion = feature-
complete moment that triggers a coverage cross-reference). Recon
only — no slice 1 work. Subagent reviewed.

---

## 1. What does "Phase E (release prep)" actually mean?

Two authoritative sources, mostly aligned:

### 1.1 QA-Jimmy session log (canonical operational definition)

`/Users/qarl/project/openmarquee/qa/qa-log-2026-05-08.md:2955`:

> **Phase E**: Release prep (§8.2 6h soak, Pi golden re-bless,
> deploy-time-verification checklist, docs polish)

Four-item bundle. The label `Phase E` and the four bullets are the
operational checklist QA has been tracking against. This recon treats
those four bullets as the load-bearing definition.

### 1.2 IMPLEMENTATION_PLAN.md Phase 11 (canonical plan-doc definition)

`/Users/qarl/project/openmarquee/IMPLEMENTATION_PLAN.md:386-390`:

> ### Phase 11 — MVP polish and first release
> - Hardware compatibility matrix.
> - Docs for end-users.
> - First tagged GitHub release.

Three-item bundle. "Hardware compatibility matrix" overlaps with
Phase F (per `qa/qa-log-2026-05-08.md:2956` Phase F is "Pi 5 / Pi 4
cross-hardware validation"); the rest maps to QA's "docs polish" +
"first tag."

### 1.3 Combined Phase E scope (this recon's working list)

Merging QA log + IMPLEMENTATION_PLAN Phase 11, dropping duplicates and
items now claimed by Phase F:

1. **§11 / §8.2 soak run** — 6h+ on the canonical playlist; gated to
   release-candidate per `feedback_no_soak_during_dev`.
2. **Pi golden re-bless** — re-capture `renderer/tests/golden/*` on
   the production Pi to lock the on-hardware pixel contract.
3. **Deploy-time-verification checklist** — operational doc the
   operator (or QA) runs post-deploy to confirm a sign is ready.
4. **Docs polish** — README, hardware/operator quickstart, end-user
   guide.
5. **First tagged GitHub release** — version-bump, changelog, tag.

Hardware compat matrix (Pi 4/5) is **Phase F** per the QA log. Not in
this Phase E scope.

### 1.4 Spec-level acceptance test (verbatim)

`docs/renderer-rewrite-requirements.md:321-326`:

> The Rust renderer is "done" for v1 when the FREE YOUR SIGN reel
> runs at 30 fps with shader transitions enabled and no OOM kills
> across an extended soak (see §8.2). Until then, the existing
> Python renderer (currently under `backend/openmarquee/rendering/`)
> stays in the tree as the live path; once Rust hits acceptance,
> Python is retired.

`docs/renderer-rewrite-requirements.md:213-217` (§8.2 no-leak):

> Across an extended soak on the canonical playlist (duration to be
> set by the implementing agent at a length that would surface a
> real leak — start point: ≥6 hours, ideally overnight), `VmData`,
> `VmRSS`, `Swap`, and `CmaUsed` must show no monotonic growth.

These two clauses are the spec-level ship gate. Everything else in
Phase E is operational hygiene around that gate.

---

## 2. As-built audit per Phase E item

### 2.1 §11 / §8.2 soak — instrumentation READY, run PENDING

Phase 9 Step 9a (commit ffbb437) shipped `IpcPaintMetrics` emitting
`ipc.soak` every 30s. Phase 9 Step 9b (commit efdefed) shipped
`scripts/renderer_pi_soak_ipc.sh` + `renderer_pi_soak_ipc_parse.py`
gating on rolling-10min `fps_avg ≥ 30.0` + OOM/crash. Phase D
(commits 8a2e043 + f03ee91) added `paint_us_p99 ≤ 33333us` to both
emit + gate.

Memory slope gate is the parallel `scripts/renderer_soak_parse.py`
(verified at `scripts/renderer_pi_soak_ipc_parse.py:30-31` — "the
other gates mem slope").

**Gap to ship:** the actual 6h run, gated by qarl-dispatch per
`feedback_no_soak_during_dev`. Recommended: explicitly time-bound the
ship gate (e.g., 6h overnight on dev Pi at 192.168.50.211 →
parser PASS → declare).

**Ship-blocker priority:** HIGH. This is the spec-literal acceptance
gate.

### 2.2 Pi golden re-bless — 90 fixtures present, status unclear

90 files in `renderer/tests/golden/` (verified via `ls | wc -l`).
The atlas-sb-2026-05-09 stash from the editor.test.js close-out
investigation (`renderer-goldens-git_sha-drift-pre-merge`) suggests
goldens have been touched by branch-merge work but not yet locked to
Pi-captured pixels.

The most recent renderer commit (`fb3f6a3 renderer: re-bless goldens
after layout fix (c56314f)`) was a re-bless, so at least the layout-
fix-affected goldens are current. But these were re-blessed on the
dev environment, not on the production Pi.

**Gap to ship:** capture goldens ON the production Pi (192.168.50.211
or fys Pi 192.168.1.67) via `scripts/render_tests.sh
openmarquee@openMarqueeDev`, diff against current goldens, decide
which need re-blessing (parity floor + scanout-buffer-specific
differences).

**Ship-blocker priority:** MEDIUM. Parity tests run against goldens;
if they drift on-Pi from dev-Mac captures, dev regression tests stop
catching real-Pi regressions. Not blocking ship — would block future
regression detection.

### 2.3 Deploy-time-verification checklist — does NOT exist

Grep for `deploy-time-verification`, `deploy-checklist`,
`smoke-test`, `post-deploy` across `docs/`, `qa/captures/`, `README*`
turns up nothing operational. Closest: `scripts/deploy.sh` itself
(just rsync + systemctl restart, no verification step).

**Gap to ship:** new doc. Recommended location:
`docs/deploy-checklist.md`. Content per QA-relayed-from-deploy-green
shape:
  - `systemctl is-active openmarquee-backend`
  - `curl /api/auth/status` → `{"configured":true}` if first-run done
  - `curl /api/content` (no auth) → 401 not 503
  - `curl /api/playback/current-thumbnail` → 200
  - `curl /api/playback/current-frame` → 200
  - `journalctl ipc.soak | head -3` → confirms IPC sidecar emission
  - `curl /welcome.html | grep welcome-continue` → if first-run
  - HDMI eyeball verify (no checklist substitute; visual confirmation)

**Ship-blocker priority:** LOW. Useful operational hygiene; absence
doesn't block tag. Could land alongside Phase E or as Phase F-prep.

### 2.4 Docs polish — README is CONTRIBUTOR-focused, no end-user doc

`/Users/qarl/project/openmarquee/code/README.md` is the top-level
README. Status section line 26 says:

> **Early.** No releases yet. Architecture is still settling and
> there's no working firmware image. See `docs/` for contributor-
> facing docs.

That statement was true when written; per
`project_phase_b_sd_card_automation` (2026-05-11) the flashable image
landed, and per the recent ship-plan A-D it's release-ready. README
needs a refresh to reflect current state.

`docs/README.md` is contributor-only ("Contributor-facing
documentation"). No user-facing "how to install on a fresh Pi"
quickstart.

**Gap to ship:**
- README.md: drop the "Early" warning, replace with quickstart
  (download image → flash → boot → captive portal).
- New `docs/quickstart.md` or `docs/install.md` for end users.
- `docs/factory-fresh.md` already exists — verify it covers the
  flashable-image happy path or merge with new quickstart.

**Ship-blocker priority:** HIGH. README is the project's front door
on GitHub.

### 2.5 First tagged GitHub release — no version, no changelog, no tag

`git tag` shows ONE tag: `pre-rust-phase-scripts-2026-05-10` —
a checkpoint, not a release.

Version state across components (all placeholder):
- `backend/pyproject.toml:7`: `version = "0.0.0"`
- `renderer/Cargo.toml:3`: `version = "0.1.0"`
- `ui/package.json`: `"version": "0.0.0"` (verified)

No `CHANGELOG.md` anywhere in the repo.

**Gap to ship:**
- Pick a version. Options: `0.1.0` (matches renderer), `0.5.0-beta`
  (signals pre-1.0), `1.0.0` (full release), `2026.05.18`
  (CalVer). See §5 Q1.
- Bump all three component versions to the chosen number.
- Write CHANGELOG.md. Recent commit history is good source material
  (Phase D, Bug 1-6, Phase 8/9, Phase B, etc.).
- `git tag -a v0.X.Y` with annotated message.
- GitHub release UI (or `gh release create`) with release notes
  + flashable image artifact attached.

**Ship-blocker priority:** HIGH. By definition, "first tagged
release" requires a tag.

---

## 3. Cumulative coverage audit
(per `feedback_qa_audit_cumulative_coverage`)

Cross-reference: canonical specs (`SYSTEM_SPEC.md`,
`docs/renderer-rewrite-requirements.md`) vs actually-consumed surface
at HEAD (447f049).

### 3.1 (a) Spec surface NOT yet implemented

Identified by grep across SYSTEM_SPEC.md major sections:

- **§5.6 AI-generated backgrounds** — partially implemented. Backend
  surface exists (`backend/openmarquee/seed_assets/backgrounds/`
  + CivitAI generator at `www/scripts/civitai-bg-gen.py`). v1
  acceptance: per spec, generation-on-demand from UI. Current state:
  pre-generated only. Priority: NICE-TO-HAVE for v0.X; not a ship-
  blocker.

- **§7.3 WS2812B Playback** — `backend/openmarquee/rendering/
  ws2812b.py` (211 lines) exists as scaffolding. Same shape as
  hub75.py: hardware-wire path stubbed per spec §11. Priority:
  NICE-TO-HAVE (HDMI is the primary target per
  `project_hdmi_1080p_is_primary_target`); stubs acceptable for v0.X.

- **§7.2 HUB75 Playback** — `backend/openmarquee/rendering/hub75.py`
  (288 lines) exists as scaffolding referencing hzeller's
  `rpi_ws281x`/RGB-matrix bindings. Hardware-wire path stubbed per
  spec §11 ("panel-write paths stay stubs until their respective
  phases"). Priority: NICE-TO-HAVE.

- **§7.4 Composite playback** — composite (NTSC/PAL) output not
  implemented. Per spec §10, the device targets HDMI + LED matrix;
  composite is mode #4 of 5. Priority: NO-ACTION for v0.X (composite
  is post-v1 per project memory pattern).

- **§5.10a v3 editor restructure** — `spec L326` notes "Editor
  restructure ships in v3 phase 3 — until then the editor surfaces
  only `text_layers[0]`." Current `ui/src/editor.js` IS v3+ (per
  `project_editor_layout_phone_width_on_desktop` 2026-05-12 +
  multi-layer support added per `feedback_handoff_narrowing_
  migrations`). So this is DONE; spec text drifted. Priority:
  spec-doc update, not code work.

### 3.2 (b) Code without spec coverage

- **`scripts/demo/` infrastructure** — entire demo pipeline (refresh,
  build, generate-seed, check-mock-drift) is operational tooling not
  referenced in SYSTEM_SPEC or IMPLEMENTATION_PLAN. Tracked in
  `reference_demo_system` memory + `www/README.md`. Priority:
  NO-ACTION; legitimate scope outside spec.

- **Phase B flock cross-device sync** — heavy implementation per
  `docs/phase-b-flock-scope.md` shipped. SYSTEM_SPEC §13 references
  the canonical scope. Aligned, but the breadth of `flock.py` +
  `ui/src/flock.js` exceeds the spec's per-clause coverage; the
  design doc is the spec-of-record for this subsystem. Priority:
  NO-ACTION; spec doc IS the contract.

- **§11 Phase 9a IPC instrumentation + Phase D p99 gate** — built
  surface that the spec mandates ACCEPTANCE on, but the
  instrumentation itself isn't spec-mandated (the implementer was
  free to pick a measurement form). Priority: NO-ACTION; legitimately
  inferred surface.

- **`renderer/src/profile.rs`** — `--profile-frames` CLI flag and
  histogram surface. Diagnostic surface not referenced in spec.
  Priority: NO-ACTION; dev-time tooling.

### 3.3 (c) Code without test coverage

Audited via `grep -L 'test\|_test\|cfg(test)' ...` on production
files cross-referenced with vitest/pytest/cargo test coverage:

- **`backend/openmarquee/rendering/snapshot.py`** — pytest coverage
  is non-zero: `backend/tests/test_rendering_snapshot.py` exists
  (~119 LOC, covers `SlideSnapshotCache`). Verified via `ls
  backend/tests/test_rendering_snapshot.py`. Production path: live-
  preview snapshots for editor parity. Priority: NO-ACTION — has
  coverage; will be deleted post-Rust-acceptance anyway.

- **`backend/openmarquee/rendering/gpu_compositor.py`** — has pytest
  coverage at `backend/tests/test_gpu_compositor.py`. Will be
  DELETED post-Rust-acceptance per the renderer-rewrite plan, so
  any remaining test debt is short-lived. Priority: NO-ACTION.

- **`backend/openmarquee/rendering/shader_compositor.py`** — same
  shape; will be deleted. Per `project_renderer_rewrite_rust` (the
  ongoing DELETE-PIL effort visible in recent commits 868a493 / 34ef94c
  / 2686d29) the Python rendering tree is on its way out. Priority:
  NO-ACTION; deleting > testing.

- **`ui/src/welcome.html`** — recently added `.welcome-continue` CTA
  (commit 3226e7a) has unit test coverage but not playwright e2e.
  Priority: LOW; covered by manual smoke + QA-on-glass.

### 3.4 (d) Audit doc drift (Phase 4w pattern)

Spot-checked the 5 most-recent audit docs in `qa/captures/` by
mtime (excluding this recon):

| Doc | Date | Status |
|---|---|---|
| `phase-d-strict-30fps-recon-2026-05-17.md` | 2026-05-17 | Mine, current |
| `motion-through-transitions-audit-2026-05-16.md` | 2026-05-16 | Has Phase 4w correction note (commit 831f471) — accurate |
| `phase8-slice0-non-text-transition-recon-2026-05-15.md` | 2026-05-15 | Phase 8 slice 0 recon; subsequent slices 1-6 shipped per commit log b0c55ea/627f96e/4dcc7b2/e285e81/1c61747 — recon scope honored |
| `parity-phase3aj-python-motion-spec-2026-05-15.md` | 2026-05-15 | Spot-check: `motion.py:220/266/290` citations look valid; status="SHIPPED" matches recent activity |
| `parity-phase3ai-hybrid-2026-05-15.md` | 2026-05-15 | Not deep-audited; smell-test clean |

No Phase-4w-shaped drift detected on smell-test. The audit-doc
correction-note pattern (per the 831f471 commit precedent) is in
place and known. Deeper validation would require per-doc citation
check — out of scope for this recon's 30-min budget.

**No STOP-ping qa-Jimmy** per the dispatch's §2(d) escape hatch.

---

## 4. Phase E slice plan

Five proposed slices. Order matters: docs/version-bump (slices 2-3)
can land in parallel with the soak gate (slice 1). Slice 5 is the
release moment.

### Slice 1 — §11 soak run + parser verdict (~0 LOC, OPERATIONAL)

The instrumentation + harness + gate are all shipped. Slice 1 is the
actual run, dispatched by QA (qarl confirmation required per
`feedback_no_soak_during_dev`):

  - Deploy current HEAD to dev Pi (`192.168.50.211`) or fys Pi
    (`192.168.1.67`).
  - `bash scripts/renderer_pi_soak_ipc.sh --target ... --duration 6h`
    overnight.
  - `bash scripts/renderer_pi_soak.sh ... --duration 6h` for the
    mem-slope companion.
  - Parser PASS → commit a `qa/captures/phase-e-soak-pass-
    2026-05-XX.md` summary attesting acceptance.
  - Parser FAIL → triage; slice 1 grows scope based on findings.

Estimated LOC: 0 (operational). Estimated wall-clock: 6h soak + ~30
min triage/write-up.

### Slice 2 — Pi golden re-bless (~0 production LOC, fixture refresh)

On-Pi capture goldens via `scripts/render_tests.sh
openmarquee@openMarqueeDev`. Diff against current
`renderer/tests/golden/` files. For each mismatch:
- Investigate cause (Pi-specific scanout buffer? bilinear vs
  GL_LINEAR? alignment?)
- Re-bless if drift is hardware-canonical (Pi pixel == ship truth).
- Investigate if drift is a regression (e.g. parity floor not yet
  closed).

LOC: 0 production code; possibly ~90 fixture file updates. Wall-clock
~1-2h depending on triage.

### Slice 3 — Version bump + CHANGELOG (~5 LOC + ~150 LOC docs)

- Pick the version per Q1 below. Bump in three locations.
- Write `CHANGELOG.md` at repo root. Source: `git log --oneline
  ed43a63..HEAD` and prior commits back to a sensible start point.
  Group by phase (Phase 4w / Phase 8 slices / Phase 9 / Phase D /
  Bug 1-6 / DELETE-PIL).
- Single commit.

LOC: ~5 version edits + ~150 LOC CHANGELOG.

### Slice 4 — Docs polish + deploy-checklist (~200-500 LOC docs)

- Rewrite top-level `README.md` quickstart section. Drop "Early. No
  releases yet." Replace with image-download / flash / boot /
  captive-portal walkthrough.
- New `docs/quickstart.md` for end users (mirrors README quickstart
  with more detail).
- New `docs/deploy-checklist.md` per §2.3 of this recon (operational
  smoke tests post-deploy).
- Verify `docs/factory-fresh.md` matches the current SD-burn flow per
  `project_phase_b_sd_card_automation`.
- **Ship-plan A/C/D lockdown notes** (per dispatch §1 candidates):
  brief inline notes in the README or a `docs/ship-contracts.md`
  documenting the user-visible contracts from the recently-shipped
  arcs:
  - A (auth): first-run welcome → set-password → login flow.
  - C (wifi-prefill): WiFi-during-flash prefill behavior.
  - D (strict-30 fps): the §11 acceptance gate
    (`paint_us_p99 ≤ 33333us`, rolling fps ≥ 30).
  These don't need separate docs each — one paragraph per contract
  in a "What's locked in v0.X.Y" section is enough.

LOC: ~200-500 docs only. Single commit.

### Slice 5 — Tag + GitHub release (~0 LOC, OPERATIONAL)

- `git tag -a v0.X.Y` with CHANGELOG-derived annotation.
- `git push origin v0.X.Y`.
- Build the flashable image via `scripts/build-image.sh`
  (per `project_phase_b_sd_card_automation`).
- **Pre-flash artifact verification** (per dispatch §1 candidate):
  capture the image's SHA256 (sha256sum the .img file); publish the
  hash alongside the GitHub release notes so downstream operators
  can verify integrity post-download. Optional GPG sign of the
  hash file if qarl's release-signing key is set up; otherwise plain
  sha256sum is the minimum-viable integrity surface.
- `gh release create v0.X.Y` with release notes + attach flashable
  image artifact + the sha256sum file.
- Update README.md badge or status line to reflect tagged release.

LOC: 0 (operational). Wall-clock ~30 min.

### Total scope

- 5 slices.
- Production code: ~5 LOC (version bumps).
- Documentation: ~350-550 LOC (CHANGELOG + quickstart + deploy
  checklist + README rewrite).
- Test/fixture: ~90 file updates (potential golden re-bless).
- Operational: 6h soak run + tag + release.

This is a multi-session arc. Slice 1 (soak) is the longest single
wait but smallest single LOC. Slices 2-4 can run in parallel with
slice 1 if multiple agents/sessions are dispatched.

---

## 5. Open questions for qarl

Product-shape decisions only. The slice plan in §4 will make-best-
guess on tactical choices per
`feedback_make_best_guess_on_broad_mandates`.

### Q1: Versioning policy — pick a number for the first tag

Backend = 0.0.0, Renderer = 0.1.0, UI = 0.0.0. Recommendation
options:

- (a) **v0.1.0** — matches renderer's existing version. Signals
  "first release, expect bugs". Doesn't promise API stability.
- (b) **v0.5.0-beta** — signals "feature-complete, expect bugs."
  More polished sounding than 0.1.0.
- (c) **v1.0.0** — signals "this is the real product." Heavyweight.
  Some users interpret 1.0 as "API stable, no breaking changes."
- (d) **CalVer (2026.05.18 or 2026.05)** — sidesteps semver
  expectations entirely. Common for end-user products (Ubuntu,
  Datasette, calendar-versioned apps).

Default if no reply: **(b) v0.5.0-beta**. Honest about pre-1.0
maturity; flushes the "first tag" milestone without overpromising;
upgrade path to v1.0.0 stays clean.

### Q2: Soak duration — 6h floor, 24h, or longer?

`docs/renderer-rewrite-requirements.md:213-217` says "≥6 hours,
ideally overnight." The soak parser defaults to a rolling-10min gate.

Recommendation options:

- (a) **6h** — spec floor; runs overnight; one Pi.
- (b) **24h** — surfaces slower leaks; runs a day on one Pi.
- (c) **6h on multiple Pis** — soak on the dev Pi + fys Pi (qarl's
  test bench) in parallel to catch unit-to-unit variation.

Default: **(a) 6h on the dev Pi at 192.168.50.211** — spec floor;
release-candidate cadence. If (a) PASSES, ship; if it FAILS, escalate
to (b)/(c).

### Q3: Does Phase E require openmarquee.com updates?

`project_site_hero_message` + `project_site_flock_management` memories
exist (referenced by dispatch); the website at `www/public_html/` is
"marketing/coming-soon." A v0.X.Y release tag with a flashable image
in the GitHub release is functional without site updates, BUT
landing-page messaging may want to flip from "coming soon" to
"download now."

Default: **NOT REQUIRED for tag.** Phase E ships the code release;
website refresh is parallel scope (could land in slice 6, or as a
separate dispatch). The hero-message and flock-management memories
point at content updates that can lag the code tag.

### Q4: End-user docs scope — out as not-a-fork

Pre-review subagent noted Q4 (quickstart only vs quickstart +
troubleshooting tree vs full operator runbook) is not a real
product-shape fork — both add additive scope, both can land
post-tag. Implementer make-best-guess on slice 4: ship **quickstart
only** for the v0.X.Y tag; troubleshooting + admin guide land as
docs-PRs post-tag as real issues surface. Withdrawn from the open-
questions list.

---

## 6. Recon summary

- Phase E = §11 soak + Pi golden re-bless + deploy-checklist + docs
  polish + first tagged release (per QA log + IMPLEMENTATION_PLAN
  Phase 11 cross-reference).
- 3 HIGH-priority ship-blockers, 1 MEDIUM, 1 LOW:
  - HIGH: §11 soak run (instrumentation ready, run pending).
  - HIGH: README rewrite (currently warns "no releases yet").
  - HIGH: tag + version bump + CHANGELOG (no tag, all versions
    placeholder).
  - MEDIUM: Pi golden re-bless (90 fixtures from dev-Mac, not Pi).
  - LOW: deploy-checklist doc (operational hygiene; nice-to-have).
- Coverage audit found 1 spec-doc drift (§5.10a editor restructure
  text claims "ships in v3 phase 3" but v3 IS shipped). No
  shipped-code surface lacks spec coverage in ship-critical paths.
  No Phase-4w-shaped audit-doc drift detected on smell-test of the
  5 most-recent audit docs.
- 5-slice plan, ~350-550 LOC docs + ~5 LOC version + ~90 fixture
  files. Multi-session arc; slice 1 (soak) is the longest wall-clock
  wait.
- 3 open questions for qarl (Q4 withdrawn as not a real product
  fork per pre-commit subagent feedback); all have documented
  defaults.

If qarl confirms the defaults: Phase E is "soak run + golden re-bless
+ docs slice + tag" — operational + documentation, ~no production
code. Real ship gate is the soak result.

Phase E does NOT collapse to "already done" like Phase D did — there
are real ship-prep artifacts missing (CHANGELOG, README rewrite, tag).
But it's smaller than its name suggests: most of the spec-mandated
acceptance work (Phases 4-8 + Phase D) is complete; Phase E is
release hygiene + the soak gate firing.
