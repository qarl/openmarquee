# Phase 3o: Cause B sub-attack 2 — composite line-bitmap parity

**Date:** 2026-05-15
**Dispatch:** Phase 3n proved fontdue per-glyph rasterize is byte-
identical across crates. Phase 3o widens one step downstream: are
the COMPOSITE line bitmaps (after PAD, baseline, advance-rounding,
and the per-glyph blit) also byte-identical, or is the line-bitmap
construction where Cause B enters?
**Probes:** `renderer/examples/composite_probe.rs`,
`renderer-wasm/examples/composite_probe.rs`
**Outputs:** `qa/captures/composite-probe-renderer-2026-05-15.json`,
`qa/captures/composite-probe-wasm-2026-05-15.json`,
`qa/captures/composite-probe-diff-2026-05-15.txt`

## Headline

**Composite line bitmaps are BYTE-IDENTICAL across the two crates
for single-glyph "I" at the parity_font_inter effective size (297).**

Both probes produce an 82×219 alpha buffer (17,958 bytes) with
identical FNV-1a-64 hash `37f093bd33ee708a`. The JSONs diff on
exactly one line — the probe identifier.

```
$ diff composite-probe-renderer-2026-05-15.json composite-probe-wasm-2026-05-15.json
2c2
<   "probe": "renderer/examples/composite_probe.rs",
---
>   "probe": "renderer-wasm/examples/composite_probe.rs",
```

The probes embed a base64-encoded copy of the full alpha buffer in
each JSON, so the byte-equality is verifiable end-to-end, not just
by hash.

## Method

Both crates have build-surface constraints that prevented importing
the production blit fns directly:

- `renderer/` exposes only `[[bin]]`, no `[lib]` — an example
  can't `use openmarquee_render::hdmi_logic::*`.
- `renderer-wasm/` has `[lib] crate-type = ["cdylib"]` — no rlib,
  so examples can't link it.

Adding `[lib]` / "rlib" would be a real config change. Instead,
each probe inlines a VERBATIM copy of its crate's blit logic
(documented at the top of each file with source-line references):

- `renderer/examples/composite_probe.rs::layout_single_line_inline`
  — single-line case of `hdmi_logic.rs::layout_text_to_alpha`
- `renderer-wasm/examples/composite_probe.rs::rasterize_inner_inline`
  — single-line case of `lib.rs::rasterize_inner`, with RGBA →
  alpha-only extraction (Color is fixed at white opaque so
  `modulated_a = (cov * 255) / 255 = cov`, i.e. the alpha channel
  equals the coverage byte that the renderer side emits directly)

If either source changes, the probe will silently drift. Retire it
rather than diverge.

## Why this works for byte-equality

Phase 3n (1ed981d) proved the per-glyph fontdue output is byte-
identical. Both blit loops then apply the same:

- `PAD = 1` (renderer-wasm:138, renderer:464)
- `glyph_x = (cursor_x + m.xmin).round() + PAD`
- `glyph_top = PAD + max_ascent − m.ymin − m.height`
  (renderer expresses it as `baseline_y − m.ymin − m.height` where
  `baseline_y = PAD + max_ascent` in the single-line case)
- `cursor_x += m.advance_width.round()`
- `bm_w = line_w + 2*PAD`, `bm_h = (max_ascent − min_descent) + 2*PAD`

Two small format-level differences that do NOT affect alpha output:

- `line_w`: renderer uses `line_advance as u32` (truncate);
  renderer-wasm uses `line_advance.round() as u32` (round). For
  text where every per-step advance is integer-valued f32 (true at
  size 297 for all "INTER" glyphs after `.round()`), the sum is
  integer and both forms give the same u32.
- Output format: renderer writes 1 byte per pixel (grayscale
  alpha); renderer-wasm writes 4 bytes per pixel (RGBA) with the
  alpha channel modulated by the fill color. With color
  `(255,255,255,255)`, the alpha-channel byte equals the raw
  coverage byte — extracting it byte-for-byte yields the same
  buffer as the renderer's grayscale output.

## Decision-tree outcome

