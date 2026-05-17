# Stale-comment audit — renderer Rust files (2026-05-17)

## Intent

End-of-night carry-forward from Phase 4w + FS_BRIGHT_GAMMA comment-drift
fixes. Two recent drifts in 24h suggested a pattern; this audit samples
the four heaviest-comment Rust files for similar drift.

## Scope

Files audited:

- `renderer/src/hdmi.rs` (10656 lines at HEAD)
- `renderer/src/hdmi_logic.rs` (8580 lines at HEAD)
- `renderer/src/ipc_main.rs` (2003 lines at HEAD)
- `renderer/src/profile.rs` (168 lines at HEAD)

Method: greppped `(Phase|slice [0-9]|piece [0-9]|will|planned|parked|TODO|Step [0-9])`
in HEAD blobs; spot-checked the highest-signal hits + comments around
recent commit touch sites. Classified per dispatch rubric (A/B/C/D).

Verification: all "Actual code behavior" claims read from
`git show HEAD:<file>` (working-tree-blind), and shipped/landed claims
cross-checked against `git log --oneline` for the relevant commit
hashes. The `legacy_3pass_transition_re_bakes_animated_layers_per_frame`
regression-lock test referenced by hdmi.rs:4915-4920 was confirmed to
exist at `hdmi_logic.rs:8505`.

`profile.rs` (168 lines) was reviewed comment-by-comment and showed
ZERO drift — all comments accurate at HEAD. Skipped in the findings
below; it's the audit-floor reference for "clean."

---

## Findings (ordered by severity — worst-drift first)

### 1. `renderer/src/hdmi_logic.rs:2106` — "to verify in piece 4e" is stale; piece 4e verified GREEN

**Classification**: B (stale fact)

**Comment text** (verbatim):
> **Documented assumption to verify in piece 4e:** on the Pi 4
> dev board's vc4 + Mesa stack, color output from this shader
> matches FS_NV12_TO_RGB output side-by-side. If the on-Pi smoke
> shows a color cast or wrong range (e.g. dark-shadow elevated),
> fall back to manually applying the BT.601 transform on the
> .r/.g/.b channels here.

**Actual code behavior** (verified at HEAD):
> Piece 4e shipped 2026-05-14 with verdict GREEN
> (`qa/captures/v4l2-piece4e-dmabuf-smoke-2026-05-14.md`):
> 6.3× mean / 9.1× p50 improvement over MMAP-upload baseline. No
> color cast or range issue surfaced; the assumption was verified.
> The FS_NV12_DMABUF_TO_RGB shader is production-wired via
> ipc_main.rs:413-418's `OPENMARQUEE_RENDERER_DMABUF` env-var gate.

**Recommended fix**: rewrite as past-tense "Verified in piece 4e
(2026-05-14, qa/captures/v4l2-piece4e-dmabuf-smoke-2026-05-14.md):
Mesa BT.601 fast-path produces correct colors on the Pi dev board."
Drop the "fall back to manually applying BT.601" hedge — no
fallback needed.

---

### 2. `renderer/src/hdmi.rs:4635` — "Phase 8 slice 4, planned" — slice 4 landed 2026-05-16

**Classification**: C (stale plan)

**Comment text** (verbatim):
> Once-per-transition (Phase 8 slice 4, planned): video
>     freezes at transition start, destination starts from its
>     first frame at transition end (Option C in the slice 0
>     recon doc).

**Actual code behavior** (verified at HEAD):
> Phase 8 slice 4 shipped at `4dcc7b2` (2026-05-16), slice 5 at
> `e285e81`, slice 6 at `1c61747`. The slice-6 commit hdmi.rs:2957
> comment explicitly documents that the Video cadence chosen was
> Option D (play-through), NOT Option C (snapshot). Slice 6's docstring
> says: "video drains one V4L2 sample per Advance, so video frames
> keep playing THROUGH the transition window alongside Text motion
> phase." So both the "planned" qualifier AND the specific Option C
> reference are stale.

