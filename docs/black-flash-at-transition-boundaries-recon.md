# Black flash at transition boundaries — recon

**Date**: 2026-05-18
**HEAD at recon**: 95c2705 (motion-phase recon, on top of 6251a9e SDF E deploy)
**Scope**: code-only inventory + hypothesis confirmation. No edits.
**Trigger**: backlog item #3 (project_black_flash_at_transition_boundaries, qarl-flagged 2026-05-09 on c8f22d5).

## TL;DR

Symmetric finding to the motion-phase recon: the originally-flagged bug is **diagnosed and FIXED IN CODE AT HEAD** by commit `7c605cce` ("renderer: Bug 2 -- hold scanout FB across in-session render-call boundaries"), landed 2026-05-09 21:14 — **same day** as the qarl-flag. The fix introduces `held_scanout_fb` / `held_scanout_bo` on EglSession plus a single `end_of_in_session_render_call` helper that all 5 in-session render entry points call at end-of-call. `modeset_done` now STAYS TRUE for the session's lifetime; SetCrtc fires once at session start, never again.

Pi-bench validation in the fix commit (FYS heavy flip-pair @1920x1080@60, 4000 frames): `commit_setcrtc` went from 35 fires (pre-fix, ~0.4 per transition entry/exit) to 1 fire (post-fix, the session-start modeset). The black-flash mechanism is gone.

What is open is **on-glass visual confirmation**. The commit message says "visual confirmation deferred to next on-glass session — qarl is at the apartment, not at office glass." That visual verify never closed the loop; the backlog memory still lists the item as open. The SDF arc deploy on 2026-05-18 (6251a9e) ships the fix to the FYS Pi; smoke-soak Step 4A logged 250 begin_slide events with zero frame skips reported by the journal, but that's runtime stability — not a single-frame visible black flash.

Recommended next slice: **glass-verify only, no code**. Same Slice 0 shape as the motion-phase recon. If a residual flash is visible, the recon's Section 3 lists alternate hypotheses to investigate.

---

## Section 1 — Confirm + characterize

**Symptom as qarl flagged it (2026-05-09 at c8f22d5)**: "the screen blinks black for a frame when transitions start/stop." Visible on the FYS reel @1080p; one-to-a-few black frames at every hold↔transition boundary.

**Possible causes inventoried** (per dispatch):

| Cause | Status at HEAD | Notes |
|------|----------------|-------|
| (a) FB cleared but not painted before swap | NOT PRESENT | Every `gl.clear(COLOR_BUFFER_BIT)` in the runtime paths is followed by a full-screen draw before `eglSwapBuffers`. The draw is gated through `?` propagation; if it errors, the swap is skipped. Scanout never sees a cleared-but-not-drawn buffer. |
| (b) Modeset at every transition boundary forcing panel re-sync | **WAS THE BUG** | Fixed by 7c605cce; see Section 2 + Section 3. |
| (c) GL state-leak causing a black draw call | Not observed | No grep evidence of stale-state drawing in transition paths. Both `paint_slide` and the transition shaders bind their own program + uniforms + attribs per call. |
| (d) Decoder/asset not ready when scanout happens | Phase-8-scope, only video paths | `paint_and_present_one_video_slide_frame` (hdmi.rs:2856) explicitly returns Ok without swap if the decoder has no frame ready (lines 2900-2905): "leaves whatever's on screen (last decoded frame or black if never decoded)." For first-frame-of-fresh-video this would briefly show whatever was last on scanout. Phase 8 is HALTED at slices 0+1; not on the live runtime path. |

**Reproducibility today**: not directly. The FYS deploy at 6251a9e ships the post-Bug-2 binary. Smoke-soak Step 4A logged 250 begin_slide / ~6.7 reel cycles with zero panics and zero frame skips in the journal, but the journal doesn't capture single-frame visible glitches — only renderer-internal failures. Slow-motion video on the real sign is the only way to confirm.

---

## Section 2 — Code path inventory

Two distinct runtime path families exist; only one had the bug.

### Family A — In-session render paths (had the bug; fixed)

Five entry points, each with its own per-frame loop terminated by `end_of_in_session_render_call`:

