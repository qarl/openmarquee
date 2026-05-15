# Phase 3b Capture-Pipeline Diagnostic — 2026-05-14

## Headline verdict

The 229-231 max_delta ceiling is **real renderer divergence**, NOT a capture-pipeline artifact. Hypothesis (iii) refuted. Two distinct downstream causes identified.

## Raw data

Script: `scripts/parity/pipeline_diag.py`. Fixture: text_static (multiline_wrap, UUID `…00000002`).

Cross-comparisons (per-channel max-delta R/G/B/A; pixels-with-any-RGB-diff out of 2,073,600 total):

| pair | RGB max | mean | pixels-different |
|---|---|---|---|
| **A vs B** canvas2d-suite vs canvas2d-diag | **0** | 0.000 | 0 (0.00%) |
| **C vs D** rust-fresh vs rust-golden | **0** | 0.000 | 0 (0.00%) |
| **A vs C** canvas2d vs rust-fresh | 229 | 10.666 | 106,900 (5.16%) |
| **A vs D** canvas2d vs rust-golden | 229 | 10.666 | 106,900 (5.16%) |

## What this proves

- **A vs B = 0** → Playwright + parity-harness capture pipeline is deterministic. Two independent driver scripts produce byte-identical PNGs of the same fixture.
- **C vs D = 0** → Pi-side `--capture-slide` is deterministic AND the checked-in golden is current. The renderer/tests/golden/multiline_wrap.png matches what Rust renders today.
- **A vs C = A vs D = 229** → Canvas2D-WASM and Rust diverge by 229 max regardless of which Rust reference we use. The divergence is in the rendering, not in the comparison.

## Localized cause for text_static

**Rust does NOT word-wrap.** `renderer/src/hdmi_logic.rs::split_text_into_lines` (line 288) only splits on `\n` / `\r` / `\r\n`. It does not break long text at word boundaries when text width exceeds box width.

**Canvas2D DOES word-wrap.** `ui/src/rasterize.js::wrapTextToWidth` uses `ctx.measureText` to break at word boundaries when the line width exceeds boxW.

For text_static the fixture text is one long paragraph: *"the quick brown fox jumps over the lazy dog while running through the meadow on a sunny afternoon"*. Canvas2D wraps it into multiple stacked lines. Rust renders it as ONE very wide line, then the quad placement GL_LINEAR-downscales the wide bitmap to fit the box horizontally. The two outputs are **fundamentally different layouts** — that's where the 229 ceiling comes from, and why 5.16% of pixels (~107K) differ.

**Spatial validation of the wrap hypothesis**: subagent review checked the diff distribution: all 106,900 diff pixels are inside the layer's text box (x=192..768, y=216..864). Mean delta within the diff mask is **206.89** (near-saturated, *not* the 10-20 you'd see from AA edge jitter). Diff rows span y=313..757 — a 444-row vertical band consistent with stacked wrapped lines vs one wide downscaled bitmap, not with sub-pixel AA noise.

**Exhaustive Rust-side wrap check**: `git grep` across `renderer/src/` finds NO width-based break logic anywhere; `split_text_into_lines` (hdmi_logic.rs:288) is the only line-splitter and it only splits on `\n` / `\r`. `layout_text_to_alpha`'s doc comment at line 180 confirms: "No wrapping, no clipping." So the wrap divergence is total, not partial.

## Second cause (font_xxx fixtures)

Font fixtures use short single-word text (e.g. `text="UNIFRAKTURCOOK"`), so wrap doesn't apply. They still show max_delta 229-231. That's a separate divergence: at `font_size_pct=60` on a 1728px-wide box, the natural rasterized bitmap is ~8400px wide. Canvas2D drawImage with bilinear and Rust FS_GLYPH with GL_LINEAR both downscale to box width, but they're not bit-identical operations at extreme downscale ratios. Phase 3c candidate: rasterize at squished pixel-height in WASM (fontdue at the actual canvas pixel-height) eliminates the post-rasterization resample on both axes.

## Phase 3c options surfaced

Two distinct fixes are now visible:

1. **Word-wrap alignment** — either (a) disable JS wrap so Canvas2D produces the same "one wide line, downscaled" output Rust does, or (b) add word-wrap to Rust so both renderers produce the same multi-line layout. (a) is much smaller. The editor preview would change visually (long text becomes a thin horizontal strip), so qarl-direct on whether the editor's word-wrap is required.

2. **Squished-size rasterization** — pass yScale (or the squished pixel-height directly) into rasterize_text_named so fontdue produces a bitmap at the canvas pixel-height, no post-rasterization downscale. Eliminates the resample-divergence cause AND fixes Phase 3a's UnifrakturCook regression as a side effect.

The two fixes are independent. (2) likely lands first (no UX change). (1) needs operator-side discussion.
