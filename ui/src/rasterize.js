// Canvas rasterization + paint helpers. Extracted from editor.js
// (Batch 5.1) so the math + composition is in its own focused
// module. Public surface: drawTextOnly, drawCanvas, pickFontSize,
// pickFontSizePct, rasterizeAtTarget.
//
// drawCanvas accepts BOTH editor-state shape (`state.layers` with
// internal field names) AND a flat single-layer back-compat shape
// (`state.text`, `state.textColor`, …) for callers that haven't
// migrated. The two shapes are distinguished by presence of
// `.layers`.

import { formatAutoText } from "./auto-format.js";
import { paintPatternOnCanvas } from "./bg-system.js";
import { paintLayerWithMotion } from "./canvas-motion.js";
import { cssFontFamily, FONT_WEIGHT_BY_VALUE } from "./font-picker.js";
import { canvasToBase64 } from "./image-upload.js";
import { isFontRegistered, isWasmReady, rasterizeText } from "./wasm-renderer.js";

const RASTERIZE_W = 3840;
const RASTERIZE_H = 2160;

// FYS bug 8: the WASM/fontdue rasterizer is monochrome — it renders
// emoji codepoints as tofu. Extended_Pictographic is the Unicode
// property for emoji pictographs (😱, ☃, …); it deliberately does
// NOT match plain digits / shapes, so only genuine color emoji
// trip the fillText fallback. Non-global so `.test()` is stateless.
const EMOJI_RE = /\p{Extended_Pictographic}/u;

// Map TextLayer.blend -> Canvas2D globalCompositeOperation so the
// editor preview matches the backend's PIL composite_with_blend
// math (W3C compositing spec: multiply/screen/overlay are first-
// class canvas modes, parity with backend modulo anti-aliasing).
// Keys MUST stay in sync with TextLayer.blend's Literal in
// content/__init__.py and _BLEND_MODES in rendering/blend.py.
// Parse a CSS color string to [r, g, b, a] (0-255 each). Supports
// the formats paintLayer actually receives from the editor:
// `#RGB`, `#RRGGBB`, `#RRGGBBAA`, `rgb(r, g, b)`, `rgba(r, g, b, a)`.
// Anything unrecognized returns null so the caller can fall back to
// ctx.fillText (which has its own CSS-color resolver). Hot-path
// safe: no regex backtracking, no allocations on the common hex case.
//
// Phase 1b parity follow-up: the WASM rasterizer takes 4 u8s for
// color; this fn produces them from the layer's `text_color` string.
function parseCssColorRgba(css) {
    if (typeof css !== "string") return null;
    const s = css.trim();
    if (s.startsWith("#")) {
        const hex = s.slice(1);
        if (hex.length === 3) {
            const r = parseInt(hex[0] + hex[0], 16);
            const g = parseInt(hex[1] + hex[1], 16);
            const b = parseInt(hex[2] + hex[2], 16);
            if (Number.isNaN(r) || Number.isNaN(g) || Number.isNaN(b)) return null;
            return [r, g, b, 255];
        }
        if (hex.length === 6) {
            const r = parseInt(hex.slice(0, 2), 16);
            const g = parseInt(hex.slice(2, 4), 16);
            const b = parseInt(hex.slice(4, 6), 16);
            if (Number.isNaN(r) || Number.isNaN(g) || Number.isNaN(b)) return null;
            return [r, g, b, 255];
        }
        if (hex.length === 8) {
            const r = parseInt(hex.slice(0, 2), 16);
            const g = parseInt(hex.slice(2, 4), 16);
            const b = parseInt(hex.slice(4, 6), 16);
            const a = parseInt(hex.slice(6, 8), 16);
            if (Number.isNaN(r) || Number.isNaN(g) || Number.isNaN(b) || Number.isNaN(a)) {
                return null;
            }
            return [r, g, b, a];
        }
        return null;
    }
    const rgbMatch = s.match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+))?\s*\)$/);
    if (rgbMatch) {
        const r = parseInt(rgbMatch[1], 10);
        const g = parseInt(rgbMatch[2], 10);
        const b = parseInt(rgbMatch[3], 10);
        const aFloat = rgbMatch[4] !== undefined ? parseFloat(rgbMatch[4]) : 1.0;
        const a = Math.max(0, Math.min(255, Math.round(aFloat * 255)));
        return [r, g, b, a];
    }
    return null;
}