**Recommended fix**: rewrite as past-tense "Phase 8 slice 6
(2026-05-16) chose Option D — video drains per Advance through the
transition window (see hdmi.rs:2957)." Drop "planned" + Option C
reference.

---

### 3. `renderer/src/hdmi.rs:5325` — "parked for 4w" — Phase 4w concluded as no-op

**Classification**: B (stale fact)

**Comment text** (verbatim):
> Phase 4v-3b: render_fade_composite remains a static-snapshot
> bake (motion_states=None); parked for 4w alongside the other
> legacy bake sites. See audit doc 2026-05-16.

**Actual code behavior** (verified at HEAD):
> Phase 4w landed at `831f471` (2026-05-16) and was a no-op + audit
> correction — the commit message reads "legacy 3-pass was ALREADY
> motion-correct" (see git log). The "other legacy bake sites" the
> comment refers to (`render_transition_animated_in_session`) were
> confirmed correct, not changed. `render_fade_composite` itself
> remains motion_states=None because it's a single-frame composite
> with no per-frame loop — that's intentional, not parked. The
> "parked for 4w" framing implies pending work that no longer exists.

**Recommended fix**: rewrite as "Phase 4v-3b: render_fade_composite
intentionally passes motion_states=None — it's a single-frame
composite with no per-frame loop, so a static-snapshot bake is
correct (Phase 4w 2026-05-16 audit confirmed this site needs no
change)."

---

### 4. `renderer/src/ipc_main.rs:413-418` — "after qarl eyeballs piece 4e's smoke" — eyeball happened, default unchanged

**Classification**: C (stale plan)

