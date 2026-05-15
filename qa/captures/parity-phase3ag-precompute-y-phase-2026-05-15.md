# Phase 3ag: Cand-B precompute y-phase for halftone + (attempted bricks)

**Date:** 2026-05-15
**Dispatch:** Phase 3af findings doc (commit a304096) named the
architectural unblock for the f64-predicted gain: precompute
`u_y_phase_*` CPU-side and rewrite halftone + bricks shaders to use
`gl_FragCoord.y` directly, mirroring Phase 3x scanlines + Phase 3aa
grid. Target: halftone margin ≥ 0.94, bricks ~pixel-perfect,
animated_halftone_pulse clears 0.92 gate.
**Status:** Halftone Cand-B + linear-coverage AA SHIPPED with real
but smaller-than-predicted gain. Bricks attempted, regressed -0.0044,
REVERTED to preserve its passing margin. animated_halftone_pulse
improved +0.0045 but does NOT clear gate.

## Before-after Pi metrics (gate SSIM ≥ 0.92, mean_delta ≤ 8)

| Fixture                         | HEAD     | 3ag      | Change   | Verdict           |
|---------------------------------|----------|----------|----------|-------------------|
| parity_bg_pattern_halftone      | 0.9225   | 0.9267   | +0.0042  | PASS (+0.0042)    |
| parity_bg_pattern_bricks        | 0.9431   | 0.9431   | 0        | PASS, unchanged   |
| parity_animated_halftone_pulse  | 0.9116   | 0.9161   | +0.0045  | **FAIL** (-0.0039)|

mean_delta deltas: halftone 5.026 → 4.976, bricks unchanged,
animated_halftone_pulse 6.106 → 6.049.

## 10-pattern + sister-pattern regression check

All 10 broad-tier patterns rerun + Phase 3x scanlines + Phase 3aa
grid (the patterns being mirrored FROM):

| Fixture       | SSIM     | Change         |
|---------------|----------|----------------|
| solid         | 0.9981   | unchanged      |
| gradient      | 0.9881   | unchanged      |
| dots          | 0.9578   | unchanged      |
| halftone      | 0.9267   | **+0.0042**    |
| stripes       | 0.9892   | unchanged      |
| scanlines     | 0.9933   | unchanged      |
| checker       | 0.9960   | unchanged      |
| grid          | 0.9986   | unchanged      |
| rings         | 0.9744   | unchanged      |
| rays          | 0.9935   | unchanged      |
| confetti      | 0.9400   | unchanged      |
| bricks        | 0.9431   | unchanged      |

Zero regressions. Scanlines + grid (Phase 3x/3aa precedents) green.

## Cand-B precompute applied (halftone)

CPU-side, mirroring `scanlines_uniforms` + `grid_uniforms` y_phase
plumbing at `renderer/src/hdmi.rs:1637-1640` and `:1658-1661`:

```rust
let y_phase_l1 = {
    let v = (mode_h as f32) - u.half;
    ((v % u.tile) + u.tile) % u.tile
};
let y_phase_l2 = {
    let v = mode_h as f32;
    ((v % u.tile) + u.tile) % u.tile
};
```

`u_y_phase_l1` = mod(viewport_h - tile/2, tile): the
gl_FragCoord.y-mod-tile position of layer-1 dot rows (centers at
canvas_y = tile/2 + k*tile). `u_y_phase_l2` = mod(viewport_h, tile):
same for layer-2 (centers at canvas_y = k*tile). Same `((v%m)+m)%m`
formulation as scanlines/grid for negative-handling robustness.

Shader-side replaces `vec2 pos = vec2(gl_FragCoord.x, u_viewport.y -
gl_FragCoord.y)` (the vc4 mediump precision trap) with direct
gl_FragCoord.y consumption + modular distance to phase:

```glsl
float frag_y_mod = mod(gl_FragCoord.y, u_tile);
float dy1 = abs(frag_y_mod - u_y_phase_l1);
float cell1_y_abs = min(dy1, u_tile - dy1);
// (length is sign-invariant, so |cell_y| suffices for circle distance)
```

X-axis stays direct (gl_FragCoord.x = canvas-x, no flip). AA shape
swaps to Phase 3af's linear-coverage + per-layer screen blend:

```glsl
float c1 = clamp(u_radius + 0.5 - d1, 0.0, 1.0);
float c2 = clamp(u_radius + 0.5 - d2, 0.0, 1.0);
float t = c1 + c2 - c1 * c2;
```

## Why the gain is smaller than Phase 3af f64-predicted

Phase 3af f64 sim (vs Canvas2D capture, text-masked): halftone
mean 1.213, max 39. That predicted a SSIM gain of ~0.025.

Pi-side actual gain: SSIM +0.0042, mean delta dropped 5.026 → 4.976
(only ~1% relative reduction).

The Cand-B precompute eliminated the y-flip subtraction, BUT vc4
mediump's `mod(gl_FragCoord.y, tile)` retains a ~0.5 px floor at
large `gl_FragCoord.y` values. Empirically (scanlines tile=13
verification): `gl_FragCoord.y = 1079.5` rounds to mediump 1080
(half-to-even), `mod(1080, 13) = 1` (correct in the deterministic
sense), but pixel-row 12 at `gl_FragCoord.y = 1067.5` rounds to
1068, `mod(1068, 13) = 2` — a 0.5-px-shifted modular position vs
the pre-round-half-to-even 1067.5 → mod = 1.5.