const BLEND_TO_CANVAS = {
    normal: "source-over",
    multiply: "multiply",
    screen: "screen",
    overlay: "overlay",
};

/**
 * Render only the text layers of a TextSlide onto `canvas`, leaving
 * the canvas's background transparent. Used by the inline-preview
 * to overlay text on top of a live video frame for Text-over-Video
 * slides (Phase 5b — SYSTEM_SPEC §5.10). Iterates `text_layers` in
 * array order (later entries composite over earlier).
 *
 * Accepts the on-the-wire ContentItem shape — not the editor's
 * internal `state` — because the inline-preview consumes
 * ContentItem directly.
 */
export function drawTextOnly(canvas, item, opts) {
    const ctx = canvas.getContext("2d");
    ctx.save();
    try {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        const layers = Array.isArray(item.text_layers) ? item.text_layers : [];
        const elapsed = opts && opts.elapsed_s;
        const slideKey = (item && (item.id || "?")) + "";
        for (let i = 0; i < layers.length; i++) {
            const layer = layers[i];
            // v1.0 close (2026-05-30) — match drawCanvas (line 517+) on
            // hidden-layer skipping. Pre-fix, a Text-over-Video slide
            // with a hidden layer would still paint that layer on top
            // of the video frame in the inline-preview path, diverging
            // from drawCanvas's behavior on the same slide. SYSTEM_SPEC
            // §5.10a lines 320-323 require save-time + preview honoring
            // of visible: false.
            if (layer?.visible === false) continue;
            // Auto-mode resolution: mirror drawCanvas (line 510). Without
            // this, a Text-over-Video slide with an auto_mode="time" /
            // "date" / "day" layer rendered the placeholder token
            // verbatim ({{time}}, ...) in the inline-preview path while
            // drawCanvas (editor) AND the Rust device path resolved
            // correctly. paintLayerWithMotion still receives the RAW
            // layer so motion / blend / opacity metadata stays sourced
            // from the wire shape; only the text content gets resolved.
            const resolved = resolveLayerForDraw(layer);
            const paint = () => paintLayer(ctx, canvas, resolved, /* fillBox */ null);
            if (elapsed === undefined || elapsed === null) {
                // Static path — current behavior, no motion.
                paint();
            } else {
                paintLayerWithMotion(ctx, canvas, layer, paint, {
                    elapsed_s: elapsed,
                    layerKey: `${slideKey}:${i}`,
                });
            }
        }
    } finally {
        ctx.restore();
    }
}

/**
 * Paint a single layer's text onto an already-cleared / pre-filled
 * context. `box` defaults to {0.1, 0.1, 0.8, 0.8} when absent.
 * Mirrors `_draw_text_into` on the backend (seed.py).
 */
