# Phase 3n: Cause B sub-attack 1 — coverage table parity probe

**Date:** 2026-05-15
**Dispatch:** Phase 3m closed sub-cause ambiguity (one mechanism, text-AA
at glyph edges). Phase 3n probes the first candidate location for that
mechanism: fontdue coverage-table generation. Byte-compare
`fontdue::Font::rasterize` output from both crate contexts (renderer +
renderer-wasm) for the "INTER" glyphs at the parity_font_inter
effective size (and 4 controls).
**Probes:** `renderer/examples/coverage_probe.rs`,
`renderer-wasm/examples/coverage_probe.rs`
**Outputs:** `qa/captures/coverage-probe-renderer-2026-05-15.json`,
`qa/captures/coverage-probe-wasm-2026-05-15.json`,
`qa/captures/coverage-probe-diff-2026-05-15.txt`

## Headline

**Coverage tables are BYTE-IDENTICAL between the two crates.**

The two probes emit JSON containing per-glyph FNV-1a-64 hashes,
bitmap dims, advance widths, and head/tail 32-byte samples for each
glyph in "INTER" at size_px ∈ {1037, 297, 216, 100, 24} (25 glyphs
total). `diff coverage-probe-renderer.json coverage-probe-wasm.json`
yields **exactly one line of diff** — the `probe` field that
identifies the source. Every other byte agrees:

```
$ diff coverage-probe-renderer-2026-05-15.json coverage-probe-wasm-2026-05-15.json
2c2
<   "probe": "renderer/examples/coverage_probe.rs",
---
>   "probe": "renderer-wasm/examples/coverage_probe.rs",
```

## Method

Both crates pin `fontdue = "0.9"` → resolves to fontdue 0.9.3 with
identical registry checksum
`2e57e16b3fe8ff4364c0661fdaac543fb38b29ea9bc9c2f45612d90adf931d2b`
(verified in both `Cargo.lock`s). Both call
`Font::from_bytes(bytes, FontSettings::default())` at load time and
plain `font.rasterize(ch, size_px)` (no subpixel-offset variant) at
rasterize time. Coverage probes pass the same `ui/fonts/inter.ttf`
bytes to each.

For each glyph: emit `(width, height, xmin, ymin, advance_width,
bitmap_len, bitmap_fnv1a_64, bitmap_head_32, bitmap_tail_32)`.
FNV-1a-64 over the raw bitmap bytes is sufficient to confirm
byte-equality across 0.9-1.4 MB of total raster output without
bloating the JSON. Inline hash impl (5 lines) — no new deps.

## Per-glyph sample (size_px=297, the parity_font_inter effective size)

| ch | width | height | xmin | ymin | advance_width | bitmap_fnv1a_64    |
|----|-------|--------|------|------|---------------|--------------------|
| I  |    28 |    217 |   26 |    0 |   79.760742   | bd11a0a1d7831de8   |
| N  |   172 |    217 |   26 |    0 |  223.765137   | 96003c9d52d95eb1   |
| T  |   164 |    217 |   14 |    0 |  191.715820   | 71d1658227ccc3a1   |
| E  |   134 |    217 |   26 |    0 |  178.519043   | 7ab765f07ac6a331   |
| R  |   157 |    217 |   26 |    0 |  191.135742   | 465ccc551dca7408   |

Both crates emit these exact rows.

## Decision tree

Per the dispatch:

- ~~If buffers BYTE-IDENTICAL: source is in offset-at-rasterization OR upload-stage.~~ ← **this case**
- If buffers DIFFER at edges but match in body: fontdue config mismatch → 1-line fix.
- If buffers DIFFER everywhere: deeper version drift.

**Cause B is NOT in coverage-table generation.** The 11
hairlines-tier fixtures' max_delta floor (197-231) and the loud-pixel
co-location confirmed in Phase 3m (dbf610f) must come from
downstream of `font.rasterize`:

- **Composite-blit math** (per-glyph placement in the line bitmap),
  OR
- **Upload-stage** (Rust GLES2 texture upload + `box_to_ndc_quad`
  scaling vs Canvas2D `drawImage` + CSS-pixel rounding), OR
- **A latent diff in line-bitmap construction** (PAD handling,
  baseline rounding, `max_ascent / min_descent` math).

Both crates use plain `rasterize()` not `rasterize_subpixel()` — so
"sub-pixel offset at rasterization" is implicitly always (0, 0) on
both sides. That sub-cause is ruled out by inspection (no probe
needed): the rasterize call site in `renderer/src/hdmi_logic.rs:425`
and `renderer-wasm/src/lib.rs:112` both invoke the no-subpixel
variant.

## Targeted Cause B fix scope for next slice

**Sub-attack 2 target: composite-blit math + upload-stage parity.**
Likely cheap to isolate (one or two line-bitmap byte-compare probes
on a single-glyph fixture) and likely a few-line fix. Multi-day only
if the divergence is in the screen-quad scaling (which Phase 3j
already partially addressed in `bde81b6`).

Concrete sub-attack 2 design (for the next dispatch):

1. Build a single-glyph end-to-end probe: rasterize "I" on both
   sides into the per-line composite bitmap (NOT the per-glyph raw
   fontdue output), capture both as PNGs, byte-compare.
2. If byte-identical: divergence is exclusively in upload (texture
   sampling AA vs Canvas2D drawImage AA). Patch is to align texture
   filter mode or pre-rounded coords.
3. If they differ: divergence is in the line-bitmap blit math (PAD,
   baseline, advance rounding). Patch is in `layout_text_to_alpha`
   vs `rasterize_inner`.

## Limitations

- Probe targets one font (Inter). The 11 hairlines-tier fixtures span
  9 different fonts (`Rye`, `Cinzel`, `Pacifico`, `Oswald`, `Inter`,
  plus 6 bg-pattern fixtures using the default Inter label). The
  fontdue determinism guarantee applies font-agnostically, but to be
  rigorous a future probe should also include one other font (e.g.
  Pacifico) as a cross-check.
- Hash comparison only proves byte-equality of the captured fields.
  The full raw bitmap bytes are NOT in the JSON; if the hash agrees
  AND `bitmap_len` agrees AND head_32 agrees AND tail_32 agrees, a
  collision in the middle would be vanishingly unlikely (FNV-1a-64
  is uniform over arbitrary input).
