# Phase 3i — per-glyph advance_width byte-compare

Date: 2026-05-15
Status: VERDICT (b) — fontdue advances byte-identical between the two
crates. Source change: probe scaffolding only (`renderer/examples/
advance_probe.rs`). No renderer source change shipped.
Prior: 90d6739 (Phase 3h ruled out GL_LINEAR as dominant cause).

## TL;DR — verdict (b)

Both `renderer/Cargo.toml` and `renderer-wasm/Cargo.toml` pin `fontdue
= "0.9"`. Both `Cargo.lock` entries resolve to `fontdue 0.9.3` from the
crates.io registry. fontdue's `Font::metrics(ch, size_px)` reduces to
a single deterministic f32 multiply (`advance_width = scale * glyph.
advance_width`) plus floor/ceil to integer for the bbox fields. No
platform-conditional code, no SIMD, no FMA fusion. IEEE-754
round-to-nearest-even is bit-exact across x86_64/aarch64/wasm32, so
calling fontdue from either crate at identical `(ch, size_px)` inputs
yields **byte-identical** output. ui/fonts/inter.ttf is identical
on dev Mac and Pi (MD5 `bff0f6e3b9e2259a28313168a907054f`). No version
drift, no font-bytes drift, no per-target codegen drift to find.

Therefore: at any **single** `size_px` value, both renderers' fontdue
calls produce identical `advance_width` arrays. Per dispatch this is
outcome (b) — pivot to quad-rasterizer / drawImage positional rounding.

## But: the REAL gap is "different size_px"

HEAD's two renderers don't call fontdue at the SAME `size_px`:

- Canvas2D `paintLayer` (rasterize.js) at yScale<1: calls
  `rasterizeText("INTER", "Inter", ~213, ...)` — squished
  effective_size_px derived from `boxH / totalInkExtent`.
- Rust `layout_text_to_alpha`: calls `font.metrics(ch, 1037)` — full
  authored size_px. No squish on the Rust side at HEAD.

So the **byte-identical guarantee** applies at any given `size_px`, but
the two paths PICK different `size_px` values. Phase 3g+3f-redux tried
to make Rust squish to canvas-pixel-dims so both sides would call at
the same `size_px` ≈ 213. That made parity WORSE because (per cdac365)
removing Rust's 5× GPU downscale exposed sub-glyph-pixel positional
differences that the downscale was previously masking.

## The cumulative round-drift mechanism

Per-glyph advances from this probe (`qa/captures/advance-byte-compare-
2026-05-15.json`):

| size_px |   I   |   N   |   T   |   E   |   R   | cumulative |
|--------:|------:|------:|------:|------:|------:|-----------:|
|   1037  |  278  |  781  |  669  |  623  |  667  |       3018 |
|   215.7 |   58  |  163  |  139  |  130  |  139  |        629 |
|   213.0 |   57  |  160  |  137  |  128  |  137  |        619 |
|   200.0 |   54  |  151  |  129  |  120  |  129  |        583 |
|   100.0 |   27  |   75  |   65  |   60  |   64  |        291 |

Cumulative cursor positions:

| size_px |   I  |  IN  |  INT |  INTE | INTER |
|--------:|-----:|-----:|-----:|------:|------:|
|   1037  |  278 | 1059 | 1728 |  2351 |  3018 |
|    213  |   57 |  217 |  354 |   482 |   619 |

Scaled comparison (Rust cursors at 1037 ÷ 4.868 to convert to
"Canvas2D-effective-size" pixels):

| glyph end | Rust@1037 ÷ 4.868 | Canvas2D@213 | diff (Canvas2D px) |
|----------:|------------------:|-------------:|-------------------:|
| I         |             57.10 |           57 |              +0.10 |
| IN        |            217.55 |          217 |              +0.55 |
| INT       |            355.00 |          354 |              +1.00 |
| INTE      |            483.00 |          482 |              +1.00 |
| INTER     |            619.95 |          619 |              +0.95 |

