---
date: 2026-05-25
type: scope
surface: renderer (Rust SP shader framework)
---

# "Glitch SP port" — scope as of 2026-05-25

The deferred-queue label "glitch SP port (Canvas2D → Rust)" describes the work
in the wrong direction. After reading both sides, the actual gap is
**Rust-internal**: glitch already has a standalone `FS_GLITCH` shader and runs
fine via the legacy 3-pass path, but it is NOT in the SP-portable set used by
the single-pass shader generator. The "port" is integrating glitch into the
SP framework — not a Canvas2D ↔ Rust port (both already implement glitch).

## TL;DR

- **What "SP" means:** Single-Pass — the renderer's fastest transition tier,
  one fragment shader composing both slides' bg + N text layers + per-kind
  mix in ONE pass. Replaces the legacy bake-A + bake-B + composite 3-pass
  structure (legacy ~22 fps @ 1080p).
- **Canvas2D state:** Glitch is **already implemented** in
  `ui/src/inline-preview.js:521-579` and listed in `ANIMATED_TRANSITIONS` at
  line 69. ~60 LOC mirroring `playback.py::_glitch`. Uses
  `Math.random()` (non-deterministic by design — see Q4 below).
- **Rust state:** `FS_GLITCH` (standalone shader) exists at
  `renderer/src/hdmi_logic.rs:1662-1684` and is dispatched by
  `fs_for_transition_kind` at line 2723. But glitch is **excluded** from the
  SP-portable set: `is_transition_kind_single_pass` (line ~2030),
  `sp_kind_static` (line 2053), and `fs_transition_sp_source` all return
  None for glitch. Comment at line 2371: *"Glitch isn't ported to SP yet
  (qarl-deferred); the gate is here for forward compat with the standalone
  FS_GLITCH which still uses the sin-hash idiom."* — but glitch's hash math
  was the original blocker, and `kind_needs_highp("glitch") = true` plus
  `kind_needs_hash("glitch") = true` are **already wired** for the future port.