| # | Function | hdmi.rs line | end-of-call call site |
|---|----------|--------------|------------------------|
| 1 | `render_one_frame_in_session` | 1069 | 1128 |
| 2 | `render_animated_slide_in_session` | (~1230) | 1441 |
| 3 | `render_transition_animated_in_session` (legacy 3-pass) | 5598 | 6029 |
| 4 | `render_transition_single_pass_in_session` (SP) | 6186 | 6526 |
| 5 | `render_transition_scissored_bake_in_session` (SB) | 6584 | 7041 |

These are the direct-driver mode paths (CLI usage like `--animate`, `--render-fade-composite`, the standalone test/bench paths). They're NOT the FYS runtime path under the IPC sidecar, but they're what the on-glass tests qarl ran on 2026-05-09 likely exercised (Bug 2 explicitly cites the "FYS heavy flip-pair bench" which runs through the CLI animated path).

### Family B — IPC sidecar paint helpers (never had the bug)

Four entry points called once per `Advance` op from `ipc_main.rs`:

| Function | hdmi.rs line | Caller |
|----------|--------------|--------|
| `paint_and_present_one_frame_for_slide` (text) | 2551 | ipc_main.rs:1044 |
| `paint_and_present_one_image_slide_frame` | 2752 | ipc_main.rs:1067 |
| `paint_and_present_one_video_slide_frame` | 2856 | ipc_main.rs:1100 |
| `paint_and_present_one_transition_frame` | 3080 | ipc_main.rs:1269 |

All four use the existing `scanout_prev_fb` / `scanout_current_fb` rotation (separate from `held_scanout_*` introduced by Bug 2). They share `commit_fb` (hdmi.rs:809) which only takes the SetCrtc branch on first call when `modeset_done = false`; after that ALL commits are page_flip. Bug 2 commit msg notes explicitly: "The IPC dispatcher path didn't have this bug — it uses the existing session.scanout_current_fb / scanout_prev_fb rotation correctly."

This is the live FYS path. PaintSlide (hold) and PaintTransition (transition) both go through the same scanout rotation. Hold-to-transition and transition-to-hold are just consecutive Advance ops that each produce one full painted frame. No "gap" frame in between.

### Clear/draw audit

Greppable `gl.clear(COLOR_BUFFER_BIT)` sites in the runtime hot path:

| Site | hdmi.rs | Followed by |
|------|---------|-------------|
| 1499, 1513 | render_one_frame_in_session body | paint_slide (full-screen) |
| 2495, 2496 | image-slide one-frame helper | upload + run_blit_pass (full-screen) |
| 3006, 3007 | paint_and_present_one_image_slide_frame | upload + run_blit_pass |
| 3290, 3291 | paint_and_present_one_transition_frame | transition shader draw_arrays (full-screen quad) |
| 3724, 3725 | one of the in-session loops body | paint_slide / shader draw |
| 3913, 3914 | scissored-bake atlas region clear | scissored paint into region |
| 4110, 4111 | another in-session site | paint_slide |
| 4689, 4690 | make_slide_fbo bake | paint_slide |
| 4826 | (cont.) | paint_slide |

Every clear is followed by a full-screen draw before any swap_buffers. No "cleared but not painted swapped" path identified.

### Modeset audit

`set_crtc` callers: exactly one, in `commit_fb` (hdmi.rs:860). It only fires when `session.modeset_done == false`. The only paths that set `modeset_done = false`:

- `egl_session.modeset_done: bool` initialized `false` in `with_egl_session`'s session construct (around hdmi.rs:557)
- Set `true` in `commit_fb` after the first successful SetCrtc (hdmi.rs:872)
- **NEVER reset to false anywhere else in hdmi.rs.** Grep confirms.

This is the post-Bug-2 invariant. Before the fix, the 5 in-session paths destroyed their scanout FB at end-of-call AND set `modeset_done = false`, forcing the next call's first commit through SetCrtc. The fix replaced that with `end_of_in_session_render_call` which:

1. drains the pending page-flip event
2. destroys the older within-call FB (off-scanout)
3. destroys the PRIOR call's `held_scanout_fb` (kernel switched away during this call's page_flips)
4. stashes THIS call's current FB into `session.held_scanout_fb` — kernel keeps a valid scanout source across the call boundary
5. `modeset_done` STAYS true

Teardown (`with_egl_session` close, hdmi.rs:689-703) drains `held_scanout_fb` cleanly.

