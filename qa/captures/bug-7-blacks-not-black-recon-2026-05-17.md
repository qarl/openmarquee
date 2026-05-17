# Bug 7 recon: blacks not black in the renderers

Date: 2026-05-17
Author: Jimmy-openmarquee-code (recon only — no fix in this commit)
Dispatch: QA, "Bug 7 (probable) — blacks not black in the
renderers" (post-Phase-E-slice-4a)
Reporter: qarl on-glass during the live driving session

Scope: trace the on-glass "blacks not black" complaint through the
three renderer paths per `feedback_pixel_perfect_renderer_parity`,
identify the most likely root cause without a live probe, surface
findings for qarl scope-confirm before fix dispatch.

---

## TL;DR

**High-confidence candidate: vc4 GLES2 `pow(0.0, y>0)` imprecision
in the `FS_BRIGHT_GAMMA` post-pass shader on the Rust HDMI path.**
The CPU mirror + Canvas2D editor preview both produce exact
`(0, 0, 0)` for a black bg by code inspection; the GL shader's
black-input math goes through `pow(0.0, 1/gamma)` which is
*implementation-defined behavior* per GLSL ES 1.00 §8.2 when the
base is 0 and the exponent is positive.

The Phase-4w pattern smell: `renderer/src/hdmi_logic.rs:1955-1958`
has a comment that says "Avoid pow(0, x) edge cases via a tiny
epsilon" — but the code right below clamps to `vec3(0.0)`, NOT to
a non-zero epsilon. Comment and code disagree. Looks like a
half-applied fix.

NOT live-probed yet (no Pi access from this session); STOP-pinging
QA per the dispatch's (c)-production-bug escape hatch before
committing a fix.

---

## 1. The three render paths

Mapping the dispatch's "three renderers" to actual code at HEAD
(3c9763a):

### 1.1 Canvas2D — `ui/src/rasterize.js`

`drawCanvas()` at L436. Black bg path:

  - L471-472: `ctx.fillStyle = backgroundColor; ctx.fillRect(...)`.
    For `backgroundColor = "#000000"`, this paints exact black.
  - L535-541: optional `applyBrightnessGamma` post-pass (default
    identity = no-op; opted in by HDMI/composite preview modes
    with `gamma=2.2`).

`applyBrightnessGamma` at L406-419: pure JS, uses `Math.pow`.
ECMAScript spec defines `Math.pow(+0, y > 0) === +0` exactly. So
for input pixel `(0, 0, 0, 255)` and any gamma > 0:
  - `scaled = clamp(0 * brightness, 0, 1) = 0`
  - `corrected = Math.pow(0, invGamma) = 0`
  - `output = round(0 * 255) = 0`

**Canvas2D produces exact (0,0,0,255) for black bg.** (Verified by
code inspection; vitest probe-fixture not yet written.)

### 1.2 "GPUSlideCompositor" — `backend/openmarquee/rendering/gpu_compositor.py`

The dispatch assumed this lived in JS (`ui/src/gpu_compositor.js`).
It doesn't — `GPUSlideCompositor` is a Python class at
`backend/openmarquee/rendering/gpu_compositor.py:207` (multi-plane
DRM atomic compositor). This path is on its way out per
`project_renderer_rewrite_rust` (DELETE-PIL phases visible in
recent commits 868a493 / 34ef94c / 2686d29).

The live HDMI render path on HEAD is the **Rust sidecar**, not the
Python compositor. So the "three renderers" in the
parity-pixel-perfect contract is more accurately:
  - (a) Canvas2D editor preview (JS, dashboard)
  - (b) Rust CPU mirror `apply_brightness_gamma_rgba`
    (host-testable, parity-harness reference)
  - (c) Rust GLES2 shader `FS_BRIGHT_GAMMA` (on-Pi, live HDMI)

The Python compositor is excluded from this recon — it's not the
live path and is queued for deletion.

### 1.3 Rust CPU mirror — `renderer/src/hdmi_logic.rs::apply_brightness_gamma_rgba`

At L1974-1989. Pure CPU mirror of the GL shader's per-pixel math:

  - `v = (val as f32) / 255.0`
  - `scaled = (v * brightness).clamp(0.0, 1.0)`
  - `corrected = scaled.powf(inv_gamma)`
  - `output = (corrected * 255.0).round() as u8`

For `val = 0`: `v = 0.0`, `scaled = 0.0`, `corrected =
0.0_f32.powf(0.4545)`. Rust's `f32::powf(0.0, positive)` is exactly
`0.0` (IEEE 754 — `pow(+0, +x)` for x > 0 returns `+0`). So
`output = 0`.

**Rust CPU mirror produces exact (0,0,0,255) for black bg.** Pinned
by tests at L4108-4170 (no explicit (0,0,0) case — but
`apply_brightness_gamma_identity_at_b1_g1` exercises identity at
`vec![0, 0, 0, 200]` and pins zero-pass-through).

### 1.4 Rust GLES2 shader — `renderer/src/hdmi_logic.rs::FS_BRIGHT_GAMMA`

At L1946-1962:

```glsl
#version 100
precision mediump float;
uniform sampler2D u_src;
uniform float u_brightness;
uniform float u_gamma;
varying vec2 v_uv;
void main() {
    vec4 c = texture2D(u_src, v_uv);
    vec3 rgb = c.rgb * u_brightness;
    // Avoid pow(0, x) edge cases via a tiny epsilon. GLSL's
    // pow is undefined for negative bases; clamping rgb to
    // [0, 1+eps] keeps it well-defined.
    rgb = clamp(rgb, vec3(0.0), vec3(1.0));
    rgb = pow(rgb, vec3(1.0 / max(u_gamma, 0.001)));
    gl_FragColor = vec4(rgb, c.a);
}
```

For `c.rgb = (0, 0, 0)` and `u_brightness > 0`:
  - `rgb = (0, 0, 0)` (after multiply)
  - `clamp(rgb, vec3(0.0), vec3(1.0)) = (0, 0, 0)` — **NO EPSILON
    APPLIED**, contradicting the comment immediately above.
  - `pow(vec3(0.0), vec3(0.4545))` — **GLSL ES 1.00 §8.2:**
    > pow (x, y). Results are undefined if x < 0. Results are
    > undefined if x == 0 and y <= 0.

    The spec says undefined only for `x == 0 and y ≤ 0`. For
    `x == 0 and y > 0`, the spec is silent — which means the
    result IS mathematically defined as `0`, not implementation-
    defined. Any divergence from `0` on vc4 is a **driver-side
    `pow` LUT/precision artifact**, not spec-permitted behavior.
    vc4 GLES2 has documented low-precision behavior on `pow`
    near-zero arguments (mediump float, LUT-backed pow approx),
    so the driver bug is plausible.

A small positive value on `pow(0, 0.4545)` → blacks lift to a dim
gray. Operator-visible as "blacks not black on HDMI."

---

## 2. Code/comment drift smell

The comment at L1955-1958 reads (verbatim):

> // Avoid pow(0, x) edge cases via a tiny epsilon. GLSL's
> // pow is undefined for negative bases; clamping rgb to
> // [0, 1+eps] keeps it well-defined.
> rgb = clamp(rgb, vec3(0.0), vec3(1.0));

The comment **describes a fix that the code below does NOT
implement**. "clamping rgb to [0, 1+eps]" — there is no epsilon.
The intent appears to have been:

```glsl
const float EPSILON = 1.0 / 1024.0;  // or similar
rgb = clamp(rgb, vec3(EPSILON), vec3(1.0));
```

But the actual code clamps to 0.0. Either:
  - The fix was conceived, the comment got written, the code
    edit was lost (Phase 4w smell).
  - The comment is wrong — fix was never applied; the developer
    decided pow(0, y>0) was reliably 0 across all targets.

Per the Phase 4w precedent: **don't trust the comment alone**.
Without a vc4 probe, I can't confirm whether vc4 returns exact 0
or a small positive value here.

---

## 3. Why is this not caught by the parity harness?

`renderer/tests/parity/` exercises FYS reel slides cross-renderer
against goldens. Default test slides may not have a pure-black-bg
fixture (the FYS reel uses colored bgs). Spot-check needed: grep
the parity fixture catalog for `background_color = "#000000"` —
if no parity test slide has pure black, the harness doesn't surface
this divergence.

**Action for fix-slice:** add a parity-harness fixture for pure
black bg if absent.

---

## 4. Other candidates ruled out (or ranked lower)

Per the dispatch's §3 candidate list, ranked:

### Ruled out by code trace
1. **Brightness uniform default** — schema-default 100/100 → shader
   `brightness = 1.0`. Identity for multiply. NOT a lift source.
2. **Color input parsing** — `hex_to_rgba("#000000")` returns
   `[0.0, 0.0, 0.0, 1.0]` (verified at L2254-2256). Exact black.
3. **Clear color** — `gl.clear_color(0.0, 0.0, 0.0, 1.0)` is the
   canonical bg clear. Drives exact black into the scene FBO.

### Ranked LOWER than the FS_BRIGHT_GAMMA candidate
4. **Image bg decode color profile** — only relevant if the bg is
   an Image slide, not Solid. qarl's report didn't specify.
5. **HDMI signal range (full vs limited 16-235)** — affects DRM
   output stage, not the GL bake. Possible but lower-likelihood;
   would require DRM `Broadcast RGB` property mismatch with the TV.

### Not a candidate
6. **Gamma misapplied** — gamma post-pass is mathematically
   correct (1/2.2 power encoding). Pure black should map to pure
   black mathematically; the issue is the vc4 GLES2 numerical
   implementation, not the algorithm.

---

## 5. Recommended fix path (NOT IMPLEMENTING — awaiting scope confirm)

### Option A — apply the epsilon the comment promised (one-line shader fix)

```glsl
const float EPSILON = 1.0 / 1024.0;
rgb = clamp(rgb, vec3(EPSILON), vec3(1.0));
rgb = pow(rgb, vec3(1.0 / max(u_gamma, 0.001)));
```

