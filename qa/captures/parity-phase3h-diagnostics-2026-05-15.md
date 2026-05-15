# Phase 3h — sampling-stage diagnostics (D1 + D2)

Date: 2026-05-15
Status: REVERTED both probes; findings-only commit.
Prior: cdac365 (Phase 3g + 3f-redux refuted metric-source-mismatch hypothesis).

## TL;DR

Neither D1 nor D2 isolated the dominant cause of the 229/231 max_delta
floor.

- **D1** (force Canvas2D WASM off, fall back to `ctx.fillText`): the
  signature changes RADICALLY — Canvas2D glyphs are 2× wider than
  Rust's (canvas2d stems span 129..1778 = 1649 px vs Rust 555..1362 =
  807 px). Per-stem deltas blow out to ±395 px. Confirms the WASM path
  (fontdue rasterization on Canvas2D side) is **load-bearing** for
  HEAD baseline parity. The pre-Phase-1b ctx.fillText path was much
  worse; do not bisect through it.

- **D2** (Rust glyph texture LINEAR → NEAREST): max_delta floor stays
  at 229/231. Mean_delta drops 0.504 → 0.484 (-4%). Disagreement-pixel
  count drops 4,671 → 4,112 (-12%). Per-stem signature stays roughly
  the same shape: [+2, +1, 0, -1, -1, -2, -3] vs HEAD [+1, +1, 0, 0,
  -1, -2, -3]. GL_LINEAR is a **secondary contributor** (~12% of the
  disagreement pixels) but NOT the dominant cause.

Per dispatch: "If max_delta floor stays at 229/231: sampling-stage
isn't the dominant cause either. Pivot needed."

## Numbers

| Probe                                   | font_inter mean | pixels>100 | max_delta | per-stem signature                     |
|-----------------------------------------|-----------------:|------------:|-----------:|-----------------------------------------|
| HEAD baseline (WASM on, LINEAR)         |           0.504 |       4,671 |        231 | `[+1, +1, +0, +0, -1, -2, -3]`         |
| D1 (WASM off, LINEAR)                   |          21.493 |     193,388 |        231 | `[+395, +320, +183, +50, -187, -271, -377]` |
| D2 (WASM on, NEAREST)                   |           0.484 |       4,112 |        231 | `[+2, +1, +0, -1, -1, -2, -3]`         |

D1's mean_delta is 42× worse than baseline; the entire word's horizontal
extent differs by ~2×. The WASM path is not optional for parity at HEAD.

D2's mean_delta is 4% better than baseline; disagreement-pixel count
12% better. The floor (max_delta) is unchanged. Per-stem signature
shifts by ±1 in a couple of positions but keeps the same fan shape.
GL_LINEAR contributes but isn't dominant.

## What the data is saying

The 229/231 max_delta floor is **rasterization-stage**, NOT sampling-
stage. The 12% of disagreement pixels that GL_NEAREST removes are
sub-pixel-positioning artifacts at glyph edges (where bilinear
interpolation rounds differently than nearest-neighbor). The remaining
88% are pixels where BOTH renderers got the SAME effective_size_px
rasterized glyph but POSITIONED it by ±1-3 pixels relative to each
other.

The ±3-px per-stem drift across "INTER" at fontSizePx=1037 → effective
~213 means each glyph's `drawX` (Canvas2D, accumulated via
`Math.round(advance_width)` after each glyph) and Rust's cursor_x
(accumulated via `m.advance_width.round()` after each glyph) differ.

Both code paths compute `round(advance_width)` per glyph. fontdue 0.9
returns identical advance_widths for the same `(char, size_px)` input.
So why does the accumulated cursor drift?

**Best-guess hypothesis for the next slice:** `predict_alpha_bitmap_dims`
in Rust and `predictTextDims` in JS could compute different
`max_line_w` from the same per-glyph advance_widths if there's an
ordering / initial-offset asymmetry, OR Canvas2D's `paintLayer` doesn't
actually call WASM with the same per-glyph cursor positions as Rust
uses. Specifically: Rust composes glyphs into a SINGLE alpha bitmap
where the cursor advances per-glyph inside `layout_text_to_alpha`,
then GPU samples that ONE bitmap as a quad. Canvas2D calls
`rasterizeText(line, ...)` to get ONE bitmap per LINE and `drawImage`s
that one bitmap. So both paths produce a single composite bitmap and
sample it once.

The ±3 px per-stem drift across 6 stems = up to 6 places where one
side's per-glyph `round()` differs from the other's by 1. That can
ONLY happen if the per-glyph `advance_width(ch, size_px)` differs by
some fraction near `0.5` between the two fontdue invocations, in a way
that compounds asymmetrically.

**Cheap diagnostic for the next slice:** dump the per-glyph
advance_widths from both sides at parity-test time. Single fixture
(font_inter "INTER" @1037 effective ~213). Compare byte-by-byte.

```
Canvas2D side: rasterizeText("INTER", "Inter", 213.x)  -> per-char metrics
Rust side:     for ch in "INTER": font.metrics(ch, 213.x)
```

If the per-glyph advances are byte-identical AND the cumulative cursor
positions are byte-identical, then the drift is sub-glyph-pixel
sampling artifacts. If they differ, **that's the source of the floor**.

## What was reverted

- `ui/src/rasterize.js` (D1 useWasm-off flip) → HEAD.
- `renderer/src/hdmi.rs` (D2 LINEAR→NEAREST at line 1867-68 AND
  5564-5572) → HEAD.
- `renderer/tests/golden/*.png` (re-blessed during D2) → HEAD.
- `qa/captures/floor-diag-font-inter.{png,json}` → HEAD.

`git diff HEAD` should report only this findings doc.

## Verification numbers (post-revert)

- `cargo test --release`: 455 passed (pre-revert state, expected
  unchanged after revert).
- `scripts/render_tests.sh`: 44/45 PASS (the one FAIL is the pre-
  existing fys_08_tile_chaos motion-dependent flake; identical to
  HEAD pre-Phase-3h state).
- HEAD goldens unchanged; Pi binary re-built from HEAD post-revert
  and verified against HEAD goldens.

## Suggested next slice

Per-glyph `advance_width` byte-comparison between
`renderer-wasm`'s WASM (Canvas2D side) and `renderer`'s native Rust
fontdue (Rust side) at the **exact effective_size_px** both paths use
on font_inter. Add a one-shot diagnostic harness to
`scripts/parity/` that emits both arrays + the diff. If they differ,
the source is **fontdue version drift between the two crates** (check
Cargo.toml/Cargo.lock pinning — both should be on identical fontdue
0.9.x patch). If they match, the source is sub-pixel-positioning
inside the quad rasterizer and the next move is harder (Rust's NDC
quad → fragment-shader uv → bilinear sample vs Canvas2D's drawImage
positional rounding).