**Comment text** (verbatim):
> V4L2 piece 4d (2026-05-14): opt-in DMA-BUF zero-copy path
> via env var. Default stays MMAP for safety (the dispatch
> recommendation flips the default after qarl eyeballs piece
> 4e's smoke). Set BEFORE allocate_buffers so REQBUFS uses
> the right memory type.

**Actual code behavior** (verified at HEAD):
> Piece 4e smoke shipped GREEN on 2026-05-14
> (qa/captures/v4l2-piece4e-dmabuf-smoke-2026-05-14.md). The
> `use_dmabuf` env-var gate at L418-422 still defaults to false
> (MMAP). Either qarl deliberately decided to keep MMAP-default
> after the smoke, or the flip is a still-pending follow-up. Either
> way, the comment's "after qarl eyeballs" milestone has passed and
> the current state doesn't match the implied plan.

**Recommended fix**: re-verify intent with qarl before touching.
If MMAP-default is deliberate, rewrite as "Default stays MMAP for
safety despite GREEN piece 4e smoke (qarl 2026-05-14 decision:
<reason>)." If the flip is a pending follow-up, leave the comment
and dispatch the flip as a separate change. NOT a comment-only fix.

---

### 5. `renderer/src/hdmi.rs:5095-5098` — "slice 4 will route through" — slice 4 shipped

**Classification**: C (stale plan)

**Comment text** (verbatim):
> A `Ok(None)` from the video helper (no frame ready this
> tick) maps to an `Err` here — for the transition path
> slice 4 will route through, a "no frame ready" snapshot
> can't be honored as a transition input. Caller decides how
> to handle the error (retry, FS_CUT fallback, etc.).

**Actual code behavior** (verified at HEAD):
> Slice 4 shipped at `4dcc7b2` (slice 6 wired Video at `1c61747`).
> `paint_and_present_one_transition_frame` now accepts
> `TransitionEndpoint<'_>` carrying per-kind state and dispatches
> through `bake_slide_to_fbo`. The video error mapping IS in place.
> The "will route through" future tense is now history.

**Recommended fix**: change "slice 4 will route through" to "the
slice-4-wired transition path routes through". Tense flip only.

---

### 6. `renderer/src/hdmi.rs:5105-5107` — "Slice 4 wires it into" — slice 4 shipped

**Classification**: C (stale plan)

**Comment text** (verbatim):
> Slice 3 introduces the dispatcher; NO caller is updated to
> use it this slice. Slice 4 wires it into
> `paint_and_present_one_transition_frame` so the IPC
> PaintTransition path stops hardcoding `slide_a: &TextSlide`.

**Actual code behavior** (verified at HEAD):
> Slice 4 landed at `4dcc7b2`; the wiring is in place. The
> "will wire it into" future tense is history.

**Recommended fix**: rewrite as past-tense "Slice 3 introduced the
dispatcher; slice 4 (4dcc7b2) wired it into
paint_and_present_one_transition_frame." Or delete the slice
history line entirely — the docstring's primary purpose is to
document the dispatcher contract, not the slice rollout.

---

### 7. `renderer/src/hdmi.rs:5208-5212` — "in slice 4 can decide" — slice 4 shipped

**Classification**: C (stale plan)

**Comment text** (verbatim):
> Free the pair and propagate an
> explicit error so the transition-path caller
> in slice 4 can decide between retry and
> FS_CUT fallback.

**Actual code behavior** (verified at HEAD):
> Slice 4 + slice 6 shipped. The transition-path caller exists and
> handles the error. The "in slice 4 can decide" future-tense
> qualifier is history. (Worth re-checking what slice 4 / 6
> actually does in the error case — the comment's "decide between
> retry and FS_CUT fallback" was a slice-3 forecast; verify the
> shipped slice-4-6 behavior matches.)

**Recommended fix**: tense flip + cross-reference verify. Drop "in
slice 4" or change to "the transition-path caller handles the
error (see paint_and_present_one_transition_frame at LXXXX)."

---

### 8. `renderer/src/hdmi.rs:967-970` — "slice (b)+ will let the reel driver hold one session" — IPC sidecar achieved this

**Classification**: C (stale plan)

**Comment text** (verbatim):
> v1-spec-delta #5 (slice a, 2026-05-08): the EGL/GBM bring-up
> + teardown is now extracted into `with_egl_session`. This
> function still does its own session per call (no behavior
> change vs slice 0); slice (b)+ will let the reel driver hold
> one session across the slide loop and skip the ~500 ms
> bring-up cost per slide.

**Actual code behavior** (verified at HEAD):
> The IPC sidecar architecture (Phase 9 / production path) DOES
> hold one EglSession across the slide loop — see
> `hdmi::run_in_egl_session(&card, |session| { ... })` at
> ipc_main.rs:688, whose closure body contains the entire Advance
> loop. The session lifetime spans the whole sidecar run.
> `render_one_frame_to_hdmi` (the function this comment annotates)
> is now used only for CLI diagnostic paths (`render_solid_color`,
> `render_animated_atomic`, etc.). The "slice (b)+" v1-spec-delta
> #5 plan was achieved via the IPC sidecar architecture, not via
> a render_one_frame_to_hdmi refactor.

**Recommended fix**: rewrite as "The IPC sidecar
(`hdmi::run_in_egl_session` closure at ipc_main.rs:688) is the
production path and holds one EglSession across all paints; this
function (`render_one_frame_to_hdmi`) is diagnostic-only and
creates its own session per call by design."

---

### 9. `renderer/src/hdmi.rs:74-80` — "Phase 4.1b ships ONE fragment shader" — actual count is ~25+

**Classification**: B (stale fact)

**Comment text** (verbatim):
> Shader sources: inline raw strings for now. Phase 4.1b ships
>     ONE fragment shader (gradient) so a `shaders/` dir +
>     include_str! is premature. Move to a directory when the
>     count grows past ~3.

**Actual code behavior** (verified at HEAD):
> The shader count is well past 3. Counting `const FS_` definitions
> in `hdmi_logic.rs` (via `git show HEAD:renderer/src/hdmi_logic.rs
> | grep -nE "^pub const FS_"`): FS_GLYPH, FS_GLYPH_OUTLINE, FS_CUT,
> FS_CUT_A, FS_CUT_B, FS_WIPE, FS_IRIS, FS_DISSOLVE, FS_PIXELATE,
> FS_SCANLINE, FS_HALFTONE, FS_GLITCH, FS_SLIDE, FS_PUSH, FS_SCROLL,
> FS_BLINDS, FS_FLIP, FS_MARQUEE, FS_SHUTTER, FS_FADE,
> FS_BRIGHT_GAMMA, FS_BLIT, FS_NV12_TO_RGB, FS_NV12_DMABUF_TO_RGB,
> FS_OVERLAY_BLEND, FS_GRADIENT, FS_PATTERN_STRIPES,
> FS_PATTERN_CHECKER, FS_PATTERN_DOTS, FS_PATTERN_HALFTONE,
> FS_PATTERN_SCANLINES, FS_PATTERN_GRID, FS_PATTERN_RINGS,
> FS_PATTERN_RAYS, FS_PATTERN_BRICKS, FS_PATTERN_CONFETTI. That's
> **36 fragment shaders**, all still inline raw strings. The "Phase
> 4.1b ships ONE" framing AND the "move to a directory when the
> count grows past ~3" recommendation are both stale — the count
> threshold was passed 12× over without action.

**Recommended fix**: re-verify the architectural recommendation
with qarl (move to shaders/ dir + include_str!?) before rewriting
the comment. If keeping inline-strings is deliberate, rewrite as
"Shader sources: inline raw strings (36 today). Moved-to-directory
was the original plan past 3 shaders; inline-strings stayed because
<reason>." Could be tied to a separate dispatch (move shaders/ to
files) if the recommendation is still desired.