---

## Section 3 — Hypothesis confirmation

**Original hypothesis** (`project_black_flash_at_transition_boundaries.md`): "scanout briefly black at transition entry+exit. Likely cleared-but-not-painted buffer being swapped, or unnecessary modeset."

**Status**: CONFIRMED + RESOLVED.

The "unnecessary modeset" half of the hypothesis was the actual root cause. Commit `7c605cce` (2026-05-09 21:14, same day as the qarl-flag) lays out the diagnosis: every in-session render call destroyed its scanout FB and reset `modeset_done = false`, so the NEXT call's first commit went through SetCrtc instead of page_flip. On vc4, every SetCrtc forces a panel re-sync = visible black frame.

**Pi-bench validation, from the fix commit:**

| | Pre-fix (FYS heavy flip-pair, 4000 frames) | Post-fix |
|---|---|---|
| commit_setcrtc count | 35 / 4000 | 1 / 4000 |
| commit_setcrtc total ms | 1088 ms | 47.6 ms |
| flip transition fps | 26.6-26.7 | 27.1 |
| frame_total p99 | 41.6 ms | 41.3 ms |

The kernel re-syncs only ONCE at session start, not at every transition boundary.

**The "cleared-but-not-painted" half of the hypothesis is not the bug.** Section 2's clear/draw audit found no clear path that isn't followed by a full-screen draw before swap.

**Could there still be a residual?** Two narrow scenarios worth checking on glass:

1. **vc4 page_flip ASYNC tearing.** The fix uses `DRM_MODE_PAGE_FLIP_ASYNC` so the kernel performs the flip immediately rather than waiting for vblank (hdmi.rs:876-888). Tradeoff acknowledged in the commit: tearing during the half-vblank window between flip and next vblank. Acceptable for the FYS reel per qarl. Worth noting that a particularly unlucky tear at a transition boundary could read visually as a brief glitch — distinct from the original Bug 2 mechanism but adjacent.
2. **V4L2 first-frame-not-ready (Phase 8 scope).** `paint_and_present_one_video_slide_frame` (hdmi.rs:2856-2920) returns Ok without swap when the decoder has no frame ready, "leaves whatever's on screen (last decoded frame or black if never decoded)." First transition into a fresh video slide could surface black briefly if the decoder takes >1 Advance to prime. **NOT on the live runtime path today** — Phase 8 slices 2+ are halted. When/if Phase 8 resumes, this becomes a real concern.

**Refined hypothesis (HEAD-aware)**: Bug 2 is structurally resolved. If a residual flash is still visible on glass post-6251a9e, the most likely culprits are page_flip ASYNC tearing (Scenario 1) or, after Phase 8 resumes, V4L2 first-frame priming (Scenario 2). Neither is a regression of Bug 2 itself.

**Confidence**: high. The structural fix is intact at HEAD (5 call sites of `end_of_in_session_render_call`, zero remaining `modeset_done = false` resets, `held_scanout_fb` teardown clean). Pi-bench numbers from 2026-05-09 quantified the fix concretely.

---

## Section 4 — Test strategy

**Primary verification: glass-time A/B on FYS Pi.** Same shape as the motion-phase recon's Slice 0. Specifics for black-flash:

1. Pick any 2-slide pair from the FYS reel that has a visually-distinct transition (fade / wipe / iris). High-contrast slide backgrounds make a black flash more obvious.
2. Record slow-motion video (≥120 fps phone capture or HDMI capture device). 60 fps is enough since the bug if present would be a single-frame artifact at 30Hz scanout = 33ms long, captured in ~4 frames at 120fps.
3. Step through the hold → transition entry → transition → exit → hold sequence frame-by-frame. Look for any frame that is entirely or significantly black.
4. Repeat across at least 2-3 transition kinds (fade, wipe, dissolve) since the IPC path takes the same `paint_and_present_one_transition_frame` regardless of kind, but the shader work differs.

**Secondary verification: profile-mode commit_setcrtc count.** The existing `crate::profile::record_phase("commit_setcrtc", ...)` instrumentation (hdmi.rs:868-871) records every SetCrtc fire. Running the FYS reel with `OPENMARQUEE_PROFILE_FRAMES=4000` (or similar; check the actual env-var name) and reading the `commit_setcrtc` count from the profile output gives the SAME measurement that validated the original fix:

- Expected: 1 setcrtc per session (the initial bringup).
- Bug-2 regression: setcrtc count tracks transition-boundary count (e.g. 35/4000 for the original bench).

This is run-on-Pi-only (vc4 hardware-specific) but doesn't need on-glass eyeballs — purely numeric. If the count is >1, the bug regressed.

**Tertiary verification: regression-lock test.** The fix commit noted "GL-scanout state is un-mockable; the 35->1 setcrtc delta IS the load-bearing test." No automated regression test was added. **This is a coverage gap**: a future refactor that re-introduces `modeset_done = false` at end-of-call (or skips wiring the new helper into a new render entry point) would not be caught by `cargo test` and would only surface on glass.

A test analogous to `legacy_3pass_transition_re_bakes_animated_layers_per_frame` (the source-grep regression test for Bug 1's fix at hdmi_logic.rs:8395) could be added:

```rust
#[test]
fn end_of_in_session_render_call_used_by_all_in_session_paths() {
    let source = read_hdmi_rs_source();
    // Each of the 5 in-session entry points must call the cleanup helper.
    assert!(source.contains("end_of_in_session_render_call("));
    let calls = source.matches("end_of_in_session_render_call(").count();
    assert!(calls >= 6, "expected ≥5 callers + the fn defn = ≥6 matches");
    // Hard fail if anyone re-introduces the bug pattern.
    assert!(!source.contains("modeset_done = false"));
}
```

Cheap (~30 lines), source-grep style same as the Bug 1 lock. Worth filing as a follow-up if Slice 0 verdict is "no residual" — locks against silent regression.

---

## Section 5 — Implementation slice plan

**Slice 0 — glass-time verify (REQUIRED, no code).** Same shape as the motion-phase recon. Owner: qarl on-site at FYS. Recipe per Section 4 Step 1-4. Output: short verdict — "black flash visible: yes / no" and which transition kinds if yes. Pre-condition for all subsequent slices below.

**If Slice 0 verdict is "no residual visible"**: backlog item #3 closes. Update `project_black_flash_at_transition_boundaries.md` to "RESOLVED in 7c605cce; verified on glass 2026-MM-DD". Add a CHANGELOG entry. Optionally land Slice 1 below as a regression-lock add-on.

**If Slice 0 verdict is "residual visible"**: take Slice 2 below (diagnostic) before any code work.

**Slice 1 (optional, no code-impact) — source-grep regression test.** Pre-condition: Slice 0 closed "no residual". Add the `end_of_in_session_render_call_used_by_all_in_session_paths` test sketched in Section 4 to `renderer/src/hdmi_logic.rs::tests`. Pair with the existing Bug 1 lock (line 8395). Catches re-regression of Bug 2 in the same way the Bug 1 lock catches that fix being undone. Estimated: ~40 lines, one commit, no parity impact.

**Slice 2 (conditional diagnostic) — narrow the residual.** Pre-condition: Slice 0 shows a flash. Recipe:

1. Re-run on Pi with `OPENMARQUEE_PROFILE_FRAMES=4000` (or similar; verify env-var name in profile.rs) plus the FYS reel. Read the `commit_setcrtc` count.
   - If count >1: Bug 2 regressed. Bisect commits 7c605cce..HEAD for the offending change (post-2026-05-09 work touched hdmi.rs heavily — Phase 8 V4L2 slices, SDF arc B.3 cutover, etc.).
   - If count == 1: not a SetCrtc-modeset issue. Move to step 2.
2. Enable `OPENMARQUEE_BOUNDARY_TRACE=1` and capture stderr during a transition. Each painted frame emits a JSON line with per-phase μs deltas. Look for outliers — a paint or swap that took an unusually long time (≥33ms) on a boundary frame would surface as a dropped/late scanout = visible black.
3. If neither commit_setcrtc nor BOUNDARY_TRACE explains it: hypothesis becomes (a) page_flip ASYNC tearing — confirmed by switching ASYNC off temporarily and re-testing; (b) V4L2 first-frame priming — only if the failing slide pair includes a video. Document the actual culprit in the recon doc + take Slice 3.

**Slice 3 (conditional code fix) — path-specific repair.** Scope depends on Slice 2 findings. Likely shapes:

- SetCrtc count regression: revert/forward-port the offending commit's interaction with `end_of_in_session_render_call`. Probably <30 lines, single commit.
- ASYNC tearing: tradeoff revisit — switch to `PageFlipFlags::EVENT` (without ASYNC) at the cost of vblank-wait latency. The commit msg for Bug 2 documents the tradeoff explicitly; it's a one-line flag change but moves perf budget. Needs Pi-bench re-validation.
- V4L2 first-frame: pre-warm the decoder one Advance earlier (BeginSlide or BeginTransition triggers the first dqbuf), or paint the previous slide's last frame on no-frame-ready instead of leaving stale scanout. Phase-8-scope; defer until Phase 8 resumes.

Risk note for any fix that touches `paint_slide` or `make_slide_fbo`: the SDF E parity bless landed 35 modified goldens (commit 5c750b7). Any change that affects the pixel output of a transition frame would need a parity re-bless. The `end_of_in_session_render_call` fix in 7c605cce did NOT affect pixel output (only commit timing), so it didn't trigger a re-bless. A Slice 3 fix targeting the same layer should preserve that property if possible.

---

## Open questions / risks

- **Is glass-verify happening soon?** Per backlog #4 ("Phase 7 #218 mystery bugs") qarl is queued for an office visit; that's the natural moment to do glass-verify on both Bug 1 (motion-phase) and Bug 2 (black-flash) at once. The two items have been entangled since 2026-05-09 (qarl flagged both in the same sitting on c8f22d5); resolving them together preserves that natural pairing.
- **vc4 page_flip ASYNC vs vblank-locked**: the explicit tradeoff in Bug 2's fix is acceptable for FYS posture today. If a future deployment target has stricter visual-perfection requirements (e.g. a cinema-display install), ASYNC may need to be revisited. Not blocking for FYS.
- **Phase 8 V4L2 video transitions**: when slices 2+ resume, the new video-transition paint paths must (a) call `end_of_in_session_render_call` if direct-driver, OR (b) use the existing IPC scanout rotation, AND (c) prime the V4L2 decoder before the first frame-on-glass to avoid the "black if never decoded" path. The standing rule is in code comments but discipline is on whoever picks up Phase 8.
- **No automated regression-lock for Bug 2** (gap noted in Section 4 + Slice 1). A refactor of `commit_fb` or the scanout rotation could silently re-introduce the bug. Cost to add a source-grep test is low (~40 lines).
- **Coupling with motion-phase recon's Slice 0**: both bugs need glass verify. If qarl sees a flash that snaps motion phase as a side effect (black frame held over motion's peak), the two diagnoses re-entangle the way they did on 2026-05-09. Run the verifies separately if at all possible — one slide pair for motion (high-amplitude motion layer, low-contrast bg) and a different one for flash (high-contrast bg, simple or no motion).

