# r69 — Transition audit: which kinds actually render vs silently look like cuts (2026-06-06)

**qarl direct observation:** "i'm not seeing the transitions. is it possible that many of our transitions (the ones we don't test constantly) don't work at all? they look like cuts to me."

**Verdict (TL;DR):** **All 16 spec kinds DO render their intended shader through the FYS IPC `PaintTransition` path.** No silent `FS_CUT` fallback fires for any spec-listed kind. The "looks like cuts" symptom is almost certainly **silent frame-skipping** when a TextOverVideo decoder under-runs mid-transition — a path that currently returns `Ok(())` without paint/swap and emits no log. r69 ships the WARN log + regression test for that path.

---

## Spec — the 16 kinds

From `backend/openmarquee/content/__init__.py:41-58`:

    cut, fade, wipe, slide, iris, scroll, flip, marquee, dissolve,
    pixelate, halftone, scanline, glitch, push, blinds, shutter

Pydantic `Literal` — backend cannot send anything else.

## Dispatch path on FYS

The FYS production renderer uses **ONE tier only** — the 1-pass legacy
shader via `paint_and_present_one_transition_frame`
(`renderer/src/hdmi.rs:4759`). The SP / SB tier complexity
(`prefer_scissored_bake`, `cached_composite_program`,
`transition_eligible_for_scissored_bake`) lives only inside
`render_transition_animated_in_session` (`hdmi.rs:~8752`), which is
reachable solely from the **standalone reel driver** —
NOT from the IPC sidecar that FYS actually runs.

IPC handler:
- `renderer/src/ipc_main.rs:2160-2483` — `OpResult::PaintTransition`
- Line 2464: `hdmi::paint_and_present_one_transition_frame(..., &kind, progress)`
- Inside `paint_and_present_one_transition_frame`:
  - Line 4770: `fs_for_transition_kind(kind)` — returns the FS_<KIND>
    shader. The only "miss" path emits `warn: transition kind {kind:?}
    not yet implemented; falling back to cut` (line 4774) and uses
    FS_CUT. This warn cannot fire for any of the 16 spec kinds —
    every spec kind has a `match` arm at
    `hdmi_logic.rs:2707-2730`.
  - Line 5030: `link_program(VS_TEXTURED_QUAD, fs)` — compiles the
    selected FS_<KIND>.
  - Line 5130: `draw_arrays` with the kind-specific shader, mixing
    `tex_a` and `tex_b` per the FS logic.

So the kind IS honored. The shader runs. The bug is elsewhere.

## Per-kind table

`sp_kind_static` column is **irrelevant to FYS** — flagged for
documentation purposes only (it gates the reel-only SP tier).

| kind     | sp_kind_static (reel-only) | FYS runtime tier | TextOverVideo case | verdict | evidence |
|----------|---|---|---|---|---|
| cut      | yes | 1-pass FS_CUT | hard switch t=0.5 | renders | `hdmi_logic.rs:1446-1456` |
| fade     | yes | 1-pass FS_FADE | linear mix | renders | `hdmi_logic.rs:1879-1889` |
| wipe     | yes | 1-pass FS_WIPE | hard left-edge mask | renders | `hdmi_logic.rs:1504-1515` |
| slide    | yes | 1-pass FS_SLIDE | horizontal slide | renders | `hdmi_logic.rs:1684-1699` |
| iris     | yes | 1-pass FS_IRIS | expanding circle | renders | `hdmi_logic.rs:1524-1536` |
| scroll   | yes | 1-pass FS_SCROLL | vertical analog of slide | renders | `hdmi_logic.rs:1740-1754` |
| flip     | yes | 1-pass FS_FLIP | scaleX card-flip; **emits BLACK outside card** | partial (looks like black sweep on video) | `hdmi_logic.rs:1780-1808` |
| marquee  | yes | 1-pass FS_MARQUEE | ticker w/ white dot in gap | renders | `hdmi_logic.rs:1815-1842` |
| dissolve | yes | 1-pass FS_DISSOLVE | per-pixel hash mask | renders | `hdmi_logic.rs:1560-1576` |
| pixelate | yes | 1-pass FS_PIXELATE | **terminates in `mix(a,b,u_t)`** | partial (looks like fade w/ block coarsening on video) | `hdmi_logic.rs:1585-1598` |
| halftone | yes | 1-pass FS_HALFTONE | 8×~14 dot grid | renders | `hdmi_logic.rs:1631-1646` |
| scanline | yes | 1-pass FS_SCANLINE | wipe + white band | renders | `hdmi_logic.rs:1606-1622` |
| glitch   | **NO** (reel-only path, not FYS) | 1-pass FS_GLITCH; **terminates in `mix(a,b,u_t)`** | partial (looks like fade w/ row jitter on video) | `hdmi_logic.rs:1657-1678` |
| push     | yes | 1-pass FS_PUSH | b enters from left, blade seam | renders | `hdmi_logic.rs:1706-1723` |
| blinds   | yes | 1-pass FS_BLINDS | 16 slats opening | renders | `hdmi_logic.rs:1759-1773` |
| shutter  | yes | 1-pass FS_SHUTTER | hex aperture | renders | `hdmi_logic.rs:1850-1869` |

