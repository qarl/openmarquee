# Motion-through-transitions audit (Phase 4v-3a)

**Date**: 2026-05-16
**HEAD at audit**: 3798efc
**Standing rule**: animated slide layers (kenburns, scroll/marquee, breathe/pulse, motion engine, flock-mesh, etc.) MUST keep moving DURING transitions. Snapshot-and-crossfade is a bug, not a tradeoff.

## TL;DR

Four Rust transition paths + one Canvas2D path. Two of the four Rust paths are broken; two are correct. Canvas2D is correct. The IPC sidecar — which is the primary runtime on the dev Pi (192.168.1.67, `OPENMARQUEE_RENDERER=rust-sidecar`) — uses the buggy IPC path.

| # | Path | File:line | Live motion? | Triggers when |
|---|------|-----------|--------------|---------------|
| 1 | IPC PaintTransition (`paint_and_present_one_transition_frame`) | hdmi.rs:3081 | **NO** | Always, in IPC sidecar mode |
| 2 | Legacy 3-pass (`render_transition_animated_in_session`) | hdmi.rs:4980 | **NO** | Direct-driver mode AND slide is SP/SB-ineligible (pattern bg / outline / non-normal blend / >6 layers per side) |
| 3 | Single-pass (`render_transition_single_pass_in_session`) | hdmi.rs:5682 | YES | Direct-driver mode AND slide fits SP (solid bg, ≤4 layers per side, total ≤4) |
| 4 | Scissored-bake (`render_transition_scissored_bake_in_session`) | hdmi.rs:6080 | YES | Direct-driver mode AND slide fits SB (solid/gradient/image bg, ≤6 layers per side) |
| 5 | Canvas2D inline preview | ui/src/inline-preview.js:255-700 | YES | Always (browser preview) |

## Path-by-path findings

### 1. IPC PaintTransition — BROKEN

`paint_and_present_one_transition_frame` (hdmi.rs:3081) is called from `ipc_main.rs:908` once per IPC `PaintTransition` op. Backend playback loop schedules one Advance per transition frame; each Advance triggers one paint.

The per-Advance bake happens via `make_slide_fbo` (hdmi.rs:4624). The smoking gun is the comment at hdmi.rs:4683-4695:

> v1-spec-delta #2 (slice c-1): FBO bake takes the static snapshot path. Slice (d) — motion through transitions — passes per-frame motion states inside render_transition_animated; this make_slide_fbo path is the initial bake, so None is correct.

And the actual call at hdmi.rs:4696-4707 passes `None` for the 6th `paint_slide` argument (the motion_states slot):

```rust
let paint_result = paint_slide(
    gl, mode_w, mode_h, bg_kind, text_layers,
    None,                       // ← motion_states
    current_unix_seconds(),     // ← wall_clock for auto_mode/clock layers
    glyph_cache, None, tex_cache,
);
```

