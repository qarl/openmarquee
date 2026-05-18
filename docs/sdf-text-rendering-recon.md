# SDF Text Rendering + Emoji — Recon

*Recon doc, 2026-05-17. Authored at the close of the DELETE-PIL arc, before the SDF impl arc dispatches. Scope: lay out the implementation surface, identify load-bearing measurement questions, and propose a sliced impl plan that QA + qarl can dispatch against.*

The pain this arc fixes: text rendering in the Rust renderer rasterizes each glyph at the requested font size into a per-layer alpha bitmap (no shared atlas). The bitmap is hard-capped at 2048×2048 to fit vc4's `GL_MAX_TEXTURE_SIZE`, and large slides (1000+ px text on 1080p) hit `clamp_size_px_to_bitmap_cap()` and silently shrink — the "font-clamp bug." Plus: zero emoji support in Rust; the browser + the (Python-side, seed-only) raster paths both render emoji via Noto Color Emoji fallback, and the Rust path does not.

The arc end-state: glyphs rasterized once at a canonical SDF cell size into a shared atlas, sampled at arbitrary on-screen scale via a threshold fragment shader. Emoji renders via a parallel color-bitmap atlas in the same composite pass.

---

## 1. Current text path inventory

**Rust renderer** (production hot path; `renderer/src/hdmi.rs` + `renderer/src/hdmi_logic.rs`):

- Per-layer alpha bitmap, **not** a shared atlas. `layout_text_to_alpha()` (hdmi_logic.rs:448) calls `font.rasterize(ch, effective_size_px)` and packs glyphs into the layer's bitmap.
- Cached at `(text, size_px, max_width_px)` granularity in `CachedGlyph` — re-rasterizes on size or text change.
- Bitmap cap: `MAX_RASTERIZED_BITMAP_DIM = 2048` (hdmi_logic.rs:208). `clamp_size_px_to_bitmap_cap()` (hdmi_logic.rs:413–446) shrinks `size_px` downward to fit + warns operators (hdmi_logic.rs:467).
- Shared compositor atlas: `ATLAS_FBO_W = ATLAS_FBO_H = 2048` (hdmi_logic.rs:1214–1215). This is the **scene FBO** used for transitions, not a glyph pool — text is baked into the scene FBO in the pre-transition phase.
- Glyph fragment shader: `FS_GLYPH` (hdmi_logic.rs:663–674) — single-channel LUMINANCE lookup, `float a = texture2D(u_atlas, v_uv).r;`, premultiplied output. `FS_GLYPH_OUTLINE` (hdmi_logic.rs:695–716) does 4-neighbor dilation.
- Sampling mode: `LINEAR` (hdmi.rs:1923). Already filterable — SDF rewrite keeps the same sampler.

**Canvas2D / browser preview** (`ui/src/rasterize.js`):

- Phase 3c (2026-05-14) cut the primary path over to **WASM-fontdue** (`rasterizeText` from `wasm-renderer.js`); the legacy `ctx.fillText`-with-`ctx.scale` path is the fallback when WASM isn't ready yet.
- Pixel positioning: integer-snap via `Math.round(baselineY - result.ascent)` (rasterize.js:316). No `ctx.fontKerning` / `ctx.textRendering` calls — no subpixel positioning APIs invoked.
- Vertical squish: `ctx.scale(1, yScale)` on the fallback path, or 9-arg `drawImage()` downscale on the WASM path (rasterize.js:322–326).
- Word-wrap: `wrapTextToWidth()` (rasterize.js:364–390), iterates words + measures via `ctx.measureText()`. Mirrors the Rust `_wrap_text_to_width`.

**Python `text_raster.py`** (`backend/openmarquee/text_raster.py`):

