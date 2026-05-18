# Motion-phase discontinuity at transition boundaries — recon

**Date**: 2026-05-18
**HEAD at recon**: 6251a9e (SDF E deploy)
**Scope**: code-only inventory + hypothesis refinement. No edits.
**Trigger**: backlog item #2 (project_motion_phase_discontinuity_at_transitions, qarl-flagged 2026-05-09 on c8f22d5).

## TL;DR

The originally-hypothesized bug — "tick_seconds basis mismatch between hold-loop and transition-loops" — was **diagnosed correctly on 2026-05-09 and is FIXED IN CODE at HEAD**. Three commits over a 9-day window plumbed `session.motion_tick_seconds()` (session-global monotonic basis) into every render path that previously held a call-local clock or a static-snapshot bake. The audit at `qa/captures/motion-through-transitions-audit-2026-05-16.md` (+ post-Phase-4w correction note) is the canonical reference; this recon doesn't re-do that work, it confirms it stands.

What is open is **glass-time verification on the FYS Pi post-fff3ab8** (the last of the three fixes, landed 2026-05-16). The current FYS deploy at 6251a9e (2026-05-18) contains that fix. On-glass A/B against the qarl-flagged symptom has not run.

Recommended next slice: **glass-verify only, no code**. If the symptom is still visible, the recon Section 5 alternatives become the implementation plan; if it's gone, the backlog item closes with no further code work.

---

## Section 1 — Reproduce + characterize

**Symptom as qarl flagged it (2026-05-09 at c8f22d5)**: "the phase of the motion seems to get confused when transitions start/stop." Visible on the live FYS reel @ 1080p60: per-layer motion (shake / bounce / pulse / breathe / blink / ticker) appeared to skip phase at every hold ↔ transition boundary. The audit doc additionally noted that animated layers (kenburns, scroll/marquee, etc.) appeared to **freeze entirely** during transitions — a related but distinct issue.

**Two underlying bugs were tangled in the one symptom:**

1. **Phase reset at boundary** — call-local `start = Instant::now()` in each in-session render entry point. tick_seconds reset to 0 every time control crossed a function boundary. `compute_motion_state`'s `sin(2*pi*freq*tick + phase)` snapped phase at every hold↔transition crossing.
2. **Motion frozen during transition** — the IPC `paint_and_present_one_transition_frame` bake helper (`make_slide_fbo`) didn't accept motion_states at all; baked a static snapshot of both endpoints once per Advance call. The transition shader then crossfaded two frozen images.

Both contribute the same on-glass behavior (phase looks wrong at boundaries), which is why one qarl-flag captured both.

**Reproducibility today**: not yet verified on glass post-fff3ab8. The full FYS reel runs through `parity_fys_*` fixtures continuously during the smoke-soak heartbeat (Step 4A of the SDF E deploy logged 250 begin_slide events / ~6.7 full reel cycles with zero panics). But that's a runtime-stability check, not a motion-continuity-at-boundaries check — slow-motion video review on the actual sign is the only way to confirm the symptom is gone.

---

## Section 2 — Code path inventory

Five render paths can drive a transition between two slides. All five are believed correct at HEAD. The audit at `qa/captures/motion-through-transitions-audit-2026-05-16.md` walked all five; what follows is the post-Phase-4w status table.

| # | Path | Entry | Status at HEAD | Plumbing source |
|---|------|-------|----------------|-----------------|
| 1 | IPC PaintTransition | `paint_and_present_one_transition_frame` (hdmi.rs:3080) | ✅ FIXED | fff3ab8 (2026-05-16, Phase 4v-3b) |
| 2 | Legacy 3-pass | `render_transition_animated_in_session` (hdmi.rs:5598) | ✅ Already correct since 2b0cbef (2026-05-07) | Audit-miss caught in Phase 4w; regression-locked by hdmi_logic.rs:8395 |
| 3 | Single-pass (SP) | `render_transition_single_pass_in_session` (hdmi.rs:6186) | ✅ Always correct (per-frame motion_states + uniform writes) | — |
| 4 | Scissored-bake (SB) | `render_transition_scissored_bake_in_session` (hdmi.rs:6584) | ✅ Always correct (per-frame motion_states gated on any_animated_*) | — |
| 5 | Canvas2D inline preview | `drawSlot` (ui/src/inline-preview.js:255–700) | ✅ Always correct (per-RAF drawSlot on both endpoints) | — |

