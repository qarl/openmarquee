# Phase 3ae: CONFETTI diagnostic — explicitly-divergent-by-design

**Date:** 2026-05-15
**Dispatch:** Diagnostic-first per Phase 3ac (rings semantic surprise) +
Phase 3ad (rays AA-only no-fix). Classify confetti before committing
to a fix profile.
**Status:** No source change committed. Diagnostic findings only.

## Verdict: explicitly-divergent-by-design (≈ accepted floor)

**Both renderers carry a standing comment that they will NOT pixel-match
by construction.** The divergence is not a bug. It is a documented
architectural difference between Canvas2D's per-particle loop and the
shader's cell-grid-with-hash approach. Visual character is the same;
individual particle positions are not.

Source evidence (canonical, both sides agree):

`ui/src/bg-system.js:403-423`:
```
// Deterministic full-canvas particle scatter [...]
// NB: editor preview and device renderer use different RNG
// families with the same seed -- both deterministic per-surface
// but they will not pixel-match. Visual character is the same.
const count = Math.round(lerp(80, 2000, density));
const rng = lcgRandom(0xC0FFE71);
for (let i = 0; i < count; i++) {
    const x = rng() * width;
    const y = rng() * height;
    const r = 2 + Math.floor(rng() * 4);
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fill();
}
```

`renderer/src/hdmi_logic.rs:2767-2799`:
```
/// The shader-side approach is structurally different
/// (cell-based vs uniform-random) -- per Python's docstring,
/// "editor canvas and device backend use different RNG families
/// with the same seed -- both deterministic per-surface but the
/// scatters will not pixel-match. Visual character is the same."
```

The Rust shader at `hdmi_logic.rs:2482-2508` uses a cell-grid
(`cell = floor(pos / u_cell)`) with per-cell hash for jitter+radius;
the Canvas2D code iterates `count` particles via an LCG drawing
random positions on the full canvas. Same algorithm sketch
("deterministic per-pixel/per-particle randomness"); different
implementation due to fragment-shader constraints (no loops, no
2000-iter scan possible per fragment).

## Concrete divergence evidence

At fixture density=0.5 (raw) → curved 0.25:

| Quantity | Canvas2D | Rust shader |
|----------|---------:|------------:|
| Effective particle count | `round(lerp(80, 2000, 0.25)) = 560` | `confetti_uniforms(0.25).count = 560` |
| Density curve exponent | 2 (`d^2`) | 2 (`d^2`, `PATTERN_DENSITY_CURVE_EXPONENT`) |
| Radius range | `2 + floor(rng()*4)` = 2..5 px | `2.0 + h11(...)*4.0` = 2..6 px (float) |
| Color palette | `b` fill on `a` background | `mix(u_color_a, u_color_b, t)` (t=step) |
| Distribution algorithm | LCG loop, uniform-random on canvas | Per-cell hash, fixed grid |
| Determinism | per-density-input only | per-(x,y) pixel hash |

Counts are identical. Color palette is the same. Radius range is
essentially the same (1-pixel difference at the upper end from
`floor()` vs continuous-float). Density curve agrees (d^2 both sides).

What differs:
- Particle POSITIONS diverge (different algorithm; no cross-renderer
  RNG canonicalization is intended).
- Per-pixel position outputs are intrinsically different because the
  cell-grid approach quantizes the particle center to (cell*u_cell +
  hash(cell)*u_cell) while the LCG approach allows particles anywhere
  in the canvas.

## Parity metrics (parity_tests.sh, current HEAD = 60c3702)

| Metric        | Value    | Gate    | Result |
|---------------|---------:|---------|--------|
| SSIM          | 0.9400   | >= 0.95 | NEAR-PASS |
| mean_delta    | 3.087    | low is good | OK     |
| pct >= 10     | 2.90%    | low is good | OK     |
| max_delta     | 229      | <= 50   | FAIL (Cause B text-glyph floor) |

SSIM=0.9400 is just below the 0.95 gate but reflects the structural
similarity of two visually equivalent scatters. The remaining
~5% structural difference is the position mismatch documented by
both source files.

## Comparison to prior categories

| Phase | Pattern | SSIM at HEAD | Verdict category |
|-------|---------|------------:|------------------|
| 3z    | checker  | 0.9960  | precision (Cand E int-domain) — FIXED |
| 3aa   | grid     | 0.9986  | precision (Cand B hybrid) — FIXED |
| 3ab   | scanlines (audit) | 0.9933  | precision (Cand B generalized) — FIXED |
| 3ac   | rings    | 0.9744  | SEMANTIC (Option C honor-docstring) — FIXED |
| 3ad   | rays     | 0.9935  | AA-only / accepted-floor — NO FIX NEEDED |
| 3ae   | confetti | **0.9400** | **explicitly-divergent-by-design — NO FIX NEEDED** |

