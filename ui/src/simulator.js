// Device simulator — standalone pop-out window (simulator.html).
// Fetches the device's output_mode from /api/settings, polls the
// MockRenderer's latest frame at /dev/preview/frame.png, and draws
// it into the canvas using the skin that matches the configured
// output_mode.
//
// The simulator is opened via window.open() from the main UI, so it
// runs as its own top-level page with no admin chrome. That keeps it
// usable on a second monitor ("walk around with your phone editing
// slides while the TV shows the sign") or in a pinned browser tab
// for QA.

import { getSettings } from "./api.js";

// Poll cadence for the frame PNG. 250ms is ~4Hz — fast enough to
// feel live, slow enough that the browser isn't hammering the
// backend. Matches the /dev/preview page's historical cadence.
const FRAME_POLL_MS = 250;

const PANEL_OUTPUT_MODES = new Set(["hub75", "ws281x", "composite"]);

/**
 * Map the device's output_mode to which draw function the simulator
 * should use. HDMI + composite share the plain skin (just scale the
 * source pixels up into the window). HUB75 gets the LED-matrix skin.
 * WS2812B gets the glow-dot skin.
 */
function pickSkin(outputMode) {
    if (outputMode === "hub75") return "hub75";
    if (outputMode === "ws281x") return "ws281x";
    return "plain"; // hdmi, composite, and any unknown mode.
}

async function boot() {
    const canvas = document.querySelector(".simulator-canvas");
    const placeholder = document.querySelector(".simulator-placeholder");
    const modeLabel = document.querySelector('[data-field="mode"]');

    // 1) Pick up the configured output_mode + dims.
    let settings;
    try {
        settings = await getSettings();
    } catch (err) {
        modeLabel.textContent = "settings unreachable";
        console.error("[simulator] /api/settings failed:", err);
        settings = {
            output_mode: "hdmi",
            display_width: 128,
            display_height: 96,
            display_rotation: 0,
        };
    }

    const sourceWidth = Number(settings.display_width) || 128;
    const sourceHeight = Number(settings.display_height) || 96;
    const rotation = Number(settings.display_rotation) || 0;
    // When the sign is rotated 90° / 270° physically, the logical
    // content is portrait — swap the canvas dims so the simulator's
    // window aspect ratio matches what the installed sign would show.
    const [signW, signH] =
        rotation === 90 || rotation === 270
            ? [sourceHeight, sourceWidth]
            : [sourceWidth, sourceHeight];
    const skin = pickSkin(settings.output_mode || "hdmi");
    modeLabel.textContent = settings.output_mode || "hdmi";

    // 2) Size the window + canvas so the sign aspect ratio is preserved.
    applyWindowSizingForMode(skin, signW, signH);
    sizeCanvasToWindow(canvas, signW, signH);
    window.addEventListener("resize", () => sizeCanvasToWindow(canvas, signW, signH));

    // 3) Start the frame poll loop.
    const ctx = canvas.getContext("2d");
    const img = new Image();
    let lastSuccessAt = 0;
    let inFlight = false;
    // Dedicated offscreen canvas for pixel sampling (hub75 + ws281x
    // skins need source-pixel access). Reused across frames but
    // resized if the MockRenderer's PNG dims differ from what
    // settings advertise (which they frequently do — the dev-mode
    // MockRenderer is pinned to 128×96 regardless of settings).
    const sampler = document.createElement("canvas");
    const samplerCtx = sampler.getContext("2d", { willReadFrequently: true });

    img.addEventListener("load", () => {
        // Use the PNG's actual natural dims for sampling — settings'
        // display_width/height drive the WINDOW aspect ratio, but the
        // content source is whatever the backend renderer wrote.
        const realW = img.naturalWidth || signW;
        const realH = img.naturalHeight || signH;
        if (sampler.width !== realW) sampler.width = realW;
        if (sampler.height !== realH) sampler.height = realH;
        samplerCtx.drawImage(img, 0, 0);
        const srcData = samplerCtx.getImageData(0, 0, realW, realH);
        drawForSkin(skin, ctx, canvas.width, canvas.height, srcData, realW, realH);
        lastSuccessAt = Date.now();
        placeholder.classList.add("hidden");
        inFlight = false;
    });
    img.addEventListener("error", () => {
        // 404 ≈ playback hasn't written a frame yet. Keep the
        // placeholder visible; no need to escalate.
        inFlight = false;
        if (Date.now() - lastSuccessAt > 2000) {
            placeholder.classList.remove("hidden");
        }
    });

    function tick() {
        if (!inFlight) {
            inFlight = true;
            // Cache-bust so the browser always goes back to the server
            // for the freshest PNG.
            img.src = `/dev/preview/frame.png?t=${Date.now()}`;
        }
    }

    tick();
    setInterval(tick, FRAME_POLL_MS);
}

/**
 * Ask the OS to size the pop-out window to the sign aspect ratio.
 * Browsers respect window.resizeTo for same-origin pop-ups. Falls
 * back silently (CSS constraints keep the canvas centered anyway).
 */