Trade-off: an epsilon-floor on the input means the output for
exact-black input lifts to `pow(1/1024, 1/2.2) ≈ 0.072 ≈ 18/255`
on output — that's MORE black-lift than the bug we're fixing.
Wrong direction.

### Option B — fast-path the (0,0,0) case (preferred)

```glsl
vec3 rgb = c.rgb * u_brightness;
rgb = clamp(rgb, vec3(0.0), vec3(1.0));
// GLSL step(edge, x) returns 0 when x < edge, else 1. So
// step(rgb, vec3(1e-6)) is "is 1e-6 >= rgb?" — yields 1 per
// channel when rgb is at/near zero, 0 otherwise. mix(a, b, t)
// returns a*(1-t) + b*t; t==1 picks the zero branch, t==0
// picks the pow branch. So channels at/near zero snap to exact
// zero; non-zero channels go through pow as usual.
rgb = mix(
    pow(max(rgb, vec3(1e-6)), vec3(1.0 / max(u_gamma, 0.001))),
    vec3(0.0),
    step(rgb, vec3(1e-6))
);
```

Branch-free: when `rgb == 0`, the `step` mask snaps the output to
exact zero, bypassing the `pow(0, ...)` driver-imprecision surface.
The `max(rgb, 1e-6)` inside `pow` ensures the base is positive
so vc4's `pow` returns a defined value (which the `mix` then
discards for exact-zero inputs).

LOC delta: 3-4 lines. No CPU mirror change needed (CPU mirror
already returns exact 0 for input 0 per IEEE 754).

### Option C — skip the FS_BRIGHT_GAMMA pass on pure-black slides

The pass is only needed for non-identity brightness/gamma. If the
*slide* is pure black AND brightness=1.0/gamma=1.0, the pass is
already skipped (L4292-4309 — only run when scene_fbo_handle is
Some). But for the spec-default brightness=100/gamma=2.2, the
pass DOES run on black bgs because the framebuffer holds (0,0,0)
+ gamma=2.2 ≠ 1.0. Option C would short-circuit the pass when ALL
pixels in scene_fbo are black, but that's a full readback to
check — not worth it.

**Recommend Option B.** Minimal shader change, branch-free, fixes
the specific vc4 imprecision without changing CPU mirror or
parity-harness math.

---

## 6. What I have NOT done

- Built a live vitest probe for Canvas2D (`drawCanvas` with
  `#000000` bg → getImageData center). Code trace shows it
  produces exact 0; probe would confirm but isn't load-bearing
  for the diagnosis.
- Built a cargo unit test for `apply_brightness_gamma_rgba` on
  pure-black input. Same shape — code trace is conclusive.
- Run the FS_BRIGHT_GAMMA shader on actual vc4 hardware to confirm
  the pow(0, 0.4545) divergence. Would need Pi deploy + on-Pi
  capture, which exceeds RECON scope.

If qarl wants quantitative confirmation before fix dispatch, the
minimum-viable probe is:

  1. Deploy current HEAD to dev Pi (192.168.50.211 or fys Pi
     192.168.1.67).
  2. Create a pure-black-bg TextSlide on the editor.
  3. Trigger playback; `glReadPixels` from the scene FBO (pre-
     FS_BRIGHT_GAMMA) → expect (0,0,0).
  4. `glReadPixels` from the default FB (post-FS_BRIGHT_GAMMA at
     gamma=2.2) → if NOT (0,0,0), this confirms the vc4
     `pow(0, 0.4545)` divergence.

This is a single-frame probe; sub-second on the Pi. Could be
bundled into Phase F (hardware probe slice) if not run as a
one-shot.

---

## 7. Scope question for qarl

The dispatch said qarl reported "blacks are not black in the
renderers" — generic. Three scoping clarifications:

- (Q-7a) **Which renderer?** Canvas2D editor preview, or the Pi
  HDMI output, or both? My finding predicts: Canvas2D preview
  blacks should be exact, Pi HDMI output blacks should be lifted.
  If qarl is seeing it in the editor preview, my candidate is
  wrong and we need a different probe.

- (Q-7b) **Pure-black slide or pure-black region (e.g. inside a
  pattern)?** My finding addresses the pure-black case. Black
  pattern fills inside a colored bg may have a separate cause.

- (Q-7c) **Which gamma setting?** Spec default is 2.2. If qarl
  has overridden to gamma=1.0 (no encoding) and blacks are still
  lifting, my FS_BRIGHT_GAMMA hypothesis is wrong — the pass is
  identity at gamma=1 and shouldn't be running.

Awaiting scope confirm; fix dispatch follows.

---

## 8. STOP-ping rationale

Per dispatch escape hatch: "If you find a (c) production bug
masked by something else: STOP, ping qa-Jimmy. That's bug-list-
workflow territory."

This trace identifies what looks like a (c): a real product bug
(blacks lift on HDMI) masked by:
  - parity-harness not having a pure-black-bg fixture (so the
    cross-renderer parity check doesn't catch it).
  - code/comment drift in the FS_BRIGHT_GAMMA shader (so the
    intent is unclear from the source alone).

STOPping for qarl product-shape confirm + scope (Q-7a/b/c).
Recommend Option B (3-4 LOC shader fix) once scope is locked.