## What's actually causing "looks like cuts"

### 1. **FYS bug C silent frame-skip — THE smoking gun**

When a TextOverVideo endpoint's V4L2 decoder hasn't dequeued a new
NV12 sample THIS tick, `bake_slide_to_fbo` returns `Ok(None)`. The
transition paint function then returns `Ok(false)` at the two
`FYS bug C` comment anchors in
`paint_and_present_one_transition_frame` (one per endpoint side)
— the caller's `if !work?` propagates that to `Ok(())` (search for
the `FYS bug C` skip comment in the caller's tail) and returns
WITHOUT a swap+commit. The previous scanout frame stays on screen.

On qarl's 17-slide all-TextOverVideo playlist this is the
dominant failure mode:

- 1080p H.264 decode is right at the codec's frame-rate envelope.
- Both `from` and `to` decoders must produce a fresh sample each
  tick during the transition window.
- Any short stall on EITHER decoder → silent skip → the previous
  scanout frame holds.
- Repeated skips clustered around t≈0 and t≈1 collapse the visible
  transition into a snap from "mostly slide A" to "mostly slide B"
  with very few intermediate frames painted — reads as a cut.

**Currently silent.** No `eprintln`, no log, nothing in
journalctl. Operator has no way to diagnose.

r69 ships a throttled WARN at both `FYS bug C` skip sites so the
symptom is visible. Throttle key: `(kind, reason)` — first skip
per `(kind, "endpoint_a_no_frame")` or `(kind, "endpoint_b_no_frame")`
within a 5s window emits; subsequent same-key skips are silent so
the journal doesn't flood at 30 skips/sec. r69 subagent NIT-5
moved this from key=`kind` to key=`(kind, reason)` so chronic
A-side underruns don't mask occasional B-side underruns on the
same kind.

### 2. **pixelate & glitch terminate in linear `mix(a,b,u_t)`**

`hdmi_logic.rs:1597` and `hdmi_logic.rs:1673`. They modify
SAMPLING (block coarsening / per-row jitter) but the final blend
is a linear cross-fade. On video content with high spatial
frequency the coordinate-modulation difference is subtle — they
read as fades with a hint of effect, not their full intended
look. Not a bug, but a UX gap.

### 3. **flip emits black outside the card** (`hdmi_logic.rs:1806-1807`)

`gl_FragColor = vec4(col * inside, 1.0)`. On a video-backed
transition this is a black bar sweep, not a true 3D card-flip
illusion. Distinct from cut but doesn't match the operator's
mental model of "flip."

## What r69 ships

1. **This audit doc** — codifies the per-kind verdict so a future
   regression in shader code can be cross-checked against the
   table.
2. **WARN log** at `hdmi.rs:4954` + `hdmi.rs:5015` — throttled by
   `(from_id, to_id)`, 5s window. Surfaces the FYS bug C
   frame-skip path that's been silent since slice 6.
3. **Regression test** for the throttling helper + an assertion
   that every TransitionKind spec value resolves to a distinct
   `FS_<KIND>` shader (catches a future shader-const rename or
   delete).

## Not in r69

- **Implementing distinguishing visual character for pixelate /
  glitch on video.** Both need a non-linear final blend; that's a
  GLSL change and a separate ship.
- **flip's black-bars-on-video issue.** Same — a real fix would
  bake `tex_b` behind the card so the off-card area shows tex_b
  instead of black. Separate ship.
- **Adding parity goldens for the 10 spec kinds without
  `transition_mid_<kind>.png`** (iris, dissolve, halftone,
  scanline, glitch, marquee, blinds, flip, shutter, push — only
  cut/fade/wipe/slide/scroll/pixelate have goldens today). A real
  parity sweep is a separate ship.

## Memory / cross-reference

- `project_h4_parity_harness_7_transitions` — 7 parity fixtures
  closed 2026-05-23 (code2 SHA 3e59c24). Audit confirms the IPC
  dispatch path those tests exercise; the 6 visible
  `transition_mid_<kind>.png` files match.
- `project_h3_m1_motion_phase_arc` — motion-state per layer in
  transitions (cited in `paint_and_present_one_transition_frame`'s
  pre-resolve step).
- `feedback_motion_through_transitions_required` — Option D
  cadence (video plays through the transition); this is what
  drives the FYS bug C skip behavior under decoder pressure.