export function applyWindowSizingForMode(skin, signW, signH) {
    // Target window widths per skin — big enough to look like a
    // device, small enough to sit on a second monitor. Height
    // follows from sign aspect ratio so pixels stay square.
    const targetInner = skin === "hub75" || skin === "ws281x" ? 720 : 960;
    const aspect = signW / signH;
    const innerW = targetInner;
    const innerH = Math.round(innerW / aspect);
    try {
        window.resizeTo(innerW, innerH);
    } catch {
        // Some browsers silently ignore resizeTo on tabs (vs real
        // pop-ups); not worth escalating — the canvas max-width:100%
        // + preserved aspect handles the tab case gracefully.
    }
}

function sizeCanvasToWindow(canvas, signW, signH) {
    // Fit the canvas into the viewport preserving sign aspect ratio.
    const margin = 0; // full-bleed; the skins draw their own borders.
    const availW = window.innerWidth - margin * 2;
    const availH = window.innerHeight - margin * 2;
    const aspect = signW / signH;
    let w = availW;
    let h = w / aspect;
    if (h > availH) {
        h = availH;
        w = h * aspect;
    }
    canvas.width = Math.max(1, Math.floor(w));
    canvas.height = Math.max(1, Math.floor(h));
}

/**
 * Dispatch to the skin's draw function. Exported for vitest.
 */
export function drawForSkin(skin, ctx, w, h, srcData, signW, signH) {
    if (skin === "hub75") return drawHub75(ctx, w, h, srcData, signW, signH);
    if (skin === "ws281x") return drawWs281x(ctx, w, h, srcData, signW, signH);
    return drawPlain(ctx, w, h, srcData, signW, signH);
}

// --- skins ---

function drawPlain(ctx, w, h, srcData, signW, signH) {
    // Bare pixel-accurate scaling. HDMI + composite just show the
    // rendered frame in the available window, no chrome.
    ctx.imageSmoothingEnabled = false;
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, w, h);
    const tmp = document.createElement("canvas");
    tmp.width = signW;
    tmp.height = signH;
    tmp.getContext("2d").putImageData(srcData, 0, 0);
    ctx.drawImage(tmp, 0, 0, w, h);
}

function drawHub75(ctx, w, h, srcData, signW, signH) {
    // LED-matrix panel look. Dark body, each source pixel as a
    // filled square with a visible gap between cells.
    ctx.fillStyle = "#0a0a0a";
    ctx.fillRect(0, 0, w, h);

    const cellW = w / signW;
    const cellH = h / signH;
    const gap = 0.18; // fraction of cell size that's inter-pixel gap
    const drawW = Math.max(1, cellW * (1 - gap));
    const drawH = Math.max(1, cellH * (1 - gap));
    const offX = (cellW - drawW) / 2;
    const offY = (cellH - drawH) / 2;

    const pixels = srcData.data;
    for (let y = 0; y < signH; y++) {
        for (let x = 0; x < signW; x++) {
            const i = (y * signW + x) * 4;
            const r = pixels[i];
            const g = pixels[i + 1];
            const b = pixels[i + 2];
            // Very-dark cells still show as dim squares so the
            // panel grid is visible even on black content — the
            // "off LED" ambience is part of what makes it read as
            // a panel.
            const dim = r + g + b < 18;
            ctx.fillStyle = dim
                ? "#111113"
                : `rgb(${r},${g},${b})`;
            ctx.fillRect(x * cellW + offX, y * cellH + offY, drawW, drawH);
        }
    }
}

function drawWs281x(ctx, w, h, srcData, signW, signH) {
    // Addressable-strip look. Near-black background, each source
    // pixel as a glowing circle with a soft radial halo.
    ctx.fillStyle = "#050505";
    ctx.fillRect(0, 0, w, h);

    const cellW = w / signW;
    const cellH = h / signH;
    const cell = Math.min(cellW, cellH);
    const coreR = cell * 0.28;
    const glowR = cell * 0.55;

    const pixels = srcData.data;
    for (let y = 0; y < signH; y++) {
        for (let x = 0; x < signW; x++) {
            const i = (y * signW + x) * 4;
            const r = pixels[i];
            const g = pixels[i + 1];
            const b = pixels[i + 2];
            const cx = x * cellW + cellW / 2;
            const cy = y * cellH + cellH / 2;

            // Draw off-LEDs as faint dim dots so the strip geometry
            // is always visible.
            if (r + g + b < 18) {
                ctx.fillStyle = "#0f0f12";
                ctx.beginPath();
                ctx.arc(cx, cy, coreR, 0, Math.PI * 2);
                ctx.fill();
                continue;
            }

            // Glow halo.
            const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, glowR);
            grad.addColorStop(0, `rgba(${r},${g},${b},0.75)`);
            grad.addColorStop(1, `rgba(${r},${g},${b},0)`);
            ctx.fillStyle = grad;
            ctx.fillRect(cx - glowR, cy - glowR, glowR * 2, glowR * 2);

            // LED core.
            ctx.fillStyle = `rgb(${r},${g},${b})`;
            ctx.beginPath();
            ctx.arc(cx, cy, coreR, 0, Math.PI * 2);
            ctx.fill();
        }
    }
}

if (typeof window !== "undefined") {
    window.addEventListener("DOMContentLoaded", boot);
}

// Exports for tests.
export { drawHub75, drawPlain, drawWs281x, pickSkin };