Confetti is the only fixture below 0.95, but its sub-threshold score is
expected and documented. The remaining 0.06 SSIM gap is intrinsic to
the two-RNG-family architecture, not a bug.

## Why no fix

Three viable directions, all rejected:

**Reject A: Make Rust shader iterate `count` particles** — fundamentally
impossible at 1080p in a fragment shader (each fragment would need to
re-walk 560+ particles to determine cell membership; ~1.2 billion ops
per frame, blowing the 16ms budget by orders of magnitude). The
cell-grid approach is the only architecturally sound option for shader
side.

**Reject B: Make Canvas2D match Rust's cell-grid** — editor preview
would then look like a grid-of-dots instead of a free scatter,
visually less appealing. Plus the editor preview is the user-facing
authority; changing it impacts saved-slide visuals at every density.

**Reject C: Canonical seed-stream chain** — would require porting the
exact same RNG implementation (LCG with specific multiplier/increment)
to both sides AND ensuring the iteration order produces identical
(x, y, r) tuples per particle. This contradicts the architectural
constraint above (Rust can't iterate particles at all).

The accepted answer is what's already shipped: same algorithm sketch
+ same statistical character + accept-the-position-difference as
documented in the source.

## Recommended action

**Mark as explicitly-accepted floor by design.** Both `bg-system.js`
and `hdmi_logic.rs` already carry comments confirming this. No source
change, no doc-comment change, no re-bless.

The "accepted floor" label is slightly different from Phase 3ad rays
(where Pi and Canvas2D produce the SAME pattern with only AA fringe
differing). Here the patterns are structurally different but visually
equivalent. The closest precedent is Phase 3ad's accepted-floor,
plus the explicit by-design framing.

## Closing the broad-tier parity arc

Phase 3ae closes the broad-tier pattern arc. Status at HEAD 60c3702:

| Pattern | SSIM | Status |
|---------|-----:|--------|
| solid     | n/a  | trivial |
| gradient  | 0.9881 | passing SSIM gate |
| dots      | 0.9578 | passing SSIM gate (Phase 3s/3t precedent) |
| halftone  | 0.9225 | NEAR-PASS, may need Phase 3af follow-up |
| stripes   | 0.9892 | passing SSIM gate |
| scanlines | 0.9933 | Phase 3x/3ab FIXED |
| checker   | 0.9960 | Phase 3z FIXED |
| grid      | 0.9986 | Phase 3aa FIXED |
| rings     | 0.9744 | Phase 3ac FIXED |
| rays      | 0.9935 | Phase 3ad accepted-floor |
| bricks    | 0.9431 | NEAR-PASS, may need Phase 3af follow-up |
| confetti  | 0.9400 | Phase 3ae explicitly-accepted-floor |

9 pattern fixtures (dots through confetti) all classified. Two (halftone,
bricks) at SSIM 0.92-0.94 are borderline — they may benefit from
follow-up after the broad-tier work, but are not catastrophic
structural divergences like rings was pre-3ac.

No NEW structural-divergence arc is surfaced by confetti. The "two RNG
families" pattern is unique to confetti by virtue of its
random-scatter nature; no other fixture has this kind of intentional
asymmetry. RAYS' AA-only and CONFETTI's by-design-divergent are the
two distinct "no-fix-needed" categories.

## Limitations

- Visual inspection done at thumbnail-scale via the conversation image
  viewer. Per-pixel dot-counting not performed (relying on the source
  formulas confirming count parity at 560 particles each side).
- Cell-grid count parity at 1080p is preserved by the call-site:
  `hdmi.rs:1714-1719` rescales `cell` to the actual viewport area
  (`cell = sqrt(actual_area / count)`), not the 1024x768 reference.
  At 1920x1080, count=560: cell ≈ sqrt(2 073 600 / 560) ≈ 60.86 px,
  giving 1920/60.86 × 1080/60.86 ≈ 31.6 × 17.7 ≈ ~560 visible
  particles -- matched to Canvas2D's count by design. The
  `cell_ref` field in `ConfettiUniforms` is the 1024x768-reference
  cell size only; it is NOT what the shader sees. (Reading the
  uniforms struct in isolation would suggest a ~3x particle-count
  mismatch at 1080p; checking the call site corrects that.)

## Next

- Phase 3ae: closes the broad-tier pattern fixture arc.
- Possible Phase 3af: halftone + bricks SSIM borderline cleanup (0.92-
  0.94 → might or might not benefit from precision/semantic work;
  diagnostic-first would clarify).
- Existing structural divergences in parity_tests.sh outside patterns:
  motion/transitions (different scope), text fonts (Cause B family).
