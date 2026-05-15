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

const RASTERIZE_W = 3840;
const RASTERIZE_H = 2160;

// Map TextLayer.blend -> Canvas2D globalCompositeOperation so the
// editor preview matches the backend's PIL composite_with_blend
// math (W3C compositing spec: multiply/screen/overlay are first-
// class canvas modes, parity with backend modulo anti-aliasing).
// Keys MUST stay in sync with TextLayer.blend's Literal in
// content/__init__.py and _BLEND_MODES in rendering/blend.py.
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
            const paint = () => paintLayer(ctx, canvas, layer, /* fillBox */ null);
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
    ctx.textBaseline = "middle";

    // Word-wrap (B4, 2026-05-05): any paragraph longer than the box
    // width gets broken at word boundaries onto multiple lines. Pre-
    // existing literal newlines are preserved.
    const wrapped = wrapTextToWidth(ctx, text, boxW);
    const lines = wrapped.split(/\r?\n/);
    const lineHeight = fontSizePx * 1.1;
    const totalHeight = lineHeight * lines.length;
    const boxCenterY = boxY + boxH / 2;
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
    const yScale = totalHeight > boxH ? boxH / totalHeight : 1;
    if (yScale === 1) {
        const startY = boxCenterY - totalHeight / 2 + lineHeight / 2;
        for (let i = 0; i < lines.length; i++) {
            ctx.fillText(lines[i], anchorX, startY + i * lineHeight, maxWidth);
        }
    } else {
        ctx.save();
        ctx.translate(anchorX, boxCenterY);
        ctx.scale(1, yScale);
        // Draw aligned around (0,0) under the local transform; each
        // line's y-offset is from the centered origin. fillText's
        // maxWidth is in untransformed coords, so it still clamps
        // horizontal width correctly.
        const lineY0 = -totalHeight / 2 + lineHeight / 2;
        for (let i = 0; i < lines.length; i++) {
            ctx.fillText(lines[i], 0, lineY0 + i * lineHeight, maxWidth);
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
 * their pre-fix pixel output. The inline preview opts in with
 * `gamma=2.2` for HDMI/composite output modes to match the deployed
 * sign's default gamma encoding (parity-audit #5 fix 2026-05-14).
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
 */
export function rasterizeAtTarget(state) {
    const off = document.createElement("canvas");
    off.width = RASTERIZE_W;
    off.height = RASTERIZE_H;
    drawCanvas(off, state);
    return canvasToBase64(off);
}