- **Gap:** Adding a `"glitch" => { ... }` arm to `push_main_body` (~25 LOC
  matching FS_GLITCH's main body) plus listing in `sp_kind_static` and
  `is_transition_kind_single_pass`.
- **Effort:** Small for SP-tier alone. Medium if we also want the SB-tier
  port (needed for text-bearing glitch — see "Strategic note" below).
- **Recommendation:** Defer until we have usage data on bg-only vs
  text-bearing glitch transitions. Most of the perf win lands only when
  BOTH SP and SB get glitch; SP-tier alone helps only the bg-only subset.

## Q1: What is "glitch SP"? What does the shader do?

The standalone `FS_GLITCH` at `hdmi_logic.rs:1662-1684`:

- `precision highp float;` — vc4's mediump (~10-bit mantissa) collapses the
  `fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453)` hash. Comment at
  line 1660-1661 documents the same reason as `FS_DISSOLVE`'s highp.
- Per-row x-jitter:
  `jitter = (_hash(vec2(row, frame_seed)) - 0.5) * 0.1 * u_t`, where
  `row = floor(v_uv.y * 1080.0)` and
  `frame_seed = floor(u_t * 30.0)`. The `* 30.0` quantizes u_t into ~30
  buckets so the jitter holds for ~33ms (one bucket at 30Hz) rather than
  changing every frame — what makes "glitchy" read as glitchy rather than
  white noise.
- Linear cross-fade: `col = mix(a, b, u_t)`.
- Tear rows: every ~18th row (`tear_row = floor(v_uv.y * 60.0)`) gets a
  `step(0.95, _hash(...))` test (~5% chance) and is recolored toward cyan
  (`vec3(0.0, 1.0, 1.0)`) at `tear * 0.5 * u_t` strength.

"SP" = **Single-Pass**. The renderer has three transition tiers
(`hdmi_logic.rs:2132-2156`):

1. **SinglePass (SP):** One FS composes both slides' bg + every text layer +
   the per-kind mix. Specialized by `(kind, n_a, n_b)`.
2. **ScissoredBake (SB):** Both slides baked into a 2048×2048 atlas (split
   vertically), then composited.
3. **Legacy 3-pass (fallback):** bake_a, bake_b, composite. The home of
   `FS_GLITCH` today.

The SP-portable set (`sp_kind_static`, line 2053-2072) currently contains 15
kinds: cut, fade, wipe, iris, dissolve, scanline, halftone, blinds, shutter,
slide, push, scroll, flip, marquee, pixelate. Glitch is **not** in it.

## Q2: What's the Canvas2D state?

Glitch is fully implemented on Canvas2D at `ui/src/inline-preview.js:521-579`:

- Listed in `ANIMATED_TRANSITIONS` at line 69 (so the inline preview
  scrubber doesn't fast-skip it).
- Capture from-slot via `getImageData`, draw to-slot, capture to-slot,
  build composite via `createImageData`.
- Per-row x-shift via `Math.random() * (2 * maxJitter + 1) - maxJitter`,
  where `maxJitter = max(1, floor(w / 10))`. Uses `np.roll`-equivalent
  wrap-around: `((x - shift) % w + w) % w`.
- Linear blend: `out[dst] = from[from] * (1 - progress) + to[to] * progress`.
- Tear rows: `nTears = max(1, floor(h / 20))` random rows overwritten
  with pure cyan.

Comment at lines 524-528 documents the design intent: *"Per-frame
randomness (jitter + tear-row positions regenerated each frame, NOT cached
on the slot like dissolve's thresholds) is what makes the breakage read as
alive — a static glitch reads as intentional, an animated glitch reads as
broken."* The Math.random() use is **deliberate**, not a port oversight.

## Q3: What's the gap?

The work is option (c) "something else" from the dispatch's framing —
specifically:

**(c) Rust-internal port of glitch from legacy 3-pass to SP framework.**

Required code changes (no implementation in this scope doc, just shape):

1. `is_transition_kind_single_pass` (`hdmi_logic.rs:~2030`): add
   `| "glitch"` to the match arm.
2. `sp_kind_static` (line 2053): add `"glitch" => "glitch"` arm.
3. `push_main_body` (line 2452): add a `"glitch" => { ... }` arm. Shape:
   ```
   "glitch" => {
       s.push_str("    float row = floor(v_uv.y * 1080.0);\n");
       s.push_str("    float frame_seed = floor(u_t * 30.0);\n");
       s.push_str("    float jitter = (_hash(vec2(row, frame_seed)) - 0.5) * 0.1 * u_t;\n");
       s.push_str("    vec2 sample_uv = vec2(v_uv.x + jitter, v_uv.y);\n");
       s.push_str("    vec3 ca = u_a_bg;\n");
       push_compose_chain(s, "u_a", "ca", n_a, "sample_uv");
       s.push_str("    vec3 cb = u_b_bg;\n");
       push_compose_chain(s, "u_b", "cb", n_b, "sample_uv");
       s.push_str("    vec3 col = mix(ca, cb, u_t);\n");
       s.push_str("    float tear_row = floor(v_uv.y * 60.0);\n");
       s.push_str("    float tear = step(0.95, _hash(vec2(tear_row, frame_seed + 1.0)));\n");
       s.push_str("    col = mix(col, vec3(0.0, 1.0, 1.0), tear * 0.5 * u_t);\n");
       s.push_str("    gl_FragColor = vec4(col, 1.0);\n");
   }
   ```
   The highp + `SP_HASH_HELPER` are already pre-gated by
   `kind_needs_highp("glitch") = true` and `kind_needs_hash("glitch") = true`
   (lines 2368-2389).
4. Test updates:
   - `is_transition_kind_single_pass` test (`hdmi_logic.rs:7025`): flip
     `assert!(!is_transition_kind_single_pass("glitch"))` → `assert!`.
   - `sp_kind_static` test (line 7060): change `Some("glitch")`.
   - `fs_transition_sp_source` test (line 7007): change
     `is_none()` → `is_some()` and assert the shader contains
     `"0.1 * u_t"`, `"u_t * 30.0"`, `"vec3(0.0, 1.0, 1.0)"`,
     `"step(0.95"`.
   - `classify_prewarm_pair` tests (lines 9636, 9730): glitch now routes to
     SinglePass (for n_a=n_b=0) or ScissoredBake-gated text path —
     but see strategic note below: the text path **also** needs SB support
     to escape legacy 3-pass.

## Q4: Pixel-parity bar — does Canvas2D need to match Rust?

There is **no** explicit `feedback_pixel_perfect_renderer_parity` memory in
the auto-memory store (I checked). The closest discipline is the **H4
parity-harness** (`project_h4_parity_harness_7_transitions.md`), which
locked 7 deterministic transitions to pixel-equivalent goldens. **Glitch
is not in that set** — and shouldn't be, for two reasons:

1. **Canvas2D's glitch is non-deterministic by design** (uses
   `Math.random()` rather than a seeded hash). The inline-preview comment
   explicitly justifies this. A goldens approach would need to be
   distribution-based ("looks glitchy") rather than per-pixel.
2. **Rust's FS_GLITCH IS deterministic** given `(row, floor(u_t * 30))`,
   but only at the device renderer's 1080p sampling. Cross-DPR pixel-equal
   between Canvas2D and Rust is not currently achievable for any transition
   that uses `v_uv.y * 1080.0` style screen-space row math (the inline
   preview is typically <1080p tall).

So the gap-question "does Canvas2D playback of a glitch-transition slide
today do what?" → **it renders glitch correctly** (per the design intent),
just stochastically. It does NOT fall back to cut and does NOT throw. The
visual character matches Rust (both look "broken in the same way"); the
exact pixels do not.

## Q5: Effort estimate

**Small** for SP-tier alone:
- ~5 lines added across `is_transition_kind_single_pass` + `sp_kind_static`.
- ~25 LOC for `push_main_body` glitch arm (port of FS_GLITCH main body).
- ~4 test updates (mostly flipping existing negative assertions to positive,
  plus one shader-content assertion test).
- No new files, no API changes, no test-fixture additions (per
  `feedback_test_commits_need_runtime_verify`: this would be test-mutations
  on the existing pure-logic test surface, which is runtime-cheap and
  static-reviewable; not the JSDOM-env-mock hazard zone).

**Medium** for SP + SB (full escape from legacy 3-pass):
- All of the above, plus the SB composite shader for glitch (separate
  match arm in the scissored-bake dispatch path; would need source-read
  of `paint_slide_with_viewport` to map the glitch math onto the atlas
  region's UV space).
- SB's blocker is the same gate as SP's: glitch isn't in the
  "SP-portable set" check at the top of
  `transition_eligible_for_scissored_bake_logic` (line 2282-2284). The
  comment at 2269-2270 confirms: *"kind outside the SP-portable set (the
  composite shader dispatch table is shared with SP)."* So adding glitch to
  the shared portable set lets BOTH SP and SB consider it — but each needs
  its own per-kind composite arm.

**Large** if we also want a parity-harness golden for glitch:
- Out of scope. Glitch is stochastic by design on the inline preview,
  and the parity discipline so far has been "deterministic transitions
  only" (per H4). Not recommended.

## Q6: Recommended next step

**Defer**, with a planned follow-on dispatch. Reasoning:

1. **SP-tier alone is half the win.** The SP-tier eligibility gate is now
   bg-only (`transition_eligible_for_single_pass_logic` at line 2248
   rejects any non-empty layer_props post-SDF-B.3). So an SP-only glitch
   port helps **only** bg-only glitch transitions. Text-bearing glitch (the
   classic "glitchy broken sign with text") still falls to legacy 3-pass
   until SB also gets the port.

2. **No usage data yet.** Before committing dev time, we want to know:
   what fraction of real operator glitch-transitions are bg-only vs
   text-bearing? If text-bearing dominates (likely — glitch's aesthetic
   pairs naturally with text), SP-only port has low real-world impact.

3. **The standalone path works.** Legacy 3-pass glitch is not broken; it's
   just slower than SP would be. No operator complaints in the queue point
   at glitch perf specifically (the legacy 22 fps quote at line 2302 is
   for the previous-architecture worst case, not a current measurement of
   FS_GLITCH).

4. **Scope-cleanest follow-on shape:** when ready, dispatch as one of:
   - **"glitch SP-tier port (bg-only)"** — Small, well-defined, mirrors
     the dissolve-SP arc precedent (P3 2026-05-09); no text-bearing
     benefit. ~Single-commit scope.
   - **"glitch SP + SB tier port (full)"** — Medium, requires SB
     composite-shader expansion. Two-commit scope (SP commit, SB commit)
     for surgical-revert hygiene per
     `feedback_test_commits_need_runtime_verify`'s isolation guidance.

**Suggested dispatch text (when ready):**
> Port glitch to SP tier. Shape: add `"glitch"` to
> `is_transition_kind_single_pass` + `sp_kind_static`; add `"glitch"` arm to
> `push_main_body` mirroring FS_GLITCH's main body but composing through
> `apply_layer` at the jittered sample_uv. Update 4 tests (flip negative
> assertions to positive; add SP-emit content assertion). Single commit.
> No Canvas2D changes — Canvas2D already implements glitch correctly per
> the per-frame-random design intent. Defer SB port to a follow-on if
> bench data shows text-bearing glitch is a hot path.

## Files referenced

- `renderer/src/hdmi_logic.rs`:
  - 1654-1684 (FS_GLITCH standalone)
  - 2030-2045 (is_transition_kind_single_pass)
  - 2053-2072 (sp_kind_static)
  - 2132-2156 (PrewarmTier classification)
  - 2210-2252 (transition_eligible_for_single_pass_logic)
  - 2277-2297 (transition_eligible_for_scissored_bake_logic)
  - 2320-2354 (fs_transition_sp_source generator)
  - 2368-2389 (kind_needs_highp / kind_needs_hash — already glitch-aware)
  - 2443-2701 (push_main_body — host of the new arm)
  - 2723 (fs_for_transition_kind dispatch — standalone path)
  - 6667-6688 (fs_glitch_uses_highp_precision test)
  - 7007, 7025, 7060, 7078 (existing glitch SP-related test assertions)
  - 9636, 9730 (classify_prewarm_pair glitch tests)
- `ui/src/inline-preview.js:66-70` (ANIMATED_TRANSITIONS), `521-579` (Canvas2D glitch impl)

## Calibration

The dispatch's "Canvas2D → Rust" label is the only material concern flagged
here. Both sides already have glitch; the inline preview's stochastic
choice is documented design intent, not parity drift. The real
deferred-queue item — Rust SP-tier port — is correctly classified as
deferred work, with the strategic question being whether to package it
SP-only or SP+SB based on usage data we don't have yet.