Per the dispatch decision tree:

- ~~byte-identical → divergence is in the upload-stage~~ ← **this case**
- ~~differ → divergence is in blit math~~ (PAD, baseline, advance rounding)

**Cause B is in the UPLOAD-STAGE.** Both crates emit the same
alpha buffer; what differs is how that buffer reaches pixels on
screen:

- **Rust (HDMI path):** alpha buffer → GLES2 texture (1-channel
  format) → fragment shader samples with GL_LINEAR filtering →
  composited into the panel framebuffer → KMS scanout. Quad
  placement uses `box_to_ndc_quad` (renderer/src/hdmi_logic.rs;
  Phase 3j made it pad-aware at bde81b6).
- **Canvas2D (WASM path):** alpha buffer → JS reads 12-byte header
  → ImageData created → drawn to canvas via `putImageData` OR a
  temporary canvas + `ctx.drawImage(temp, x, y, w, h)` with
  CSS-pixel coords. Browser-managed bilinear smoothing on scale.

Both AA paths produce sub-pixel coverage at glyph edges. The
197-231 max_delta band from Phase 3l-post and the loud-pixel
co-location on glyph outlines from Phase 3m (dbf610f) are
consistent with a small mismatch in the sub-pixel filter response
at glyph edges.

## Targeted Cause B fix scope for next slice (sub-attack 3)

The upload-stage has multiple candidate sub-causes. The cheapest
isolation is a one-shot end-to-end probe: capture a 1-glyph
fixture rendered through both paths (the parity_tests harness
already does this for the live suite; we'd capture just the "I"
sub-region) and look at the actual pixel-level delta pattern.

Candidate sub-causes ranked by likelihood, given Phase 3m
evidence (loud pixels on glyph outlines, hairlines tier
`max_delta` 197-231):

1. **Quad placement / scaling rounding** — `box_to_ndc_quad`
   (Rust) vs the `drawImage(x, y, w, h)` rect chosen by ui/ →
   sub-pixel offset misalignment → 1-pixel-wide false-AA edge
   on one side of every glyph. Most likely cause; Phase 3j
   partially addressed but didn't fully eliminate.
2. **Texture filter mode** — Rust uses GL_LINEAR; Canvas2D uses
   browser bilinear. Both are mathematically bilinear at
   non-pixel-aligned sample points, but the filter MIDPOINT
   (where t=0.5 lookup happens) can differ by half a pixel at
   the edge. Less likely to produce max_delta=231 (would max
   around 127); ruled in only if not (1).
3. **DPR mismatch** — Playwright runs at DPR=1 by default; Pi
   compositor runs native pixels (1080p panel). If Rust assumes
   DPR=1 and Canvas test runs at DPR=2 (Retina), every
   placement is off by 2×. Easy to check via the
   `parity_tests.sh` config.
4. **WASM 12-byte header parse** — `renderer-wasm/src/lib.rs:155-158`
   prepends 12 bytes (bm_w, bm_h, ascent) to the RGBA buffer; the
   JS reader (ui/src/rasterize.js) must skip exactly 12 bytes to
   reach pixel data. An off-by-N byte shift would translate the
   ImageData horizontally and produce loud-pixel edges parallel to
   the glyph outline. Cheap to verify: read the JS header-parse
   site and confirm offset=12.

Recommended sub-attack 3: one-shot diff visualization on a
single-glyph fixture (`parity_font_inter` already exists with
"INTER" — a 1-char subset would isolate it). Compare loud-pixel
location to the predicted quad edges from `box_to_ndc_quad` vs
`drawImage` rect. If they fall ON the quad edges → cause (1). If
they fall in a uniform band parallel to glyph outlines → cause
(2) or (3).

## Limitations

- Single glyph ("I") at single size (297). The composite-bitmap
  byte-equality should hold for any (text, size) since both
  blits are determinant on the same inputs, but a cross-check
  with "INTER" at size 297 (or any of the other hairlines-tier
  fonts) would tighten the proof.
- The probe inlines the blit logic — if production drifts, the
  probe doesn't catch it. Retire on production change.