Rust's cumulative cursor drifts +1 Canvas2D-px relative to Canvas2D at
the trailing glyphs. This drift is **invisible inside the Rust bitmap**
(1-px diffs in a 3018-wide bitmap are well below sampling resolution),
but when GPU LINEAR samples that bitmap into a 1728-px-wide screen
quad, the sub-bitmap-pixel boundary positions of glyph edges differ
from Canvas2D's per-glyph drawImage placements by ~1 Canvas2D-px
across the rear half of the word.

Combined with centered text math (`drawX = boxX + (boxW -
totalAdvance) / 2`), this produces the symmetric +1/+1/0/0/-1/-2/-3
fan signature observed at floor_diag's HEAD baseline.

## Why Phase 3f-redux failed despite same-size_px alignment

Phase 3f-redux made Rust call fontdue at the same ~213 size_px
Canvas2D uses. The cumulative-rounding mismatch (Rust@1037 vs
Canvas2D@213) goes away — they SHOULD agree at the Canvas2D-pixel
grid. But the floor got WORSE (5px max drift vs 3px). This means:
once the 5× GPU downscale-as-anti-aliasing is removed, **some OTHER
sub-pixel mismatch becomes visible** — likely the screen-quad NDC
math (Rust's `compute_layer_uv_rect`) vs Canvas2D's `drawImage(rasterized,
drawX, drawY, drawW, drawH)` positional handling.

## What was added (this commit)

- `renderer/examples/advance_probe.rs` — one-shot Rust binary that
  emits per-glyph advance metrics from fontdue 0.9.3 at multiple
  size_px. Reusable for future text-parity work. Build:
  `cargo run --release --example advance_probe -- ui/fonts/inter.ttf`.
- `qa/captures/advance-byte-compare-2026-05-15.json` — probe output for
  font_inter "INTER" at sizes [1037, 215.7, 213, 200, 100].
- This findings doc.

No renderer or UI source changes. `git diff HEAD` reports only those
three files added.

## Verification numbers (unchanged from HEAD)

- `cargo test --release`: 455/455 PASS.
- `scripts/render_tests.sh`: 44/45 PASS (the 1 FAIL is the pre-
  existing fys_08_tile_chaos motion-dependent flake).
- `scripts/parity_tests.sh`: 0/39 PASS at default threshold.
  parity_font_inter mean_delta=0.504, max_delta=231.

## Suggested next slice

The dispatched-for outcome (b) is confirmed: fontdue is not the gap.
The gap is **how each renderer turns a deterministic cumulative
glyph-cursor sequence into screen pixels**. Specifically:

- Canvas2D: `drawImage(bitmapAtSize213, drawX, drawY, drawW, drawH)`
  where `drawX = boxX + (boxW - rasterizedW)/2`, `drawW =
  rasterizedW`, no upscaling — bitmap drawn 1:1 horizontally at
  integer drawX.
- Rust: `compute_layer_uv_rect` computes NDC quad coordinates from
  bitmap bounds + box bounds + halign/valign. Fragment shader samples
  via GL_LINEAR. Box-relative positioning happens in float-NDC space.

Two cheap next-probe options:

1. **Single-glyph isolated probe** — render a single 'X' centered in
   the box on both sides, dump the canvas-X of the glyph's pixel
   centroid. If the centroids differ by sub-pixel amounts, the gap
   is in the centering math (boxX, boxW, drawX rounding), not the
   per-glyph advance compounding.
2. **NDC quad coord dump** — instrument `compute_layer_uv_rect` to
   emit the (ndc_l, ndc_r, ndc_t, ndc_b) values for font_inter, then
   compute what Canvas2D's equivalent drawImage rectangle is (in
   canvas px). Compare. The sub-pixel diff between these two
   rectangles IS the floor.

(1) is cheaper to execute; (2) is more diagnostic. Recommend (2) for
the next dispatch, since the ±3 px drift across stems can't be
explained by centering math alone (it's positional ACROSS the word,
not at the boundary).