- **Not deleted** by DELETE-PIL — still alive in production via `seed.py`'s first-boot seed asset generation (gradient backgrounds + the bundled demo reel's text PNG bake at install time). `seed.py:1554–1560` imports `load_font` + `measure_centered` + `cached_truetype`.
- Pillow `ImageFont.truetype()` with a 128-entry LRU keyed on `(path, size)`. Bundled font lookup via `BUNDLED_FONT_FILES` (text_raster.py:32–56) → `ui/fonts/`.
- Not on the render hot path post-DELETE-PIL. SDF migration doesn't touch this; emoji migration may (Section 9).

**Pixel-perfect parity** (`scripts/parity/fixtures.json`):

- Defaults: `ssim_min = 0.92`, `mean_delta_max = 8`. No per-pixel allowlist; gating is structural (SSIM + L1 mean).
- Phase 3 dropped `max_delta_max` because fontdue's bitmap AA produces ~229-delta single-pixel spikes on glyph edges that exceeded any meeting threshold (`run.py:27–33`).
- `bless_fys_goldens.py` scp's the FYS demo reel's renders from the Pi and overwrites `renderer/tests/golden/*.png`. Per `[[reference_bless_uses_opt_path]]`, the bless script invokes `/opt/openmarquee/bin/openmarquee-render`, NOT `/usr/local/bin/`. Both paths must carry the same binary after deploy.

---

## 2. fontdue SDF audit