---

## Closing summary

**Total findings: 9.**
- **3 B (stale fact)**: #1 (piece 4e verified), #3 (parked for 4w), #9 (ONE shader)
- **6 C (stale plan)**: #2 (slice 4 planned), #4 (qarl eyeballs piece 4e), #5 (slice 4 will route), #6 (slice 4 wires), #7 (slice 4 can decide), #8 (slice (b)+ will let)
- **0 D (stale precedent)**: none surfaced.

**Pattern.** Six of nine findings are stale "slice N will / planned"
references in or near `bake_slide_to_fbo` / `bake_video_slide_to_
current_fbo` / `render_fade_composite` — the docstrings narrate the
slice-rollout sequence rather than the post-rollout contract. Fixing
these as a cluster (rewrite docstrings to focus on the contract,
optionally with parenthetical "shipped in slice N" past-tense
markers) would close findings #2, #3, #5, #6, #7 in one dispatch.

**Worst-drift finding**: #1 (`hdmi_logic.rs:2106` — "to verify in
piece 4e" when piece 4e verified GREEN on 2026-05-14). High severity
because the comment implies an unconfirmed assumption that could
trigger an unnecessary debug rabbit-hole — readers might suspect
BT.601 color is wrong when it's been smoke-verified.

**Out-of-scope follow-ups**:
- Backend Python + UI JS files not audited this pass.
- `hdmi.rs:6603` ("based on layer-union-rect is a Phase-2
  optimization") — borderline; "Phase 2" here refers to phase-2 of
  the scissored-bake work, not the HDMI Phase 2 milestone.
  Judgment call → A (accurate).
- `hdmi.rs:2942` ("SLICE-D SCOPE NOTE: Slice (e) or follow-up adds
  a session-level cache keyed on (from, to, fps_bucket)") — today's
  cache is keyed by slide.id, not (from, to, fps_bucket). The note's
  forecast plan is technically not-yet-shipped, just achieved via a
  different key. Judgment call → A (still describes a plausible
  follow-up cache shape).
- `ipc_main.rs:260` ("TODO(piece 4+): release decoder on cache
  eviction") — valid open TODO, not drift.