function paintLayer(ctx, canvas, layer) {
    const text = layer?.text || "";
    if (!text) return;
    const textColor = layer.text_color || layer.textColor || "#FFFFFF";
    const fontFamily = layer.font_family || layer.fontFamily || "sans-serif";
    const box = layer.box || { x: 0.1, y: 0.1, w: 0.8, h: 0.8 };

    const boxX = box.x * canvas.width;
    const boxY = box.y * canvas.height;
    const boxW = Math.max(1, box.w * canvas.width);
    const boxH = Math.max(1, box.h * canvas.height);

    let fontSizePx;
    const pct = layer.font_size_pct ?? layer.fontSizePct;
    const px = layer.font_size_px ?? layer.fontSize;
    if (Number.isFinite(pct) && pct > 0) {
        // §5.10a v3.1.2 (qarl 2026-05-01 review #3): font_size_pct is
        // a percentage of BOX WIDTH (not slide width). Resizing the
        // box visibly resizes the text — operators expected that math
        // and asked for it explicitly. Math: pct% × box.w × canvas.width
        // = pct% × boxW (already in pixels).
        fontSizePx = Math.max(4, Math.round((boxW * pct) / 100));
    } else if (Number.isFinite(px) && px > 0) {
        fontSizePx = px;
    } else {
        fontSizePx = pickFontSize(boxW);
    }
    ctx.fillStyle = textColor;
    const weight = FONT_WEIGHT_BY_VALUE.get(fontFamily) ?? 700;
    ctx.font = `${weight} ${fontSizePx}px ${cssFontFamily(fontFamily)}`;
    const textAlign = layer.text_align || layer.textAlign || "center";
    ctx.textAlign = textAlign === "left"
        ? "left"
        : textAlign === "right"
            ? "right"
            : "center";
    // Use textBaseline="alphabetic" + explicit baseline placement so the
    // vertical anchor depends on actual glyph ink (matching Rust's
    // fontdue ascent/descent) rather than the font-bounding-box (which
    // `textBaseline="middle"` keys off of and which varies by engine
    // for tall em-box fonts like VT323). Parity-audit P0 follow-up to
    // b445aa5 — was the dominant Canvas2D ↔ Rust vertical-positioning
    // divergence (77,651 mismatched-vertical pixels on the Boot slide).
    ctx.textBaseline = "alphabetic";

    // Word-wrap (B4, 2026-05-05): any paragraph longer than the box
    // width gets broken at word boundaries onto multiple lines. Pre-
    // existing literal newlines are preserved.
    const wrapped = wrapTextToWidth(ctx, text, boxW);
    const lines = wrapped.split(/\r?\n/);
    // Integer line-height matching `renderer/src/hdmi_logic.rs:269`
    // (Rust: `(size_px * 1.1).round() as u32`). Float line-height
    // accumulated 0.4 px / line drift over 6 boot-log lines.
    const lineHeight = Math.round(fontSizePx * 1.1);
    // Glyph-ink ascent/descent across all chars in all lines, matching
    // Rust's `max_ascent` / `min_descent` over all glyphs (hdmi_logic.rs:
    // 417-428). measureText().actualBoundingBoxAscent/Descent is the
    // Canvas2D equivalent — distance from baseline to TOP/BOTTOM of
    // the actual rendered glyph bbox, not the font's em-box.
    //
    // Measured across the JOINED text so the worst-case ascent +
    // descent reflects all characters; lines with descender-bearing
    // glyphs (g, j, p, q, y, Q) then share uniform vertical advance.
    // Falls back to a 0.8/0.2 split of fontSizePx if the canvas impl
    // doesn't expose the API (jsdom test ctx mocks don't stub
    // measureText, and some browsers' impls don't populate the
    // actualBoundingBox* fields).
    let maxAscent;
    let maxDescent;
    if (typeof ctx.measureText === "function") {
        const inkMetrics = ctx.measureText(lines.join(""));
        maxAscent = Number.isFinite(inkMetrics.actualBoundingBoxAscent)
            ? inkMetrics.actualBoundingBoxAscent
            : fontSizePx * 0.8;
        maxDescent = Number.isFinite(inkMetrics.actualBoundingBoxDescent)
            ? inkMetrics.actualBoundingBoxDescent
            : fontSizePx * 0.2;
    } else {
        maxAscent = fontSizePx * 0.8;
        maxDescent = fontSizePx * 0.2;
    }
    const lastLineExtent = maxAscent + maxDescent;
    // Match Rust's `bm_h = last_line_extent + (N - 1) * line_h` (the
    // pad isn't ink-relevant for centering; it's a 1px halo for
    // outline shaders).
    const totalInkExtent = lastLineExtent + (lines.length - 1) * lineHeight;
    const boxCenterY = boxY + boxH / 2;
    // First-line baseline = top of ink bbox + maxAscent. The ink
    // bbox top sits at `boxCenter - totalInkExtent/2`.
    const firstBaselineY = boxCenterY - totalInkExtent / 2 + maxAscent;

    // Anchor x depends on textAlign: left = box left, right = box right,
    // center = box center. Canvas's textAlign+x interplay handles the
    // per-line offset.
    let anchorX;
    if (textAlign === "left") anchorX = boxX;
    else if (textAlign === "right") anchorX = boxX + boxW;
    else anchorX = boxX + boxW / 2;
    const maxWidth = Math.max(1, boxW);
    // Vertical squish (qarl 2026-05-01 ask #1): when total rendered
    // text height exceeds box height, scale-y around the box center
    // so lines stay inside. fillText's maxWidth handles horizontal
    // overflow as before — both axes squish independently.
    const yScale = totalInkExtent > boxH ? boxH / totalInkExtent : 1;
    // Per-line baseline offset from the line-group's vertical center.
    // Hoisted because both the WASM-path and the fillText-squish path
    // need it; identical expression in both (review nit, Phase 3a).
    const firstBaselineLocal = -totalInkExtent / 2 + maxAscent;
    // WASM-rasterize path: closes the per-engine glyph AA + per-glyph
    // kerning divergence (Phase 1b 2026-05-14) AND the +1 bitmap-pad
    // shift (Phase 2 2026-05-14, f71734f). Gated only on (a) wasm
    // initialized, (b) font registered, (c) color parses to RGBA.
    // yScale<1 is HANDLED on the WASM path now (Phase 3a 2026-05-14):
    // drawImage's 9-arg form downscales the rasterized bitmap on Y
    // when totalInkExtent > boxH, mirroring fillText's ctx.scale(1,
    // yScale) squish without the canvas-transform incompatibility
    // that originally forced the fallback. drawImage uses the
    // browser's image-smoothing resampling (typically bilinear);
    // Rust's FS_GLYPH path uses GL_LINEAR — equivalent at the
    // single-axis-downscale operation point, so parity is preserved.
    const colorRgba = parseCssColorRgba(textColor);
    // FYS bug 8: when the text contains an emoji codepoint, fall the
    // whole block back to ctx.fillText — the WASM/fontdue path is
    // monochrome and tofus emoji, while fillText renders color emoji
    // natively (much closer to the on-glass COLR render than a tofu
    // box). A preview block with emoji can't pixel-match the Rust
    // renderer anyway, so the WASM-parity path has nothing to lose.
    const hasEmoji = lines.some((line) => EMOJI_RE.test(line));
    const useWasm = (
        colorRgba !== null
        && isWasmReady()
        && isFontRegistered(fontFamily)
        && !hasEmoji
    );

    if (useWasm) {
        // Phase 3c (2026-05-14): when yScale<1, rasterize at the
        // squished pixel-size so fontdue produces a bitmap that's
        // already at the canvas pixel-dims. Eliminates the post-
        // rasterization downscale that Phase 3a relied on, which
        // diverged from Rust's GL_LINEAR at extreme ratios.
        //
        // Phase 3d (2026-05-14): derive baselineY math from
        // EFFECTIVE-size quantities so the inter-line stride
        // matches what fontdue's per-line rasterization would
        // produce. Pre-3d used `round(fontSizePx * 1.1) * yScale`
        // for the stride (full-size round, then proportional scale)
        // while the natural stride at effective size is
        // `round(effectiveSizePx * 1.1)`. Sub-pixel rounding
        // asymmetry showed up as 1-px inter-line drift, surfacing
        // as the 229/231 max_delta floor at glyph edges. Using
        // effective-size lineHeight closes that floor for
        // multi-line content.
        //
        // For yScale=1: effectiveSizePx === fontSizePx, all
        // effective-size quantities reduce to their full-size
        // equivalents, and the formula collapses to the original
        // `firstBaselineY + i*lineHeight`. Single-line content is
        // unaffected (i*lineHeight is 0 when lines.length==1).
        //
        // Cache key is text|font|size|color and includes
        // effectiveSizePx implicitly via the size slot — different
        // squish ratios cache as separate entries (LRU 256-entry
        // capacity holds the working set comfortably).
        const effectiveSizePx = fontSizePx * yScale;
        const lineHeightEff = Math.round(effectiveSizePx * 1.1);
        const maxAscentEff = maxAscent * yScale;
        const maxDescentEff = maxDescent * yScale;
        const totalInkExtentEff =
            (maxAscentEff + maxDescentEff)
            + (lines.length - 1) * lineHeightEff;
        const firstBaselineYEff = boxCenterY - totalInkExtentEff / 2 + maxAscentEff;
        for (let i = 0; i < lines.length; i++) {
            const line = lines[i];
            if (!line) continue;
            const baselineY = firstBaselineYEff + i * lineHeightEff;
            const result = rasterizeText(line, fontFamily, effectiveSizePx, colorRgba);
            if (!result) continue; // empty / whitespace-only line.
            // Bug 4 (2026-05-19): per-line X-squish at ORIGINAL
            // fontSize scale. Pre-Bug-4 the targetW comparison used
            // result.width (Y-squished width) vs boxW, so X-squish
            // only fired for un-wrappable words that exceeded boxW
            // EVEN AFTER yScale-induced font shrink. Per spec §5.10a
            // ("both axes squish independently when both overflow")
            // each line's natural width AT ORIGINAL fontSize is the
            // right comparison. We recover natural_w_orig by
            // dividing result.width by yScale (since the rasterizer
            // ran at effectiveSizePx = fontSizePx * yScale).
            //
            // Final canvas dst_w = min(natural_w_orig, boxW). For
            // lines that fit boxW at ORIGINAL fontSize, dst_w =
            // natural_w_orig (1/yScale UPSCALE of the bitmap on X)
            // — Y stays at bitmap height. For lines that overflow,
            // dst_w = boxW (combined Y-induced + X-squish via
            // bilinear interp). The Phase 3c rasterize-at-effective
            // path is preserved for Y-axis AA quality (qa-Jimmy
            // greenlight choice (a)); the X-axis bilinear blur is
            // the acceptable trade for matching Rust's per-line
            // ndc-quad scaling without two-pass rasterization.
            const naturalWOrig = result.width / yScale;
            const targetW = Math.min(naturalWOrig, boxW);
            let drawX;
            if (textAlign === "left") drawX = boxX;
            else if (textAlign === "right") drawX = boxX + boxW - targetW;
            else drawX = boxX + (boxW - targetW) / 2;
            // result.ascent is at the effective (squished) size;
            // baselineY is at the canvas-Y of the line's baseline.
            // Subtract directly (no *yScale -- the bitmap already
            // accounts for the squish). Round to whole pixels so
            // the bitmap snaps to the canvas grid (preserves Phase 2
            // integer-X alignment).
            const drawY = Math.round(baselineY - result.ascent);
            // Always 9-arg drawImage now — even no-overflow lines
            // get the 1/yScale X-upscale applied so the rendered
            // glyph X is at NATURAL-original-size scale, NOT
            // yScale-effective scale. Pre-Bug-4 3-arg path
            // implicitly applied yScale to X via the smaller bitmap.
            ctx.drawImage(
                result.image,
                0, 0, result.width, result.height,
                Math.round(drawX), drawY, targetW, result.height,
            );
        }
    } else if (yScale === 1) {
        for (let i = 0; i < lines.length; i++) {
            ctx.fillText(lines[i], anchorX, firstBaselineY + i * lineHeight, maxWidth);
        }
    } else {
        ctx.save();
        ctx.translate(anchorX, boxCenterY);
        ctx.scale(1, yScale);
        // Draw aligned around (0,0) under the local transform; each
        // line's y-offset is from the centered origin. fillText's
        // maxWidth is in untransformed coords, so it still clamps
        // horizontal width correctly. Same baseline math, just
        // recentered around the local origin. This squish branch is
        // now only reached when WASM isn't available OR the font
        // isn't registered (Phase 3a removed the yScale==1 WASM gate).
        for (let i = 0; i < lines.length; i++) {
            ctx.fillText(lines[i], 0, firstBaselineLocal + i * lineHeight, maxWidth);
        }
        ctx.restore();
    }
}