The tick-seconds-basis fix has three layers that compose:

- **7417ae0** (2026-05-09): introduced `EglSession::motion_tick_seconds()` returning `session_start.elapsed().as_secs_f64()` — session-global, never reset. Repointed the four in-session entry points:
  - `render_animated_slide_in_session` (hold loop)
  - `render_transition_animated_in_session` (legacy 3-pass)
  - `render_transition_single_pass_in_session` (SP)
  - `render_transition_scissored_bake_in_session` (SB)
- **413efca** (2026-05-13): extended to the IPC sidecar's `paint_and_present_one_frame_for_slide` (hold-path), which was deriving tick from `t_in_slide_ms / 1000` — reset to 0 at every BeginSlide. Now uses `session.session_start.elapsed()` matching the four standalone paths.
- **fff3ab8** (2026-05-16): plumbed `motion_states: Option<&[MotionState]>` THROUGH `make_slide_fbo` into `paint_slide` so the IPC PaintTransition path can pass per-frame states. `paint_and_present_one_transition_frame` now computes `motion_states_for_layers(...)` for both endpoints each Advance call using `session.motion_tick_seconds()` and passes Some(&states_*). This fixed Path #1, which was the primary live-on-FYS path.

**Canonical motion-tick derivation (the structural guard)** — hdmi.rs:4216:

```rust
pub fn motion_tick_seconds(&self) -> f64 {
    self.session_start.elapsed().as_secs_f64()
}
```

The docstring (hdmi.rs:4206–4215) is explicit: "Future paint entry points: call this, do NOT roll your own from a call-local clock." Any new render entry must use this method; the four-callsite count is now closer to seven (the three callers above plus three transition-loop sites plus one capture path).

**Capture path note (intentional outlier)** — `paint_one_for_capture` (hdmi.rs:4459) is the goldens-capture path and derives tick from `t_in_slide_ms / 1000`. This is *intentional*: capture pins motion to a deterministic per-fixture tick so re-bakes are reproducible. Not the runtime path; not in scope for the boundary-phase question.

---

## Section 3 — Hypothesis confirmation / refinement

**Original hypothesis** (`project_motion_phase_discontinuity_at_transitions.md`): "tick_seconds basis mismatch between hold-loop and transition-loops."

**Status**: HISTORICALLY CORRECT, REFUTED AT HEAD. The hypothesis pinpointed the right root cause when filed on 2026-05-09. Three commits later (7417ae0 + 413efca + fff3ab8) every render path uses the same session-global basis. The audit doc's post-Phase-4w correction note + the regression-lock test at `hdmi_logic.rs::tests::legacy_3pass_transition_re_bakes_animated_layers_per_frame` (hdmi_logic.rs:8395) close the loop.

**Refined hypothesis (HEAD-aware)**: The originally-flagged symptom is either (a) **fully fixed and just needs on-glass A/B to confirm**, or (b) **partially residual via a different mechanism** — most plausibly one of:

1. **Black flash at boundary (separate backlog item #3)** — `project_black_flash_at_transition_boundaries.md`, qarl-flagged in the same 2026-05-09 sitting. Mechanically distinct (cleared-but-not-painted buffer in scanout, or an unnecessary modeset between hold and transition) but visually adjacent. Could be misread as "phase skip" if the black frame falls on a peak of the sine motion.
2. **MSDF-vs-AlphaBitmap intensity divergence** (post-SDF arc, B.3+ deploy 2026-05-18) — the SDF arc's smoothstep AA produces a different intensity distribution than the deleted AlphaBitmap gradient. A motion blur or motion-driven offset that was visually-smooth under AlphaBitmap might now show step transitions at low-amplitude phase values. Speculative but worth eyeballing.
3. **A new entry point added since fff3ab8 that rolls its own tick** — grep at HEAD finds no such site, but Phase 8 V4L2 slices are halted mid-arc (slices 0+1 committed, 2+ paused per qarl-Pi-debug priority); if slice 2+ adds a video transition entry point it must call `motion_tick_seconds()`. Standing rule in the docstring guards against this; a new sub-path would still need conscious adherence.

**Confidence**: high that the originally-described bug is fixed. The audit doc + regression test + the doctrinal `motion_tick_seconds()` guard cover the structural concern. Glass-time A/B is the missing empirical check.

---

## Section 4 — Test strategy

**Primary verification: glass-time A/B on FYS Pi**. The current deployed binary (6251a9e at qarl@192.168.1.67) contains all three fixes. Recipe:

1. Pick a motion-bearing slide pair from the FYS reel. Any of the parity_fys fixtures with `motion=breathe|pulse|shake|bounce|blink|ticker` on a text layer, transitioning into another slide of the same shape.
2. Record short slo-mo video (240fps phone capture or screen-grab from HDMI capture device) covering hold → transition entry → transition window → transition exit → hold.
3. Step through entry / exit frames. Look for visible phase snap (a "tick" or jump in the motion's position at boundary crossings).
4. Repeat across at least 3 transition kinds (e.g. `fade`, `wipe`, `iris`) since the SP / SB / legacy paths take different shaders.

**Secondary verification: synthetic boundary continuity assertion**. Optional, only if glass shows a residual. Drive the renderer state machine across a hold→transition boundary, capture motion_states at hold-end frame N and transition-start frame N+1, assert continuity of `(state[i].dx, state[i].dy, state[i].alpha, ...)`. This would catch a regression in *code* without needing on-glass capture. A natural sibling to the existing `compute_motion_state` continuity tests in hdmi_logic.rs (which assert phase continuity at t=0.50 → 0.59 within a single call; not the same as call-boundary continuity).

**Tertiary verification: motion-state log scrape**. Already partially supported — the renderer emits `rasterized text` log lines per slide. Add temporary instrumentation that dumps `motion_states[0]` at each Advance call; compare hold-end and transition-start values. Cheap, no glass needed, catches structural regressions only. Not worth doing speculatively; useful only as a diagnostic if Step 1 above shows a residual.

**Out of scope for this recon's verification**: black-flash boundary issue (separate backlog #3). Worth keeping separate so the diagnosis paths don't entangle again the way they did on 2026-05-09.

---

## Section 5 — Implementation slice plan

**Slice 0 — glass-time verify (REQUIRED, no code)**. Recipe per Section 4 Step 1–4. Owner: qarl (only path; no other operator has glass access). Output: short verdict — "phase snap visible: yes / no" and which transition kinds if yes. Pre-condition for ALL subsequent slices below.

**If Slice 0 verdict is "no residual visible"**: close the backlog item. Update `project_motion_phase_discontinuity_at_transitions.md` to "RESOLVED in fff3ab8; verified on glass 2026-MM-DD". Add a CHANGELOG entry. No additional code work.

**If Slice 0 verdict is "residual visible"**: take Slice 1 below; further slices conditional on Slice 1 findings.

**Slice 1 (conditional) — narrow which path is residual**. Pre-condition: Slice 0 produced specific transition kinds where the issue is visible. Run the parity capture pipeline (`scripts/parity/bless_fys_goldens.py`-style) for the specific failing fixture(s) and inspect the rendered intermediate frames. If only certain transition kinds show it, that points to the SP / SB / legacy split: kinds where eligibility forces the legacy path will look different from kinds where SP/SB takes over.

Approx scope: recon-only; no code edits. Output: a doc that names the specific render path responsible (one of the five) + the specific frame indices where phase visibly snaps. Estimated: 1 commit, docs only.

**Slice 2 (conditional) — path-specific code fix**. Pre-condition: Slice 1 identified a specific path. Fix shape depends on path:

- If Path #1 (IPC PaintTransition): re-audit fff3ab8's plumbing for a missed call site. Most likely: the `motion_states` passed in is computed correctly but doesn't reach the inner `paint_slide` invocation for some endpoint variant. Re-read the slice 4 dispatcher (`bake_slide_to_fbo`) for the Image and Video endpoint paths — Text endpoint plumbing is the one verified by fff3ab8's tests.
- If Path #2 (legacy 3-pass) under direct-driver mode: should be regression-locked by hdmi_logic.rs:8395; if the assertion is passing but glass still shows the bug, the assertion's depth is insufficient. Add a second-layer test that drives the loop with a real motion-bearing layer and asserts state continuity across frame-N→frame-N+1 within the loop AND across loop entry from a prior hold.
- If Path #3 or #4 (SP / SB): per-frame `motion_states_for_layers` already runs; check whether `prepare_layers_for_single_pass` (SP) or `bake_slide_to_fbo` via SB drops the values on the floor for any layer kind.
- If Path #5 (Canvas2D): re-audit `drawTextSlideAnimated` for `elapsed_s` continuity across `drawSlot` calls within a transition window.

Estimated scope per path: 20–80 lines, single file (hdmi.rs or hdmi_logic.rs), one commit.

**Slice 3 (optional, only if Slice 2 lands) — synthetic boundary continuity test**. Add the hdmi_logic.rs-level test described in Section 4 secondary verification. Pre-condition: a Slice 2 fix actually shipped; this protects against re-regression. Without a real bug to lock down, the test would assert behavior that's already structurally guaranteed by `motion_tick_seconds()`. Estimated: 30–60 lines, one commit.

---

## Open questions / risks

- **Can the symptom be triggered from existing parity_fys fixtures?** The 36 fixtures captured at SDF E (6251a9e) all use specific slides + transition kinds. If glass shows the residual, but it ONLY affects specific kind+motion combinations not in the parity set, the issue would be invisible to the goldens pipeline. Verifying this requires Slice 0 + slow-motion video.
- **Does the SDF arc deployment (2026-05-18) introduce a regression independent of the original bug?** The SDF arc touched `paint_slide`'s text rendering (B.2 cutover); B.3 deleted the AlphaBitmap path. Motion handling code itself (`motion_states_for_layers`, `compute_motion_state`) is upstream of the text rendering split and was not modified. But verifying that the MSDF text path consumes motion offsets the same way as AlphaBitmap did is a sanity check.
- **Phase 8 (V4L2 video decode) is halted mid-arc.** If/when slice 2+ resumes, new video-transition entry points must hit `motion_tick_seconds()`. The standing rule (hdmi.rs:4206–4215 docstring) covers this, but it's a discipline check on whoever picks up Phase 8.
- **Black flash item (backlog #3) is structurally adjacent.** A simultaneous fix attempt on both bugs in 2026-05-09 would have entangled them. The recon recommends keeping them separate; if Slice 0 surfaces both as glass-visible, treat them as two distinct dispatches.

---

## References

- `qa/captures/motion-through-transitions-audit-2026-05-16.md` — canonical 5-path audit + post-Phase-4w correction. Read in full before any Slice 1+ code work.
- Commits: 7417ae0 (in-session tick fix), 413efca (IPC PaintSlide tick fix), fff3ab8 (IPC PaintTransition motion-through), 2b0cbef (legacy 3-pass live re-bake), 831f471 (Phase 4w regression lock).
- Regression test: `renderer/src/hdmi_logic.rs::tests::legacy_3pass_transition_re_bakes_animated_layers_per_frame` (line 8395).
- Canonical tick basis: `EglSession::motion_tick_seconds()` (hdmi.rs:4216).
- Motion-state helper: `motion_states_for_layers` (hdmi.rs:4649) — uses `layer_id_seed(slide_id, i)` per `feedback_structural_identity_for_seeds`.
- Backlog memory: `project_motion_phase_discontinuity_at_transitions.md`. Sibling: `project_black_flash_at_transition_boundaries.md`.