**fontdue does not produce SDFs.** It rasterizes glyph outlines to alpha bitmaps via `rasterize()` / `rasterize_indexed()`. Color emoji (CBDT / sbix / COLR) is wishlist-only ([fontdue#1](https://github.com/mooman219/fontdue/issues/1)). For SDF we need a separate generator.

**Rust SDF generator options**, in rough order of fit for our scale range:

- **`msdfgen`** (https://docs.rs/msdfgen) — safe Rust wrapper around Chlumsky's C++ msdfgen. Produces MSDF / MTSDF / pseudo-SDF / plain SDF; integrates with `ttf-parser` (already in fontdue's dep tree). C++ dep, but a contained one. **Best fit** for our 16px → ~1500px range (62× upscale, see §5).
- **`msdf` / `msdf-rs`** (https://docs.rs/msdf) — alternative safe binding to the same C++ msdfgen via `msdf-sys`. Equivalent functionality; lighter integration surface than `msdfgen`.
- **`msdf_font`** (https://lib.rs/crates/msdf_font) — atlas + error correction; mostly C++ msdfgen translated to Rust. Worth a look if we want pure-Rust without FFI.
- **`fontsdf`** (https://lib.rs/crates/fontsdf) — pure Rust, `no_std`, generates **single-channel** SDF directly from outlines (not by downscaling a bitmap). Lighter dep footprint but single-channel-only.
- **`msdfont`** (https://github.com/Blatko1/msdfont) — pure-Rust MSDF; less mature than `msdfgen` but no C++ dep.

**Single-channel SDF vs MSDF for our scale range:** Single-channel SDF rounds sharp corners at high upscale ratios (Valve 2007 SIGGRAPH paper's documented ~4× ceiling; Chlumsky / Red Blob writeups for the failure mode). At 24px SDF cell → 1500px on-screen = ~62× upscale, single-channel will visibly soften serif terminals, type counters, and tight joins. **MSDF is the right default** for an 8-font Latin reel with Anton + Alfa Slab One + Bowlby One SC (heavy display weights with sharp inside corners).

**Best-guess recommendation, flagged for override:** `msdfgen` Rust crate, MSDF (3-channel) output. The C++ FFI is the cost; the upside is mature parity with the canonical msdfgen implementation everyone else (Mapbox, Three.js wrappers, Bevy) gates against. If qarl wants pure-Rust no-FFI, `fontsdf` single-channel + a smaller scale ceiling (cap on-screen at ~250px) is the fallback — but that's a feature-narrowing call.

---

## 3. Shader design + vc4 derivatives check

**SDF threshold fragment shader, single-channel form:**

```glsl
precision mediump float;
varying vec2 v_uv;
uniform sampler2D u_atlas;
uniform vec4 u_text_color;

void main() {
    float d = texture2D(u_atlas, v_uv).r;
    float aa = fwidth(d);                        // adaptive AA width
    float a = smoothstep(0.5 - aa, 0.5 + aa, d); // anti-aliased threshold
    gl_FragColor = vec4(u_text_color.rgb * a, a);
}
```

**MSDF variant** (recommended per §2):

```glsl
void main() {
    vec3 s = texture2D(u_atlas, v_uv).rgb;
    float d = max(min(s.r, s.g), min(max(s.r, s.g), s.b)); // median
    float aa = fwidth(d);
    float a = smoothstep(0.5 - aa, 0.5 + aa, d);
    gl_FragColor = vec4(u_text_color.rgb * a, a);
}
```

The median-of-three preserves sharp corners that single-channel SDF rounds.

**vc4 GLES2 derivatives — open measurement question:**

- vc4 ships `GL_OES_standard_derivatives` (Khronos extension registry: present on Mesa VC4 / Gallium 0.4 / V3D 2.1). `dFdx` / `dFdy` / `fwidth` are callable on the Pi Zero 2 W.
- There's a documented Mesa warning about "wrong precision" when enabling the extension on VC4 (https://github.com/libretro/RetroArch/issues/5374 and related). Whether the precision is *unusable* for SDF AA or just a noisy warning is **not answerable from public docs in 15 minutes.**
- **Best-guess assumption:** the precision is workable. `fwidth()` on a smooth distance field is a low-frequency signal — the precision floor of `mediump` is ~10 bits of mantissa, which gives ~3 ulps of error at the threshold edge. That's well below visible AA noise on a 1080p display where one pixel ≈ 1/1920 in NDC space.
- **Empirical test proposal (Section 3.X work item, 30–60 min of glass time):** a single-quad spike on the dev Pi that renders an MSDF glyph two ways — `fwidth()`-driven AA vs fixed-pixel AA (`aa = 1.5 / glyph_height_px`) — and pixel-diffs them. If the diff is within parity allowlist (Section 10), ship `fwidth()`. If the precision is too coarse, fall back to fixed-pixel AA + accept a small loss on extreme rotation / shear (which we don't currently use).

**Perf cost on Pi Zero 2 W at 1080p:**

- Current `FS_GLYPH` is one texture fetch + one multiply. SDF would be one fetch + ~5 ALU ops. The fetch dominates; ALU delta is rounding error on a VC4 fragment program.
- The per-glyph rasterization cost goes away (cache hit serves the SDF atlas; fontdue is invoked only at atlas-build time, see §8). This is a net win on slide-change overhead at large font sizes.

---

## 4. Atlas sizing math

**vc4 ceiling:** `GL_MAX_TEXTURE_SIZE = 2048` (hdmi_logic.rs:201). The SDF atlas must fit in a single 2048×2048 RGB8 texture (MSDF) or 2048×2048 R8 (single-channel SDF). RGB8 atlas = ~12 MB; R8 = ~4 MB. Comfortable on the Pi Zero 2 W's 512 MB.

**FYS reel font count:** 8 families confirmed from `backend/openmarquee/seed.py` — Anton, Alfa Slab One, Bowlby One SC, Playfair Display, Caveat Brush, VT323, JetBrains Mono, Permanent Marker. (Worst-case glyph coverage: Latin-1 ≈ 256 glyphs per font, Latin-Extended-A ≈ 384.)

**Atlas math at varying cell sizes:**

| Cell | Cells per atlas (2048²) | Glyphs/font (Latin-1) | Fonts that fit |
|------|------------------------:|----------------------:|---------------:|
| 32×32  | 4096 | 256 | 16 |
| 48×48  | 1764 | 256 | 6 |
| 64×64  | 1024 | 256 | 4 |
| 96×96  | 441  | 256 | 1.7 |
| 128×128 | 256 | 256 | 1.0 |

**Recommendation:** **64×64 MSDF cells**, one atlas-per-font (8 atlases, 8 texture units). At 64px MSDF, Mapbox's well-documented production-tested numbers translate cleanly: their 24pt single-channel atlas covers their full scale range; ours at 64px MSDF gives us margin for the FYS reel's larger letterforms.

Alternative: **48×48 MSDF cells**, two fonts per atlas, packed via a simple shelf packer (1764 cells / 2 fonts ≈ 880 cells each — covers Latin-Extended). Halves the texture-unit count + atlas-build time. **Pre-approved per qarl ("reduce atlas tile size if it helps")** — recommend 48×48 if Section 5's scale-ceiling math holds.

**Glyph counts per font from FYS** — I have not enumerated which exact codepoints the reel actually uses. Worst-case Latin-1 is the safe over-cover. A 1-shot codepoint-set pass over the reel's text would let us pack tighter; flagging as a Section 4.X work item if we end up tight on atlas budget.

---

## 5. Scale ceiling math

**Largest on-screen FYS text:** Slide `f15000000001` ("01 · FREE") — Anton font, `font_size_pct: 80.0`, box `w = 0.9`, `h = 0.829`. At 1920×1080:

- Font size px ≈ `0.80 × 0.9 × 1920` = **1382 px** (nominal). Vertical squish + motion can push this higher transiently.

**SDF upscale ratios:**

- **Single-channel SDF:** ~4× clean before corner rounding kicks in. From a 64px cell, that's a 256px ceiling. **Our 1382px requirement exceeds this by 5.4×** — single-channel is not viable for the FYS reel.
- **MSDF:** practical upscale up to ~16× before edge artifacts (with `fwidth()` AA); soft ceiling ~30× before pixel-grid quantization shows. From a 64px cell, 16× = 1024px and 30× = 1920px. **Our 1382px sits comfortably inside the MSDF range.**
- From a 48px cell: 16× = 768px, 30× = 1440px. **1382px is at the 28× ratio — within the soft ceiling for MSDF.** This is the load-bearing fact for the "48×48 if it covers the reel" pre-approval.

**Conclusion:** 64×64 MSDF is the conservative pick. 48×48 MSDF is the pre-approved-if-it-works pick that halves the atlas budget — and the math says it works for FYS. **Recommendation: 48×48 MSDF, validated against the FYS-01-FREE slide in the impl arc's first parity pass.**

**Assumption flag:** the "30× soft ceiling" number is from Chlumsky's writeup + the Three.js MSDF wrapper community; I'm not citing a specific paper. If qarl wants empirical confirmation before shader work starts, the impl arc's first slice (atlas generation) is the right time to capture an MSDF-render-at-1382px screenshot for review.

---

## 6. Subpixel positioning

**Current canvas2d behavior** (rasterize.js:316): `Math.round(baselineY - result.ascent)` — integer pixel snapping on Y; X is laid out via fontdue's `Layout` which returns integer pixel x-positions per glyph. **No subpixel positioning today on either canvas2d or Rust.**

**SDF natively supports subpixel positioning** — the sampler can be placed at any fractional UV. **We choose not to use it.** Per `[[feedback_pixel_perfect_renderer_parity]]`, pixel-perfect parity across canvas2d + Rust is the contract; introducing subpixel SDF in Rust would break that contract.

**Migration rule:** the SDF glyph quad's vertex positions get the same `floor()` / `round()` math the bitmap path does today. Subpixel positioning is reserved for a future "smoothness mode" (e.g. animated text mid-transition) that we'd opt into per-layer — not the default render path. Recommend deferring that feature to a post-arc slice and shipping pixel-snapped SDF in the first impl arc.

---

## 7. Animated text + transitions

**Current architecture:** transition fragment shaders (FS_IRIS / FS_PUSH / FS_FLIP / FS_MARQUEE / FS_SCANLINE / FS_GLITCH / FS_SHUTTER + the standard 6: FS_CUT, FS_FADE, FS_BLIT, FS_GRADIENT, FS_DISSOLVE, FS_PIXELATE) all sample a **pre-rasterized scene FBO**, not the glyph atlas directly. Text is composited into the scene FBO in Phase 4 (before the transition phase samples).

**This is great news for SDF migration:** transitions don't care that the scene FBO was composited from SDF glyphs vs. bitmap glyphs. The migration is **atlas-level + glyph-shader-level only** — no transition-shader edits.

**Caveat:** the glyph FBO baking step (Phase 4, hdmi.rs:1886–1930) runs `FS_GLYPH` at the current font size, sampling the per-layer alpha bitmap. After SDF migration, this baking step runs `FS_MSDF` (the §3 shader) at the requested on-screen quad size, sampling the SDF atlas. The output FBO shape is identical (an RGBA8 pre-composited scene); downstream transitions are untouched.

**One concrete check before impl:** the glyph FBO bake must run at the requested quad size, not at the SDF cell size. The vertex shader emits a quad sized to the requested on-screen rectangle; the fragment shader samples the SDF atlas with UVs that map the quad surface back to the atlas cell. Standard SDF-atlas-quad math. No new geometry pipeline.

**Outline / shadow effects:** current `FS_GLYPH_OUTLINE` (hdmi_logic.rs:695–716) does 4-neighbor dilation on the bitmap. SDF outlines are simpler — sample once, compare against two thresholds:

```glsl
float a_fill = smoothstep(0.5 - aa, 0.5 + aa, d);
float a_outline = smoothstep(0.45 - aa, 0.45 + aa, d) - a_fill;
gl_FragColor = vec4(text_color * a_fill + outline_color * a_outline, a_fill + a_outline);
```

The outline width is in *SDF distance units*, which means it stays visually consistent across scale — a free quality win over the current pixel-dilation approach.

---

## 8. Migration plan

**Atlas generation timing:** **At binary build time, baked into the executable.** Rationale:

- Atlas regen at startup costs ~50–200 ms per font on Pi Zero 2 W (msdfgen at MSDF quality is not cheap). Eight fonts → 0.4–1.6 s of cold-start latency we don't want.
- Disk cache works but adds a sync/invalidation surface (font version vs atlas version) we don't need.
- Bake-time generation: a `build.rs` step that calls `msdfgen` for each `ui/fonts/*.ttf` we want to ship, emits a `.bin` (or `include_bytes!`-able blob) per font, and the runtime just `gl.tex_image_2d`'s it in at `open()` time. Mapbox + Three.js + Bevy all use this pattern.

**Rebless path:** per `[[reference_bless_uses_opt_path]]`, `bless_fys_goldens.py` invokes `/opt/openmarquee/bin/openmarquee-render` to generate the new goldens, but the systemd unit launches `/usr/local/bin/openmarquee-render`. After deploy, both paths get the binary. The SDF migration changes the rendering output deterministically, so the rebless step is **mandatory** in the deploy slice — every FYS golden gets re-blessed against the SDF output.

**Migration sequencing (per the arc plan in Section 11):**

1. Add `msdfgen` (or `msdf-rs`) as a build-time dep, not a runtime dep. Atlas blobs are checked into `renderer/assets/sdf-atlases/` next to the .ttf files.
2. New `FS_MSDF` shader added to hdmi_logic.rs (replaces `FS_GLYPH`).
3. `layout_text_to_alpha()` is renamed to `layout_text_to_quads()` and emits per-glyph quad geometry instead of an alpha bitmap. The `clamp_size_px_to_bitmap_cap()` path (and `MAX_RASTERIZED_BITMAP_DIM`) goes away — the bug is fixed.
4. `CachedGlyph` re-shapes around quad geometry, not bitmap pixels.
5. FYS goldens re-blessed in the deploy slice.

**Backward compat:** none needed. The DELETE-PIL arc just shipped a major architecture change; SDF is the natural sibling.

---

## 9. Emoji support

**The shape of the problem:**

- Browser + Python already render emoji via Noto Color Emoji as a fallback / segmented run. CSS @font-face cascade in `ui/styles.css:42–47` with `unicode-range: U+1F000-1FFFF, U+2600-27BF`. Python `seed.py:1411` defines `_segment_text_for_emoji` and `seed.py:1442` defines `_load_emoji_font` — together they split runs by codepoint range and render with `PIL.ImageDraw.text(..., embedded_color=True)`.
- Rust renderer has **zero emoji**. fontdue can't rasterize CBDT / sbix / COLR. Slides with emoji silently drop the emoji codepoints (or render `tofu` boxes — needs an empirical check).

**The CBDT problem:** Noto Color Emoji is a CBDT / CBLC font — color bitmap data embedded in the TTF as PNG payloads. **It cannot be rasterized as SDF.** SDF works on vector outlines; CBDT has no outlines (the COLRv1 Noto variant does have outlines, but Rust support for COLRv1 is not yet production-ready per cosmic-text issue #2546).

**The Rust crate options:**

- **`ttf-parser`** (already in fontdue's dep tree): parses CBDT, sbix, EBDT/EBLC, COLR (v0+v1), CPAL. Provides byte-level access; doesn't render.
- **`swash`** (https://github.com/dfrg/swash): renders sbix + CBDT + COLR/CPAL. Mature, used in cosmic-text.
- **`cosmic-text`**: full shaping + rendering pipeline; CBDT works, COLRv1 doesn't yet.

**Best-guess architecture:** parallel **color-bitmap atlas** alongside the SDF atlas:

- At build time, extract CBDT PNG payloads from `noto-color-emoji.ttf` via `ttf-parser` and pack them into a 2048×2048 RGBA8 atlas. Tile size 96×96 covers Noto's largest CBDT bitmaps (Noto ships at 128×128 — we downscale at bake time to fit our atlas budget; emoji is rendered at moderate sizes in practice, so the downscale loss is acceptable).
- At layout time, **segment** the text run by codepoint ranges (same range list the browser uses: U+1F000-1FFFF, U+2600-27BF, plus the standard variation-selector handling). For each sub-run, emit either SDF quads (text) or color-bitmap quads (emoji).
- Fragment shader: a uniform flag picks SDF threshold-sampling vs straight RGBA passthrough.
- Color-bitmap glyphs are pixel-quantized at their atlas size — no smooth upscale story. That's an honest tradeoff: emoji at 1382px will look blocky. Mitigation: cap emoji on-screen size at the atlas bitmap size; downstream slides that want huge emoji use the same trick the browser uses (CSS pixel-fits emoji to its bitmap size).

**Spec edit needed:** SYSTEM_SPEC §5.10a (text layers) currently doesn't mention emoji. The recon flags this as a queued sub-task — an inline note that text runs are emoji-segmented, with U+1F000-1FFFF + U+2600-27BF as the first-class codepoint ranges. **Spec edit lands in the impl arc's emoji slice (slice C in §11), not in this recon commit.**

**Assumption flags:**

- "Noto Color Emoji ships at 128×128 CBDT" — I'm citing from memory of the Noto repo; should be verified at build time when the build.rs step runs `ttf-parser` on the actual TTF file.
- COLRv1 not yet supported in Rust — accepting this for v1; revisit in 6 months if cosmic-text catches up.
- The "downscale CBDT to 96×96 at bake" loses some emoji fidelity. If qarl wants edge-perfect emoji we can use the full 128×128 atlas tile (still fits 256 tiles in 2048² = 65k cells; emoji codepoint count is ~3500 unique = fits comfortably at 128×128 in a single 2048² atlas at 256 tiles per row × 16 rows = need 14 rows for 3500 emoji).

---

## 10. Test/parity strategy

**The fundamental tension:** SDF subpixel sampling vs canvas2d pixel-snap parity. Per §6 we resolve this by pixel-snapping SDF quad vertices to match canvas2d's behavior. With pixel-snapped quads, the only AA difference is the SDF threshold-edge AA vs canvas2d's bitmap-AA.

**Expected parity-test impact:**

- The existing `ssim_min: 0.92` + `mean_delta_max: 8` thresholds are tuned against bitmap-AA differences. SDF threshold-AA is a different noise profile — sharper edges, less subpixel ringing.
- Initial parity runs will likely fail until thresholds are re-tuned. Tune AGAINST the SDF output (not against canvas2d), since canvas2d is the second-class path now (per the WASM-fontdue cutover noted in §1).
- The `max_delta_max` gate is already dropped (run.py:27–33); the existing `mean_delta_max: 8` will need a one-time re-bless against SDF output. **Recommend keeping the threshold value the same and re-blessing all 37 FYS goldens** in the deploy slice.

**Threshold tuning workflow:**

1. Render all 37 FYS slides with the SDF binary.
2. Diff against canvas2d (the same parity-harness.html path that produces the goldens today).
3. If `ssim_min: 0.92` still holds → ship.
4. If a fixture trips the threshold but the diff is "SDF is clearly sharper than the bitmap path" → re-bless the golden, the bitmap path was the old reference.
5. Edge case: a fixture where SDF produces a *worse* result (rounded corner on a heavy display font) → this is the MSDF-vs-single-channel decision boundary from §2. If we picked MSDF (recommended), shouldn't happen.

**Threshold-tune slice (in §11 slice plan):** allow one slice-cycle of parity-threshold-adjustment after the shader+atlas slice lands and before the deploy slice closes.

---

## 11. Implementation slice plan

Five slices, dispatched in order. Each slice gets the standard pre-commit subagent review per `[[feedback_subagent_review_required]]`.

**Slice A — Atlas generation (build.rs + msdfgen)** *(~1–2 days)*

- Add `msdfgen` crate as a build-time dep (or `msdf-rs` — qarl pick).
- New `renderer/build.rs` step: iterate `ui/fonts/*.ttf` (matching the FYS reel font set + Noto), invoke msdfgen for each, emit `renderer/assets/sdf-atlases/<font-stem>.msdf` as a packed binary.
- Atlas layout: 48×48 MSDF cells (or 64×64 if FYS parity reveals corner rounding — pre-approved fallback).
- Side-by-side codepoint coverage check: the bake step also emits a `<font-stem>.codepoints.json` listing the U+ codepoints baked, so the runtime layout pass can `tofu` unknown codepoints deterministically (instead of silently dropping).
- Tests: cargo unit test that the bake step produces a non-empty atlas + the expected number of codepoints for a known input font.

**Slice B — Shader integration (FS_MSDF + glyph quad pipeline)** *(~2–3 days)*

- New `FS_MSDF` shader (the §3 median-of-three threshold form). Replaces `FS_GLYPH` for text layers.
- New `FS_MSDF_OUTLINE` (the §7 dual-threshold form). Replaces `FS_GLYPH_OUTLINE`.
- `layout_text_to_alpha()` → `layout_text_to_quads()`: per-glyph quad geometry instead of bitmap; consults `<font-stem>.codepoints.json` for atlas UV lookup.
- `CachedGlyph` re-shape: caches quad-vertex-buffer-handles + texture-handle, not bitmap bytes.
- `MAX_RASTERIZED_BITMAP_DIM` + `clamp_size_px_to_bitmap_cap()` deleted. The font-clamp bug is fixed by construction.
- Tests: cargo unit tests on quad geometry for a known glyph + known font size + known on-screen quad size. Existing `test_glyph_*` tests get re-aimed at the quad path.

**Slice C — Emoji color-bitmap parallel atlas** *(~2–3 days)*

- Add `ttf-parser` direct usage (already in fontdue's dep tree as a transitive — promote to direct).
- New build.rs sub-step: extract CBDT PNGs from `noto-color-emoji.ttf`, decode, pack into a 2048×2048 RGBA8 emoji atlas. Tile size 96×96 (or 128×128 — see §9 assumption).
- New `FS_EMOJI` shader: straight RGBA passthrough sampling the emoji atlas (no SDF math).
- `layout_text_to_quads()` adds the codepoint-segmentation step (per §9), emitting quads tagged with either SDF or EMOJI source.
- SYSTEM_SPEC §5.10a edit: emoji-segmentation as a first-class text feature; codepoint ranges documented.
- Tests: a new FYS-equivalent slide with mixed text+emoji + a parity test against the canvas2d path (the browser side already segments correctly).

**Slice D — Parity test re-bless** *(~1 day)*

- Run `bash scripts/parity_tests.sh` against the SDF binary on the dev Pi.
- For each fixture that drifts: classify (SDF-better → re-bless golden; SDF-worse → flag for review; same-but-different-noise → adjust threshold).
- Update `scripts/parity/fixtures.json` per-fixture overrides where needed.
- The §10 measurement work happens here — concrete numbers, not assumptions.

**Slice E — Deploy + verify** *(~half-day)*

- Cross-build via `scripts/renderer_cross_build.sh` (per `[[project_virtiofs_cargo_workaround]]`).
- Deploy to FYS Pi (192.168.1.67) — BOTH `/usr/local/bin/openmarquee-render` AND `/opt/openmarquee/bin/openmarquee-render` per `[[reference_bless_uses_opt_path]]`.
- 60+ sec heartbeat watch + cycle-through-all-37-FYS-slides verify.
- Re-run `bless_fys_goldens.py` to capture the SDF-rendered goldens for the canvas2d parity baseline.

**Cumulative estimate:** ~7–10 days end-to-end across the 5 slices, mostly Rust work + a small spec edit. The vc4 derivatives empirical question (§3.X) is a ~1 hour spike that should land in slice A (early signal — if derivatives are unusable, we fall back to fixed-pixel AA before the shader work in slice B).

---

## Open assumption flags (consolidated)

Per `[[feedback_make_best_guess_on_broad_mandates]]`, the recon makes best-guess calls on the following genuine measurement questions. Each is flagged here so qarl can override:

1. **MSDF over single-channel SDF** (§2). Single-channel rounds corners at >4× upscale; our worst case is ~28×. MSDF is the conservative pick. Override = `fontsdf` + accept softer corners on large display weights.
2. **48×48 MSDF cells** (§4 + §5). Saves atlas budget vs 64×64 and the math says it covers the worst FYS slide. Override = 64×64 for headroom.
3. **vc4 `fwidth()` precision is workable** (§3). Best-guess from extension-presence + visual-noise math. Empirical test queued for slice A. Override = fixed-pixel AA fallback baked into slice B.
4. **No subpixel positioning** (§6). Pixel-snap to preserve parity-test contract. Override = subpixel as opt-in "smoothness mode" per-layer.
5. **MSDF crate = `msdfgen` (C++ FFI)** (§2). Best-tested option. Override = `fontsdf` pure-Rust + accept single-channel limits.
6. **Build-time atlas baking** (§8). Mapbox/Three.js/Bevy pattern; avoids cold-start cost. Override = runtime regen + disk cache.
7. **Emoji at 96×96 atlas tile** (§9). Downscaled from Noto's 128×128 CBDT. Override = 128×128 (still fits comfortably).
8. **No COLRv1 support** (§9). cosmic-text doesn't ship it yet. Override = block on Rust ecosystem catching up.

All eight are reversible mid-arc. None are load-bearing on the slice ordering — change them between slices A and B if qarl pushes back during the dispatch cycle.