/**
 * Insert \n at word boundaries so each line measures within
 * `maxWidth` via the current ctx.font. Preserves existing literal
 * newlines. Mirrors the backend's _wrap_text_to_width helper (B4,
 * 2026-05-05). Single words wider than maxWidth are left intact --
 * the existing fillText maxWidth + horizontal squish handles
 * overflow.
 */
function wrapTextToWidth(ctx, text, maxWidth) {
    if (!text || maxWidth <= 0) return text;
    const out = [];
    for (const paragraph of text.split(/\r?\n/)) {
        if (!paragraph) {
            out.push("");
            continue;
        }
        const words = paragraph.split(" ");
        let line = [];
        for (const word of words) {
            if (line.length === 0) {
                line.push(word);
                continue;
            }
            const candidate = line.concat(word).join(" ");
            if (ctx.measureText(candidate).width > maxWidth) {
                out.push(line.join(" "));
                line = [word];
            } else {
                line.push(word);
            }
        }
        if (line.length > 0) out.push(line.join(" "));
    }
    return out.join("\n");
}

// v1-spec-delta #10 (slice b, parity-audit follow-up 2026-05-14) —
// pure-JS mirror of Rust's `apply_brightness_gamma_rgba` at
// `renderer/src/hdmi_logic.rs:1974-1989`. Per-pixel transform
// matching the FS_BRIGHT_GAMMA shader:
//
//   rgb' = pow(clamp(rgb * brightness, 0, 1), 1.0 / max(gamma, 0.001))
//
// `brightness` is in [0, 1] (caller pre-divides the schema's
// [0, 100] value); `gamma` > 0. Alpha pass-through. Identity case
// (brightness=1, gamma=1) is a no-op; callers should skip the
// getImageData round-trip by checking before calling.
//
// Tolerance vs Rust CPU-mirror: 1 LSB per channel from float→u8
// rounding; both sides use `round` then clamp to [0, 255].
export function applyBrightnessGamma(canvas, brightness, gamma) {
    if (canvas.width <= 0 || canvas.height <= 0) return;
    const ctx = canvas.getContext("2d");
    const img = ctx.getImageData(0, 0, canvas.width, canvas.height);
    const data = img.data;
    const invGamma = 1.0 / Math.max(gamma, 0.001);
    for (let i = 0; i < data.length; i += 4) {
        for (let c = 0; c < 3; c++) {
            const v = data[i + c] / 255;
            const scaled = Math.min(1, Math.max(0, v * brightness));
            const corrected = Math.pow(scaled, invGamma);
            data[i + c] = Math.max(0, Math.min(255, Math.round(corrected * 255)));
        }
        // alpha (i+3) untouched.
    }
    ctx.putImageData(img, 0, 0);
}