The 7th arg (`current_unix_seconds()`) advances clock-display text but NOT general motion phase. Animated layers freeze at whatever phase their `motion_states_for_layers` evaluation would produce if it ran (which it doesn't, because None).

The "Slice (d)" plumbing referenced in the comment **does not exist** in hdmi.rs. Grep for any per-frame motion-state pass into the transition bake returns nothing. The aspiration was captured in comments but the implementation never landed.

### 2. Legacy 3-pass — BROKEN

`render_transition_animated_in_session` (hdmi.rs:4980) is the fallback when SP and SB eligibility both fail. Bakes both slides ONCE before the frame loop (hdmi.rs:5073-5084):

```rust
// -- Build slide_a and slide_b FBOs once.
let (fbo_a, tex_a) = unsafe { make_slide_fbo(gl, mode_w_u32, mode_h_u32, &bg_a, &layers_a, None, None)? };
let (fbo_b, tex_b) = unsafe {
    match make_slide_fbo(gl, mode_w_u32, mode_h_u32, &bg_b, &layers_b, None, None) {
        Ok(pair) => pair,
        ...
```

The per-frame loop then runs the transition shader against the static tex_a / tex_b. Same `make_slide_fbo`-with-None bug as path #1, but worse: only one bake, not even a per-Advance rebake to mask anything.

Triggers only when SP+SB both ineligible: pattern bg, outline (text-stroke overlay), non-normal blend mode, >6 layers per side. Operator content with those properties hits this path under direct-driver mode.

### 3. Single-pass — CORRECT

`render_transition_single_pass_in_session` (hdmi.rs:5682). Eligible when both slides have solid backgrounds and total layer count ≤4 + per-slide ≤4. Per-frame work at hdmi.rs:5810-5818:

```rust
let t = (frame as f32 / (total_frames - 1).max(1) as f32).clamp(0.0, 1.0);
let tick_seconds = session.motion_tick_seconds();
let wall_clock_unix = current_unix_seconds();
let states_a = motion_states_for_layers(slide_a.id, &layers_a, tick_seconds);
let states_b = motion_states_for_layers(slide_b.id, &layers_b, tick_seconds);
```

States feed `prepare_layers_for_single_pass` (called at hdmi.rs:5826) which fills per-layer uniforms each frame. Motion advances. ✓

### 4. Scissored-bake — CORRECT (with conditional optimization)

`render_transition_scissored_bake_in_session` (hdmi.rs:6080). Eligible for ≤6 layers per side and solid/gradient/image bg.

Per-frame motion state evaluation at hdmi.rs:6300-6309:

```rust
let states_a = if bake_a_needed {
    motion_states_for_layers(slide_a.id, &layers_a, tick_seconds)
} else { Vec::new() };
let states_b = if bake_b_needed {
    motion_states_for_layers(slide_b.id, &layers_b, tick_seconds)
} else { Vec::new() };
```

The `static_pair` optimization (hdmi.rs:6236-6244) bakes once at frame 0 ONLY when neither slide has any animated layer AND neither has auto_mode:

```rust
let any_animated_a = layers_a.iter().any(|(l, _, _)| parse_motion_kind(&l.motion) != MotionKind::Static);
...
let static_pair = !any_animated_a && !any_animated_b && !any_auto_a && !any_auto_b;
```

So if ANY layer animates, both sides re-bake every frame with fresh motion_states. ✓

### 5. Canvas2D inline preview — CORRECT

`ui/src/inline-preview.js`. The transition driver structure (L255-700):

1. L257: `drawSlot(slot)` paints the FROM slide each RAF tick (advances motion via `elapsed_s = position - slot.startSec` inside `drawTextSlideAnimated` at L825).
2. L268-700: per-transition-kind block paints the TO slide via `drawSlot(timeline[nextIdx])` on top with clipping/blending/translation.

Both `drawSlot` calls happen every RAF tick during the transition window. Both pass through `drawSlot → drawTextSlideAnimated → drawCanvas` with fresh `elapsed_s`. Motion advances on both sides. ✓

The pixelate transition (L365-418) uses temporary canvas snapshots (`fromCanvas` / `toCanvas` at L391-399) for the chunky-pixel resample, but those snapshots are FRESHLY CAPTURED each RAF tick — they're per-frame intermediate buffers, not transition-lifetime caches. Motion still advances.

## Perf wrinkle

The IPC path's per-Advance rebake already costs ~30 ms on vc4 at 1080p (hdmi.rs:3075-3080):

> SLICE-D SCOPE NOTE: the FBO bake happens every call. Slice (e) or follow-up adds a session-level cache keyed on (from, to, fps_bucket) so a transition's per-frame Advance calls don't re-bake the inputs. Today's per-call rebake costs ~30 ms on vc4 at 1080p -- borderline 30 fps; acceptable for v1 demo posture, but flagged for follow-up.

The fix in Phase 4v-3b adds `motion_states_for_layers` evaluation per call. That's CPU-side work (font layout + animation interpolation) — probably <2 ms incremental — but it forces the rebake to actually paint the per-frame composed state, which is the dominant ~30 ms cost. The bake was already happening; we're just changing what it bakes.

**Net expected perf impact of 4v-3b alone**: small. The bake was per-call already; switching motion_states from `None` to `Some(motion_states_for_layers(...))` doesn't add fragment-shader cost, only CPU-side state computation. The 30 ms wall-clock cost stays. **Pi Zero 2 W transitions should still run ~30 fps**, same as before.

**The Slice (e) session-level cache that would amortize the rebake DOES NOT EXIST.** Mentioned in comments at hdmi.rs:3075-3080 and 3106-3116 as future work. Live-fire on the dev Pi should confirm whether transitions still hit 30 fps with the motion fix applied. If they drop, Slice (e) becomes urgent.

The non-IPC SP and SB paths already pay this CPU motion-state cost per frame and hold 30 fps on vc4 (per the §8.3 perf log lines they emit on completion — see e.g. hdmi.rs:5426 and 6052). So precedent suggests the IPC path won't regress materially either.

## Fix shape per path

### Phase 4v-3b (next): IPC PaintTransition only

Minimal viable plumbing for the path that runs on the dev Pi today.

1. Add `motion_states: Option<&[MotionState]>` parameter to `make_slide_fbo` (hdmi.rs:4624). Forward to the inner `paint_slide` call at L4696-4707.
2. In `paint_and_present_one_transition_frame` (hdmi.rs:3081), compute `motion_states_a` and `motion_states_b` mirroring hold-path L1236-1238:

```rust
let tick_seconds = session.motion_tick_seconds();
let states_a = motion_states_for_layers(slide_a.id, &layers_a, tick_seconds);
let states_b = motion_states_for_layers(slide_b.id, &layers_b, tick_seconds);
```

Pass `Some(&states_a)` / `Some(&states_b)` into the two `make_slide_fbo` calls at L3142 and L3168.

3. The plumbing terminates inside `paint_slide` (called by `make_slide_fbo` at L4696-4707), which already accepts a `motion_states` arg in slot 6 — currently `None`. The signature change on `make_slide_fbo` just lets the caller-provided states reach that slot.

4. The legacy `render_transition_animated_in_session` calls `make_slide_fbo(... None, None)` at hdmi.rs:5074. The new signature change forces updating that call site too — pass `None` for motion_states there (preserves the existing buggy-but-known behavior for the legacy fallback; addressed separately).

Estimated diff: 30-50 lines, single commit, single file (hdmi.rs).

### Phase 4w-1 (follow-up): legacy 3-pass

Move bake from before-loop to inside-loop, OR replace the legacy path with calls to SP/SB where eligibility allows, OR add a per-frame rebake step. The path is rare (only triggers for pattern bg / outline / non-normal blend / >6 layers per side), so deferring is acceptable. Likely scope: 50-100 lines.

### Phase 4w-2 (optional, may not be needed): Slice (e) session cache

Only urgent if live-fire 4v-3c shows post-fix transitions drop below 30 fps on Pi Zero 2 W. Implementation already partially sketched in hdmi.rs:3106-3116. Scope: 100-200 lines.

## Recommended 4v-3b scope

Just the IPC fix (path 1 only). Three small changes:

1. `make_slide_fbo` signature: add `motion_states: Option<&[MotionState]>`.
2. `paint_and_present_one_transition_frame`: compute and pass motion_states.
3. `render_transition_animated_in_session` (legacy call site at L5074): pass `None` (no behavior change there; just keeps it compiling).

Subagent reviews. Single commit `4v-3b`. Then QA does 4v-3c live-fire on the debug Pi.

## Known-broken parking lot (carry forward to 4w)

- Legacy 3-pass path bakes once before loop. Affects pattern-bg / outline / non-normal-blend / >6-layer slides under direct-driver mode (NOT under IPC mode, which has its own buggy path being fixed in 4v-3b).
- No session-level rebake cache. ~30 ms wall-clock per Advance on vc4 at 1080p remains. Acceptable for v1 demo.

## 4v-3c live-fire ask

QA to identify a motion-bearing slide pair for the live-fire verify on debug Pi 192.168.1.67. Good candidates: any seeded slide with a `motion=kenburns|scroll|breathe|...` layer, transitioning into another such slide. The pre-fix freeze should be visually obvious; the post-fix continuation should also be obvious. Default reel may or may not exercise this — explicit slide-pair pick beats hoping the default reel hits it.

## References

- Hold-path motion plumbing reference: hdmi.rs:1166 `render_animated_slide_in_session`, especially L1236-1238 (the canonical pattern).
- Motion state helper: `motion_states_for_layers` defined at hdmi.rs:4555 (uses `layer_id_seed(slide_id, i)` — confirms the structural-identity-for-per-layer-seeds rule). Called from hdmi.rs:1238, 5816-5818, 6301-6306; the 4v-3b plumbing adds a fourth call site inside `paint_and_present_one_transition_frame`.
- Canvas2D entry: `ui/src/inline-preview.js:713 drawSlot` → `L812 drawTextSlideAnimated` → `L825 elapsed_s`.
- Subagent-audit miss: the Explore subagent claimed the Rust path was CORRECT on first pass. Direct file read invalidated that. Lesson: explore-style audits can miss "the function does what its comment says it does, not what its name suggests."