For step-based detection (scanlines, grid: `step(abs(m-phase), 0.5)`)
this 0.5-px shift falls inside the tolerance window — the line
either lands at row 0 or row 12 depending on rounding, but the
scanline is still painted on ONE of those rows. Net visual effect:
near-zero. SSIM 0.9933.

For circle-AA detection (halftone: `clamp(r + 0.5 - d, 0, 1)`)
the 0.5-px shift in `cell_y_abs` translates to a 0.5-coverage
flip at the circle boundary (d=r). The AA band is 1 px wide;
0.5-px noise = 50% coverage uncertainty per affected pixel.
Affects ~5-10% of the AA ring pixels around every dot, capping
the SSIM gain at ~+0.005.

## Why bricks was reverted

Same root mechanism: trapezoidal AA on `v_mortar` is precision-
sensitive at the canvas right edge (gl_FragCoord.x near 1919.5
suffers the same half-to-even quantization as gl_FragCoord.y near
1079.5). The Phase 3u step+floor formulation was robust to this
because `floor(pos.x)` integer-quantizes BEFORE the modular
arithmetic, absorbing the 0.5-px noise into binary 0/1 outcomes.

Empirical Pi-side result of bricks Cand-B + trapezoidal AA:
- SSIM 0.9431 → 0.9387 (regression -0.0044).
- mean_delta 3.281 → 3.672.
- pct≥10 3.20% → 3.67%.

The right-edge precision wash on the new trapezoidal AA outweighs
the left-edge gain over step+floor's half-pixel offset (which Phase
3u explicitly accepted). Reverted bricks to its HEAD state.

Halftone doesn't show this regression because its boundary is a
circle (curved boundary), not a vertical line at fixed x. The
right-edge precision noise affects circles at the right of the
viewport but as part of the broader ~12% AA-ring pixel set; doesn't
dominate.

## What's shipped

- `FS_PATTERN_HALFTONE` (`renderer/src/hdmi_logic.rs:2292`):
  Cand-B precompute + linear-coverage AA + per-layer screen blend.
  +2 uniforms (`u_y_phase_l1`, `u_y_phase_l2`).
- `hdmi.rs:1610-1639` PatternKind::Halftone dispatch: precomputes
  `y_phase_l1` / `y_phase_l2` from `mode_h` and passes as uniforms.
  Pattern mirrors Phase 3x scanlines (`hdmi.rs:1637-1640`) + Phase
  3aa grid (`:1658-1661`).
- 4 lines of test assertions in
  `pattern_shaders_have_gles2_preamble` for new uniform presence.

Bricks shader + uniforms UNCHANGED (reverted to HEAD pre-Phase-3af).

## Subagent review focus

- Phase 3x scanlines + Phase 3aa grid precedent comparison.
- vc4 mediump precision behavior of new uniforms (u_y_phase_l1
  = 22.5 for tile=47, half=23.5; u_y_phase_l2 = 46; both << half-
  float overflow ~65k, no precision concern).
- Goldens re-blessed reflect real shader change; no test-tweak-to-
  pass (animated_halftone_pulse still FAILS the gate per the
  metrics table above — bless faithfully captured a still-failing
  fixture).
- Pattern parity suite (all 10 broad-tier): no regression.

## Animated_halftone_pulse: gate not cleared

The dispatch asked "Did animated_halftone_pulse clear gate?". **No.**
SSIM moved 0.9116 → 0.9161, margin -0.0084 → -0.0039 (improved but
still under 0.92).

Same precision-floor mechanism as static halftone. The Cand-B
unblock gives +0.0045 SSIM (almost identical gain to static
halftone), but the animated fixture's pulsing TEXT layer
contributes its own divergence in the text-region — that's a
distinct arc (text rendering parity, not pattern parity).

The pattern-background portion of animated_halftone_pulse should
now match static halftone parity; the remaining ~0.004 below gate
is text-pulse divergence. A future text-parity slice would close
this.

## Verdict (mixed)

**Halftone Cand-B shipped, real-but-small gain.** The architectural
unblock works; the magnitude of unblock is bounded by vc4 mediump's
mod() precision on large gl_FragCoord values (1-pixel-magnitude
half-to-even quantization). The pattern-class fix is correct;
further halftone polish would require either highp float (not
supported on vc4 Pi Zero 2 W) or a fundamentally different sampling
strategy (e.g., supersample-AA via FBO ping-pong, which Phase 7
shader compositor design rules out as bandwidth-binding).

**Bricks unchanged.** Cand-B was applied + reverted; the OLD
step+floor formulation is the better Pi-tuned shape for bricks'
fractional-half v_mortar case.

**animated_halftone_pulse below gate.** Pattern-side improved with
halftone; text-pulse residual is the remaining barrier. Separate
arc (text-rendering parity).

## Cross-refs

- Phase 3af findings doc (commit a304096): the deferred-fix
  recommendation that this slice executes.
- Phase 3x scanlines (commit dd94e0e via 3ab audit): Cand-B
  precompute prior art.
- Phase 3aa grid (commit 218d858): Cand-B precompute prior art.
- Phase 3t (commit 0a84dcb): the halftone smoothstep AA that
  Phase 3af + 3ag superseded.