/**
 * Draw the slide onto `canvas`. Pure: only reads `state` and writes
 * pixels — no DOM wiring, no event handlers.
 *
 * `opts.brightness` / `opts.gamma` (both default to 1.0 = identity)
 * apply a brightness/gamma post-pass matching Rust's FS_BRIGHT_GAMMA
 * (see `applyBrightnessGamma` above). Default identity is a no-op so
 * existing callers (parity-harness, rasterizeAtTarget, tests) keep
 * their pre-fix pixel output. The inline preview opts in with the
 * operator's configured gamma for HDMI/composite output modes; the
 * sign's schema default is 1.0 (identity) post the 2026-05-17 flip
 * from 2.2 (parity-audit #5 fix 2026-05-14).
 */
export function drawCanvas(canvas, state, opts) {
    const ctx = canvas.getContext("2d");
    const {
        backgroundColor = "#000000",
        bgSource = "color",
        bgImage = null,
        bgPattern = null,
    } = state;

    ctx.save();
    try {
        if (bgSource === "slide" && bgImage) {
            const scale = Math.max(
                canvas.width / bgImage.width,
                canvas.height / bgImage.height,
            );
            const drawW = bgImage.width * scale;
            const drawH = bgImage.height * scale;
            ctx.drawImage(
                bgImage,
                (canvas.width - drawW) / 2,
                (canvas.height - drawH) / 2,
                drawW,
                drawH,
            );
        } else if (bgSource === "pattern" && bgPattern) {
            // The bg-system.js canvas painter mirrors the backend's
            // numpy render_pattern() so editor preview = device
            // render. Tile sizes / dot radii / band spacing match.
            paintPatternOnCanvas(
                ctx, canvas.width, canvas.height,
                bgPattern.pattern, bgPattern.color_a, bgPattern.color_b,
                bgPattern.density,
            );
        } else {
            ctx.fillStyle = backgroundColor;
            ctx.fillRect(0, 0, canvas.width, canvas.height);
        }

        const layers = layersForDraw(state);
        const elapsed = opts && opts.elapsed_s;
        for (let i = 0; i < layers.length; i++) {
            const layer = layers[i];
            // §5.10a v3.1: editor's eye toggle sets visible=false;
            // skip hidden layers entirely so the rasterized PNG
            // matches what the operator sees in preview.
            if (layer?.visible === false) continue;
            const resolved = resolveLayerForDraw(layer);
            const paint = () => paintLayer(ctx, canvas, resolved);
            // Per-layer Photoshop blend mode. Canvas natively
            // supports multiply / screen / overlay (W3C compositing).
            // Map to 'source-over' for normal so the editor preview
            // matches the backend's PIL composite_with_blend math
            // (modulo small anti-aliasing differences -- same WYSIWYG
            // parity rule we hold for gradient/pattern). Only
            // save/restore when we're actually flipping the
            // compositor away from the default; the common normal
            // case skips the wrapper so drawCanvas's "exactly one
            // save/restore" invariant (relied on by tests + the
            // outer try/finally) holds.
            const blendKey = layer?.blend || "normal";
            const opacityVal = typeof layer?.opacity === "number" ? layer.opacity : 1;
            const needsBlendWrap = blendKey !== "normal";
            const needsAlphaWrap = opacityVal < 1;
            const drawOnce = () => {
                if (elapsed === undefined || elapsed === null) {
                    paint();
                } else {
                    // The motion wrapper takes the unresolved layer
                    // so it reads .motion / .motion_intensity /
                    // .motion_phase off the editor-state shape;
                    // paintLayer is called via the closure with the
                    // auto-resolved layer.
                    paintLayerWithMotion(ctx, canvas, layer, paint, {
                        elapsed_s: elapsed,
                        layerKey: `editor:${i}`,
                    });
                }
            };
            if (needsBlendWrap || needsAlphaWrap) {
                ctx.save();
                if (needsBlendWrap) {
                    ctx.globalCompositeOperation = BLEND_TO_CANVAS[blendKey];
                }
                if (needsAlphaWrap) {
                    ctx.globalAlpha = Math.max(0, Math.min(1, opacityVal));
                }
                try {
                    drawOnce();
                } finally {
                    ctx.restore();
                }
            } else {
                drawOnce();
            }
        }
    } finally {
        ctx.restore();
    }
    // Brightness/gamma post-pass. Identity (1.0, 1.0) → skip the
    // getImageData round-trip so default callers pay no cost.
    const brightness = opts && typeof opts.brightness === "number"
        ? opts.brightness : 1.0;
    const gamma = opts && typeof opts.gamma === "number" ? opts.gamma : 1.0;
    if (brightness !== 1.0 || gamma !== 1.0) {
        applyBrightnessGamma(canvas, brightness, gamma);
    }
}