---

## References

- Commit `7c605cce`: "renderer: Bug 2 -- hold scanout FB across in-session render-call boundaries", 2026-05-09 21:14. The fix itself + Pi-bench validation.
- Commit `7417ae0`: "renderer: Bug 1 -- session-global tick_seconds basis", 2026-05-09 21:24. Sister fix in the same sitting; addresses motion-phase discontinuity (backlog #2).
- `docs/motion-phase-discontinuity-recon.md`: parallel recon on backlog #2 (95c2705). Same shape; same conclusion.
- `qa/captures/motion-through-transitions-audit-2026-05-16.md`: canonical 5-path audit for motion. Touches scanout rotation as context but doesn't address black-flash specifically.
- Canonical Bug 2 fix points: `EglSession::held_scanout_fb` / `_bo` (hdmi.rs:298-299), `end_of_in_session_render_call` (hdmi.rs:976), `modeset_done` lifecycle docstring (hdmi.rs:384-403), session teardown drain (hdmi.rs:689-703).
- Backlog memory: `project_black_flash_at_transition_boundaries.md`. Sibling: `project_motion_phase_discontinuity_at_transitions.md`.
- IPC paint helpers (Family B, never had the bug): `paint_and_present_one_frame_for_slide` (2551), `paint_and_present_one_image_slide_frame` (2752), `paint_and_present_one_video_slide_frame` (2856), `paint_and_present_one_transition_frame` (3080) — all in hdmi.rs.