function layersForDraw(state) {
    if (Array.isArray(state.layers) && state.layers.length > 0) {
        return state.layers;
    }
    // Back-compat single-layer shape: pull a synthetic layer off the
    // top-level state fields. This keeps drawCanvas usable from older
    // unit tests that pass `{text, textColor, …}` directly.
    return [
        {
            text: state.text || "",
            textColor: state.textColor,
            fontFamily: state.fontFamily,
            fontSizePct: state.fontSizePct,
            fontSize: state.fontSize,
            autoMode: state.autoMode,
            autoFormat: state.autoFormat,
            box: state.box,
        },
    ];
}

function resolveLayerForDraw(layer) {
    // Auto-mode tokens (time / date / day): the canvas shows the
    // current formatted value so the preview matches what the device
    // renders at playout. Operator's typed text is the fallback.
    const rawText = layer.text || "";
    const mode = layer.auto_mode ?? layer.autoMode ?? null;
    const fmt = layer.auto_format ?? layer.autoFormat ?? null;
    const text = mode
        ? formatAutoText(mode, fmt, new Date()) || rawText
        : rawText;
    return { ...layer, text };
}

/**
 * Heuristic fallback when neither `font_size_pct` nor `font_size_px`
 * is set on a layer. Width-relative per §5.10a v3.1.1 (qarl
 * 2026-05-01 ask #1) — matches the new pct semantic so a slide
 * without explicit sizing reads the same way the editor's "% of
 * width" field would suggest.
 */
export function pickFontSize(panelWidth) {
    return Math.max(12, Math.floor(panelWidth * 0.3));
}

// Default percent-of-width for a brand-new auto-mode-less text
// slide. 30% reads cleanly as a single-word slogan on common 4:3 /
// 16:9 panels; the operator can dial in something more specific
// from the field.
export function pickFontSizePct() {
    return 30;
}

/**
 * Render the editor scene onto a fresh offscreen 4K canvas and
 * return its base64 PNG body.
 *
 * FYS bug 7 (2026-05-20): `rotation` is the device's display_rotation
 * (0 / 90 / 180 / 270). At 90 / 270 the panel is mounted portrait, so
 * the saved asset.png must be laid out PORTRAIT — RASTERIZE_H ×
 * RASTERIZE_W — to match the portrait `--device-aspect` the dashboard
 * thumbnail tile pins. At 0 / 180 the asset.png stays landscape
 * (RASTERIZE_W × RASTERIZE_H), exactly as before. Only the offscreen
 * canvas DIMS change here; drawCanvas lays the scene out fractionally
 * (box.x/y/w/h are 0..1) so it fills whatever aspect it's handed —
 * there is no pixel-rotation transform, the operator already authored
 * the slide against a portrait editor canvas at 90/270.
 */
export function rasterizeAtTarget(state, rotation = 0) {
    const off = document.createElement("canvas");
    const portrait = rotation === 90 || rotation === 270;
    off.width = portrait ? RASTERIZE_H : RASTERIZE_W;
    off.height = portrait ? RASTERIZE_W : RASTERIZE_H;
    drawCanvas(off, state);
    return canvasToBase64(off);
}
