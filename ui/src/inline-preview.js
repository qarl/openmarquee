// Inline preview — client-side playback simulator embedded in the
// Playlists panel. Replaces the old live-preview widget (which mirrored
// the backend's current slide) AND the simulator pop-out window. The
// backend's playback loop is now a hardware-always-running autonomous
// process; this surface is a parallel simulator the operator drives
// with its own play / pause / scrub, styled with device-appropriate
// skin chrome (HUB75 matrix / WS2812B glow dots / plain HDMI /
// composite).
//
// Rendering model:
//
// - Each content item has a [start, end) range computed from its
//   duration; `position` is seconds-from-start. `findActiveItem(position)`
//   picks which item to draw.
// - text_slide / image → a cached `<img>` loaded
//   from /api/content/{id}/asset is drawn onto the canvas through
//   the skin draw fn.
// - h264 video → a hidden `<video>` element seeks to
//   `position - item.start` and drawImage pulls the current frame.
// - text_slide with auto_mode → after the image is drawn, an
//   overlaid <div> renders the current time/date/day (same
//   formatAutoText used by the old live-preview).
// - Transition "fade" between items: linear cross-fade over
//   transition_ms at the boundary. "cut" is instant.

import { formatAutoText } from "./auto-format.js";
import { drawTextOnly } from "./editor.js";

const TEMPLATE = `
    <section class="inline-preview" aria-label="playlist preview">
        <div class="inline-preview-stage">
            <canvas class="inline-preview-canvas" aria-hidden="true"></canvas>
            <div class="inline-preview-auto-text" hidden></div>
            <div class="inline-preview-idle">
                <span>Add slides to the playlist, then press play.</span>
            </div>
        </div>
        <div class="inline-preview-transport">
            <button type="button" class="inline-preview-play"
                    aria-label="play or pause">▶</button>
            <input type="range" class="inline-preview-scrub"
                   min="0" max="0" step="0.1" value="0"
                   aria-label="playback position">
            <span class="inline-preview-time">0:00 / 0:00</span>
        </div>
    </section>
`;

const PANEL_OUTPUT_MODES = new Set(["hub75", "ws281x", "composite"]);

function pickSkin(outputMode) {
    if (outputMode === "hub75") return "hub75";
    if (outputMode === "ws281x") return "ws281x";
    return "plain"; // hdmi + composite + unknown
}

/**
 * Mount the inline preview.
 *
 * @param {HTMLElement} container — parent (emptied + replaced).
 * @param {object} options
 * @param {number} options.width — sign width in pixels.
 * @param {number} options.height — sign height in pixels.
 * @param {string} [options.outputMode] — "hdmi" | "hub75" | "ws281x" | "composite"
 * @param {() => Promise<{items: Array}>} options.fetchPlaylist
 *     Returns the default playlist (with resolved content items).
 *     Shape: { items: [{ item_id, transition, transition_ms, content: {...} }] }
 * @returns {{ refresh: () => Promise<void>, stop: () => void }}
 */
export function mountInlinePreview(container, options) {
    const { width, height, outputMode = "hdmi", fetchPlaylist } = options;
    container.innerHTML = TEMPLATE;

    const stage = container.querySelector(".inline-preview-stage");
    const canvas = container.querySelector(".inline-preview-canvas");
    const autoText = container.querySelector(".inline-preview-auto-text");
    const idle = container.querySelector(".inline-preview-idle");
    const playBtn = container.querySelector(".inline-preview-play");
    const slider = container.querySelector(".inline-preview-scrub");
    const timeEl = container.querySelector(".inline-preview-time");

    stage.style.aspectRatio = `${width} / ${height}`;
    const skin = pickSkin(outputMode);

    // Playlist → ranged timeline. Each entry is { item, startSec, endSec,
    // transition, transition_ms }.
    let timeline = [];
    let totalSec = 0;
    let position = 0;
    let playing = false;
    let rafId = null;
    let lastTick = null;
    let stopped = false;

    // Asset caches. Image entries are HTMLImageElement; video entries
    // are HTMLVideoElement. Loaded lazily on first draw. Evicted
    // wholesale at refresh() — an edit-save on any referenced slide
    // means the bytes may have changed, and keying by id would otherwise
    // serve stale pixels from a cached <img>.
    let imageCache = new Map();
    let videoCache = new Map();
    // Bumped on every refresh() so `?v=${refreshVersion}` forces the
    // browser HTTP cache to refetch after a slide edit.
    let refreshVersion = 0;
    // Tracks which video element is currently the "active" one — i.e.
    // the one driving the slot we're showing. We let it play in real
    // time and just sample drawImage() each raf tick. Seeking is reserved
    // for scrub / wrap-around / slot transitions because exact-seek on
    // <video> is slow and the per-frame drift threshold from the old
    // implementation pinned the visible fps at ~5.
    let activeVideoId = null;
    // Offscreen sampler for reading the image's pixel data (hub75 +
    // ws281x skins need per-pixel access).
    const sampler = document.createElement("canvas");
    const samplerCtx = sampler.getContext("2d", { willReadFrequently: true });
    // Lazily-created scratch canvas for rasterizing the text overlay of a
    // Text-over-Video slide. Sized to the bg video's native resolution so
    // the text scales the same way the device will scale it. The raster
    // is slot-bound: once filled for a slide, it's reused on every rAF
    // tick (text doesn't change between frames, only the video does).
    // textOverlayKey fingerprints the cached raster's slide so a slot
    // change forces re-rasterization; refresh() clears it on edit-save.
    let textOverlay = null;
    let textOverlayKey = null;

    async function refresh() {
        if (stopped) return;
        let playlist;
        try {
            playlist = await fetchPlaylist();
        } catch (err) {
            console.error("[inline-preview] fetchPlaylist failed:", err);
            return;
        }
        // Evict in-memory caches so a post-save refresh paints fresh
        // bytes. Pause + drop any videos too — their .src gets a new
        // cache-bust on the next getCachedVideo call.
        for (const v of videoCache.values()) v.pause?.();
        imageCache = new Map();
        videoCache = new Map();
        activeVideoId = null;
        textOverlayKey = null;
        refreshVersion += 1;

        timeline = buildTimeline(playlist?.items || []);
        totalSec = timeline.length > 0 ? timeline[timeline.length - 1].endSec : 0;
        slider.max = String(totalSec.toFixed(2));
        if (position > totalSec) position = 0;
        idle.hidden = timeline.length > 0;
        renderOnce();
    }

    function buildTimeline(items) {
        const out = [];
        let cursor = 0;
        for (const entry of items) {
            const content = entry.content;
            if (!content) continue; // stale id
            const duration = Math.max(0.1, (content.duration_ms || 5000) / 1000);
            out.push({
                item: content,
                transition: entry.transition || "cut",
                transition_ms: Number(entry.transition_ms) || 0,
                startSec: cursor,
                endSec: cursor + duration,
            });
            cursor += duration;
        }
        return out;
    }

    function findActiveIndex(pos) {
        if (timeline.length === 0) return -1;
        for (let i = 0; i < timeline.length; i++) {
            if (pos < timeline[i].endSec) return i;
        }
        return timeline.length - 1;
    }

    function renderOnce() {
        if (!sizeCanvasToStage()) return;
        // Always clear with the skin's expected backdrop first so an
        // unloaded image (drawSlot bails) leaves something coherent
        // on screen instead of stale or empty pixels.
        clearCanvas();
        if (timeline.length === 0) {
            autoText.hidden = true;
            return;
        }
        const idx = findActiveIndex(position);
        if (idx < 0) return;
        const slot = timeline[idx];
        drawSlot(slot);
        // Transition into the next slot if we're within its window.
        // The "next slot" wraps modulo timeline length so the last
        // slide's transition honors its setting when cycling back to
        // the first — the backend playback loop already wraps, matching
        // it here keeps preview and device in sync.
        const timeInto = position - slot.startSec;
        const timeLeft = slot.endSec - position;
        const ANIMATED = new Set([
            "fade",
            "wipe",
            "slide",
            "iris",
            "scroll",
            "flip",
            "marquee",
            "dissolve",
            "pixelate",
            "halftone",
            "scanline",
            "glitch",
            "push",
            "blinds",
            "shutter",
        ]);
        const fadeSec = ANIMATED.has(slot.transition)
            ? slot.transition_ms / 1000
            : 0;
        if (fadeSec > 0 && timeLeft < fadeSec && timeline.length > 1) {
            const nextIdx = (idx + 1) % timeline.length;
            const progress = 1 - timeLeft / fadeSec; // 0 → 1 through window
            const ctx = canvas.getContext("2d");
            if (slot.transition === "fade") {
                ctx.globalAlpha = progress;
                drawSlot(timeline[nextIdx]);
                ctx.globalAlpha = 1;
            } else if (slot.transition === "wipe") {
                // Wipe left→right — clip to a growing left strip.
                ctx.save();
                ctx.beginPath();
                ctx.rect(0, 0, canvas.width * progress, canvas.height);
                ctx.clip();
                drawSlot(timeline[nextIdx]);
                ctx.restore();
            } else if (slot.transition === "slide") {
                // Both frames move: from-image shifts left, to-image
                // enters from the right at the same offset.
                const ox = canvas.width * progress;
                ctx.save();
                ctx.translate(-ox, 0);
                drawSlot(slot);
                ctx.restore();
                ctx.save();
                ctx.translate(canvas.width - ox, 0);
                drawSlot(timeline[nextIdx]);
                ctx.restore();
            } else if (slot.transition === "iris") {
                // Circular reveal of next slot from canvas center.
                const cx = canvas.width / 2;
                const cy = canvas.height / 2;
                const maxR = Math.hypot(cx, cy);
                ctx.save();
                ctx.beginPath();
                ctx.arc(cx, cy, maxR * progress, 0, Math.PI * 2);
                ctx.clip();
                drawSlot(timeline[nextIdx]);
                ctx.restore();
            } else if (slot.transition === "scroll") {
                // Vertical scroll: from-image rolls up off the top,
                // to-image rolls in from below at the same rate.
                // Mirrors playback.py::_scroll on the device.
                const oy = canvas.height * progress;
                ctx.save();
                ctx.translate(0, -oy);
                drawSlot(slot);
                ctx.restore();
                ctx.save();
                ctx.translate(0, canvas.height - oy);
                drawSlot(timeline[nextIdx]);
                ctx.restore();
            } else if (slot.transition === "dissolve") {
                // Per-pixel random reveal. Threshold matrix generated
                // once per transition (cached on the slot) so the
                // dissolve pattern is stable across frames — same
                // contract as playback.py::_dissolve, which seeds its
                // numpy thresholds once per call.
                const total = canvas.width * canvas.height;
                if (
                    !slot._dissolveThresholds ||
                    slot._dissolveThresholds.length !== total
                ) {
                    const thr = new Uint8Array(total);
                    for (let i = 0; i < total; i++) {
                        thr[i] = Math.floor(Math.random() * 256);
                    }
                    slot._dissolveThresholds = thr;
                }
                // drawSlot(slot) was painted above — capture from-slot
                // pixels before we overwrite, then redraw to-slot, then
                // restore from-slot pixels for cells whose threshold
                // hasn't yet been crossed.
                const fromImg = ctx.getImageData(
                    0,
                    0,
                    canvas.width,
                    canvas.height,
                );
                drawSlot(timeline[nextIdx]);
                const composed = ctx.getImageData(
                    0,
                    0,
                    canvas.width,
                    canvas.height,
                );
                const thr = slot._dissolveThresholds;
                const progressByte = Math.floor(progress * 256);
                for (let i = 0; i < total; i++) {
                    if (thr[i] >= progressByte) {
                        const off = i * 4;
                        composed.data[off] = fromImg.data[off];
                        composed.data[off + 1] = fromImg.data[off + 1];
                        composed.data[off + 2] = fromImg.data[off + 2];
                    }
                }
                ctx.putImageData(composed, 0, 0);
            } else if (slot.transition === "pixelate") {
                // Chunky-pixel cross-fade. Both slots pixelate to a
                // peak block size at progress=0.5 then sharpen back to
                // native resolution as the alpha-blend lands. Mirrors
                // playback.py::_pixelate: triangular block-size,
                // linear cross-fade.
                const w = canvas.width;
                const h = canvas.height;
                if (w < 2 || h < 2) {
                    // Strip fallback — same shape the server uses.
                    ctx.globalAlpha = progress;
                    drawSlot(timeline[nextIdx]);
                    ctx.globalAlpha = 1;
                } else {
                    const maxBlock = Math.max(2, Math.floor(Math.min(w, h) / 4));
                    const triangular = 1 - Math.abs(2 * progress - 1);
                    const blockSize = Math.max(
                        1,
                        Math.round(1 + triangular * (maxBlock - 1)),
                    );
                    // Capture from-slot (drawSlot(slot) above pre-painted),
                    // draw to-slot, capture, then build the pixelated +
                    // blended composite manually. Manual is cheaper than
                    // an offscreen <canvas> per frame and keeps the
                    // smoothing-flag dance off the main ctx.
                    const fromImg = ctx.getImageData(0, 0, w, h);
                    drawSlot(timeline[nextIdx]);
                    const toImg = ctx.getImageData(0, 0, w, h);
                    const out = ctx.createImageData(w, h);
                    const oneMinusP = 1 - progress;
                    for (let y = 0; y < h; y++) {
                        const by = Math.floor(y / blockSize) * blockSize;
                        for (let x = 0; x < w; x++) {
                            const bx = Math.floor(x / blockSize) * blockSize;
                            const srcOff = (by * w + bx) * 4;
                            const dstOff = (y * w + x) * 4;
                            // Pixel-block sample from each side, then blend.
                            out.data[dstOff] =
                                fromImg.data[srcOff] * oneMinusP +
                                toImg.data[srcOff] * progress;
                            out.data[dstOff + 1] =
                                fromImg.data[srcOff + 1] * oneMinusP +
                                toImg.data[srcOff + 1] * progress;
                            out.data[dstOff + 2] =
                                fromImg.data[srcOff + 2] * oneMinusP +
                                toImg.data[srcOff + 2] * progress;
                            out.data[dstOff + 3] = 255;
                        }
                    }
                    ctx.putImageData(out, 0, 0);
                }
            } else if (slot.transition === "halftone") {
                // Halftone-dot reveal. Mirrors playback.py::_halftone:
                // a regular grid of cell centers, each with a circle
                // whose radius grows 0 → max_r as progress 0 → 1.
                // Inside any circle = to-slot pixels; outside = the
                // un-overwritten from-slot the outer renderer left.
                const w = canvas.width;
                const h = canvas.height;
                if (w < 4 || h < 4) {
                    ctx.globalAlpha = progress;
                    drawSlot(timeline[nextIdx]);
                    ctx.globalAlpha = 1;
                } else {
                    const pitch = Math.max(2, Math.floor(Math.min(w, h) / 8));
                    const maxR = Math.round(pitch * 0.71) + 1;
                    const radius = Math.round(progress * maxR);
                    ctx.save();
                    ctx.beginPath();
                    for (let cy = pitch / 2; cy < h; cy += pitch) {
                        for (let cx = pitch / 2; cx < w; cx += pitch) {
                            // moveTo before each arc so adjacent arcs
                            // don't get connected by an implicit line —
                            // would silently fill the rectangle between
                            // them.
                            ctx.moveTo(cx + radius, cy);
                            ctx.arc(cx, cy, radius, 0, Math.PI * 2);
                        }
                    }
                    ctx.clip();
                    drawSlot(timeline[nextIdx]);
                    ctx.restore();
                }
            } else if (slot.transition === "scanline") {
                // CRT scanline sweep. Bright band top-to-bottom over
                // the transition; above the band is to-slot, below
                // stays from-slot (already pre-painted by the outer
                // renderer). Mirrors playback.py::_scanline.
                const w = canvas.width;
                const h = canvas.height;
                if (h < 2) {
                    // Strip fallback — same shape the server uses.
                    ctx.globalAlpha = progress;
                    drawSlot(timeline[nextIdx]);
                    ctx.globalAlpha = 1;
                } else {
                    const bandHeight = Math.max(1, Math.floor(h / 32));
                    const sweepY = Math.round(progress * h);
                    // Reveal to-slot above the sweep via clip rect.
                    if (sweepY > 0) {
                        ctx.save();
                        ctx.beginPath();
                        ctx.rect(0, 0, w, sweepY);
                        ctx.clip();
                        drawSlot(timeline[nextIdx]);
                        ctx.restore();
                    }
                    // Bright glow band centered on the sweep, clamped
                    // so it doesn't extend past the canvas edges.
                    const bandTop = Math.max(0, sweepY - Math.floor(bandHeight / 2));
                    const bandBot = Math.min(h, bandTop + bandHeight);
                    if (bandBot > bandTop) {
                        ctx.fillStyle = "#fff";
                        ctx.fillRect(0, bandTop, w, bandBot - bandTop);
                    }
                }
            } else if (slot.transition === "glitch") {
                // Digital-corruption transition: per-row x-jitter +
                // cross-fade + cyan tear rows. Mirrors playback.py::
                // _glitch. Per-frame randomness (jitter + tear-row
                // positions regenerated each frame, NOT cached on the
                // slot like dissolve's thresholds) is what makes the
                // breakage read as alive — a static glitch reads as
                // intentional, an animated glitch reads as broken.
                const w = canvas.width;
                const h = canvas.height;
                const maxJitter = Math.max(1, Math.floor(w / 10));
                const nTears = Math.max(1, Math.floor(h / 20));
                // Capture from-slot (outer renderer pre-painted), draw
                // to-slot, capture, then build the jittered + blended
                // composite manually. Same shape as the dissolve and
                // pixelate branches.
                const fromImg = ctx.getImageData(0, 0, w, h);
                drawSlot(timeline[nextIdx]);
                const toImg = ctx.getImageData(0, 0, w, h);
                const out = ctx.createImageData(w, h);
                const oneMinusP = 1 - progress;
                for (let y = 0; y < h; y++) {
                    const shift =
                        Math.floor(Math.random() * (2 * maxJitter + 1)) - maxJitter;
                    for (let x = 0; x < w; x++) {
                        // np.roll equivalent — wrap around. (x - shift)
                        // mod w. JS % can return negatives, so the
                        // explicit w-and-mod chain.
                        const fromX = ((x - shift) % w + w) % w;
                        const fromOff = (y * w + fromX) * 4;
                        const toOff = (y * w + x) * 4;
                        const dstOff = (y * w + x) * 4;
                        out.data[dstOff] =
                            fromImg.data[fromOff] * oneMinusP +
                            toImg.data[toOff] * progress;
                        out.data[dstOff + 1] =
                            fromImg.data[fromOff + 1] * oneMinusP +
                            toImg.data[toOff + 1] * progress;
                        out.data[dstOff + 2] =
                            fromImg.data[fromOff + 2] * oneMinusP +
                            toImg.data[toOff + 2] * progress;
                        out.data[dstOff + 3] = 255;
                    }
                }
                // Tear rows — overwrite a few random rows with cyan.
                const tearYs = new Set();
                while (tearYs.size < Math.min(nTears, h)) {
                    tearYs.add(Math.floor(Math.random() * h));
                }
                for (const ty of tearYs) {
                    for (let x = 0; x < w; x++) {
                        const dstOff = (ty * w + x) * 4;
                        out.data[dstOff] = 0;
                        out.data[dstOff + 1] = 255;
                        out.data[dstOff + 2] = 255;
                        out.data[dstOff + 3] = 255;
                    }
                }
                ctx.putImageData(out, 0, 0);
            } else if (slot.transition === "push") {
                // Classic-display push: to-slot enters from the LEFT,
                // pushing from-slot off the right edge. Both move
                // together at the same rate. 1-px bright vertical
                // separator at the seam = the projector-blade feel
                // that distinguishes push from slide. Mirrors
                // playback.py::_push.
                const w = canvas.width;
                const h = canvas.height;
                if (w < 2) {
                    ctx.globalAlpha = progress;
                    drawSlot(timeline[nextIdx]);
                    ctx.globalAlpha = 1;
                } else {
                    const offset = Math.round(progress * w);
                    ctx.clearRect(0, 0, w, h);
                    // to-slot translated to x = -w + offset (its right
                    // `offset` columns visible at frame [0, offset)).
                    ctx.save();
                    ctx.translate(-w + offset, 0);
                    drawSlot(timeline[nextIdx]);
                    ctx.restore();
                    // from-slot translated to x = +offset (its left
                    // `w - offset` columns visible at frame [offset, w)).
                    ctx.save();
                    ctx.translate(offset, 0);
                    drawSlot(slot);
                    ctx.restore();
                    // Bright projector-blade separator at the seam.
                    if (offset > 0 && offset < w) {
                        ctx.fillStyle = "#fff";
                        ctx.fillRect(offset - 1, 0, 1, h);
                    }
                }
            } else if (slot.transition === "blinds") {
                // Venetian-blind reveal. Multi-slat clip path —
                // one rect per slat, growing from each slat's
                // midline outward. Mirrors playback.py::_blinds.
                const w = canvas.width;
                const h = canvas.height;
                if (h < 4) {
                    ctx.globalAlpha = progress;
                    drawSlot(timeline[nextIdx]);
                    ctx.globalAlpha = 1;
                } else {
                    const nSlats = Math.max(2, Math.floor(h / 8));
                    const slatH = h / nSlats;
                    ctx.save();
                    ctx.beginPath();
                    for (let s = 0; s < nSlats; s++) {
                        const slatTop = Math.round(s * slatH);
                        const slatBot = Math.round((s + 1) * slatH);
                        const slatHeight = slatBot - slatTop;
                        const bandHeight = Math.round(slatHeight * progress);
                        if (bandHeight <= 0) continue;
                        const bandTop =
                            slatTop + Math.floor((slatHeight - bandHeight) / 2);
                        ctx.rect(0, bandTop, w, bandHeight);
                    }
                    ctx.clip();
                    drawSlot(timeline[nextIdx]);
                    ctx.restore();
                }
            } else if (slot.transition === "shutter") {
                // Hexagonal-aperture shutter: 6-vertex polygon centered
                // on the canvas grows from 0 to canvas-covering radius.
                // Distinct from iris (circle) by the polygon's straight
                // edges. Mirrors playback.py::_shutter.
                const w = canvas.width;
                const h = canvas.height;
                if (w < 4 || h < 4) {
                    ctx.globalAlpha = progress;
                    drawSlot(timeline[nextIdx]);
                    ctx.globalAlpha = 1;
                } else {
                    const cx = w / 2;
                    const cy = h / 2;
                    // Inflate vertex radius by 1/cos(π/6) so the hexagon's
                    // narrowest direction (edge midpoint, ~0.866R) still
                    // reaches the canvas corner at progress=1.
                    const maxR = (Math.hypot(cx, cy) + 2) / Math.cos(Math.PI / 6);
                    const radius = progress * maxR;
                    ctx.save();
                    ctx.beginPath();
                    for (let k = 0; k < 6; k++) {
                        const a = (Math.PI / 3) * k;
                        const x = cx + radius * Math.cos(a);
                        const y = cy + radius * Math.sin(a);
                        if (k === 0) ctx.moveTo(x, y);
                        else ctx.lineTo(x, y);
                    }
                    ctx.closePath();
                    ctx.clip();
                    drawSlot(timeline[nextIdx]);
                    ctx.restore();
                }
            } else if (slot.transition === "marquee") {
                // Tickertape: from-slot scrolls off to the left, a
                // gap with a centered dot (the "·" separator) passes
                // through, then to-slot arrives from the right. Same
                // [from | gap | to] compound idea as playback.py::_marquee
                // — built here as three consecutive draws under a
                // shared -offset translate.
                const gapW = Math.max(4, Math.floor(canvas.width / 8));
                const scrollTotal = canvas.width + gapW;
                const offset = scrollTotal * progress;
                ctx.clearRect(0, 0, canvas.width, canvas.height);
                ctx.save();
                ctx.translate(-offset, 0);
                drawSlot(slot);
                // Gap panel: black field with a centered white dot.
                ctx.fillStyle = "#000";
                ctx.fillRect(canvas.width, 0, gapW, canvas.height);
                ctx.fillStyle = "#fff";
                const dotR = Math.max(
                    1,
                    Math.min(Math.floor(gapW / 3), Math.floor(canvas.height / 3)),
                );
                ctx.beginPath();
                ctx.arc(
                    canvas.width + gapW / 2,
                    canvas.height / 2,
                    dotR,
                    0,
                    Math.PI * 2,
                );
                ctx.fill();
                // To-slot at canvas.width + gapW (in pre-translate
                // coords). drawSlot paints at (0, 0) → (w, h), so
                // translate first, then draw, then we're done.
                ctx.translate(canvas.width + gapW, 0);
                drawSlot(timeline[nextIdx]);
                ctx.restore();
            } else if (slot.transition === "flip") {
                // Card-flip: from squishes to center column over [0,
                // 0.5], then to grows from center column over [0.5,
                // 1]. 2D approximation of a 3D card-flip; mirrors
                // playback.py::_flip. Strip-fallback (width<2 = fade)
                // lives on the server; the preview canvas is always
                // wider than 1px so the live flip path works here
                // directly.
                //
                // Clear the canvas first because the drawSlot(slot)
                // call above this transition block painted the full
                // from-image at (0,0). Without the clear, the squished
                // card sits on top of the un-squished original instead
                // of carving silhouette space around it.
                let scale;
                let drawTarget;
                if (progress < 0.5) {
                    scale = 1 - 2 * progress;
                    drawTarget = slot;
                } else {
                    scale = 2 * progress - 1;
                    drawTarget = timeline[nextIdx];
                }
                ctx.clearRect(0, 0, canvas.width, canvas.height);
                ctx.save();
                ctx.translate(canvas.width / 2, 0);
                ctx.scale(scale, 1);
                ctx.translate(-canvas.width / 2, 0);
                drawSlot(drawTarget);
                ctx.restore();
            }
        }
        updateAutoOverlay(slot);
        // Silence unused for now.
        void timeInto;
    }

    function drawSlot(slot) {
        const item = slot.item;
        if (item.type === "video") {
            syncActiveVideo(item.id, slot);
            drawVideo(item, slot);
        } else if (item.type === "text_slide" && item.background_video_slide_id) {
            // Phase 5b — Text over Video. The slide carries a reference to
            // a saved VideoSlide; the inline preview composites the slide's
            // text on top of the live video frames in the browser. The
            // device's playback engine does the same per SYSTEM_SPEC §5.10
            // (deferred to Phase 5c — VideoSlide playback substrate isn't
            // implemented yet; live-preview still works because it's just
            // browser canvas blitting, no Pi pipeline involved).
            const bgId = String(item.background_video_slide_id);
            syncActiveVideo(bgId, slot);
            drawTextOverVideo(item, bgId);
        } else {
            // Switched away from a playing video — pause it so audio-less
            // h264 doesn't keep decoding off-screen.
            pauseAllVideosExcept(null);
            activeVideoId = null;
            drawImage(item);
        }
    }

    function syncActiveVideo(videoId, slot) {
        const video = getCachedVideo(videoId);
        const offsetInto = position - slot.startSec;
        const isNewActive = activeVideoId !== videoId;
        if (isNewActive) {
            // Slot just changed. Pause every other video, point this one
            // at the slot offset, and (if we're playing) let it run.
            pauseAllVideosExcept(videoId);
            activeVideoId = videoId;
            try {
                video.currentTime = Math.max(0, offsetInto);
            } catch {
                // Video isn't ready yet; loadeddata listener will retry.
            }
            if (playing) video.play?.().catch(() => {});
            return;
        }
        // Same active video. Only re-seek on a big drift — this catches
        // the operator scrubbing the slider, or the playlist clock
        // wrapping back to 0 at totalSec. The threshold is loose
        // (1 second) so steady playback never seeks at all.
        if (Math.abs((video.currentTime || 0) - offsetInto) > 1.0) {
            try {
                video.currentTime = Math.max(0, offsetInto);
            } catch {
                // ignore
            }
        }
        // Mid-slot play/pause toggles can drift the video element out
        // of sync with the playing flag (e.g. the user pressed play
        // before this video became active). Reconcile each tick.
        if (playing && video.paused) {
            video.play?.().catch(() => {});
        } else if (!playing && !video.paused) {
            video.pause?.();
        }
    }

    function pauseAllVideosExcept(keepId) {
        for (const [id, v] of videoCache) {
            if (id !== keepId) v.pause?.();
        }
    }

    function drawImage(item) {
        const img = getCachedImage(item);
        if (!img.complete || img.naturalWidth === 0) {
            // Not ready yet; leave whatever was drawn last.
            return;
        }
        const ctx = canvas.getContext("2d");
        const srcW = img.naturalWidth;
        const srcH = img.naturalHeight;
        if (sampler.width !== srcW) sampler.width = srcW;
        if (sampler.height !== srcH) sampler.height = srcH;
        samplerCtx.drawImage(img, 0, 0);
        const srcData = samplerCtx.getImageData(0, 0, srcW, srcH);
        drawForSkin(skin, ctx, canvas.width, canvas.height, srcData, srcW, srcH);
    }

    function drawVideo(item) {
        const video = getCachedVideo(item.id);
        if (video.readyState < 2 || !video.videoWidth) return;
        const ctx = canvas.getContext("2d");
        const srcW = video.videoWidth;
        const srcH = video.videoHeight;
        if (sampler.width !== srcW) sampler.width = srcW;
        if (sampler.height !== srcH) sampler.height = srcH;
        samplerCtx.drawImage(video, 0, 0);
        const srcData = samplerCtx.getImageData(0, 0, srcW, srcH);
        drawForSkin(skin, ctx, canvas.width, canvas.height, srcData, srcW, srcH);
    }

    /**
     * Phase 5b — Text-over-Video composite. Sample the bg video frame,
     * rasterize the slide's text on top with a transparent overlay
     * canvas, then push both through the skin pipeline as a single
     * frame. Mirrors what the device-side compositor will do at
     * playback (per §5.10) — same byte order, same scale logic — so a
     * future Phase 5c that wires up the Pi pipeline just needs to copy
     * the math, not the look.
     */
    function drawTextOverVideo(item, videoId) {
        const video = getCachedVideo(videoId);
        if (video.readyState < 2 || !video.videoWidth) return;
        const ctx = canvas.getContext("2d");
        const srcW = video.videoWidth;
        const srcH = video.videoHeight;
        if (sampler.width !== srcW) sampler.width = srcW;
        if (sampler.height !== srcH) sampler.height = srcH;
        // Layer 1: video frame fills the sampler.
        samplerCtx.drawImage(video, 0, 0);
        // Layer 2: text rasterized at the same resolution onto a separate
        // canvas (transparent bg), then drawn over the video frame. Cache
        // the raster across rAF ticks within a slot — text is fixed for
        // the slide's lifetime, only the bg video frame changes per tick.
        // Slot exit (operator scrubs to a different slide) or edit-save
        // (refresh() clears the key) triggers a fresh rasterize.
        if (!textOverlay) {
            textOverlay = document.createElement("canvas");
        }
        const sizeChanged =
            textOverlay.width !== srcW || textOverlay.height !== srcH;
        if (sizeChanged) {
            textOverlay.width = srcW;
            textOverlay.height = srcH;
        }
        const key = `${item.id}|${item.text || ""}|${item.text_color || ""}|${item.font_family || ""}|${item.font_size_px || ""}|${item.font_size_pct || ""}|${srcW}x${srcH}`;
        if (sizeChanged || textOverlayKey !== key) {
            drawTextOnly(textOverlay, item);
            textOverlayKey = key;
        }
        samplerCtx.drawImage(textOverlay, 0, 0);
        const srcData = samplerCtx.getImageData(0, 0, srcW, srcH);
        drawForSkin(skin, ctx, canvas.width, canvas.height, srcData, srcW, srcH);
    }

    function getCachedImage(item) {
        const cached = imageCache.get(item.id);
        if (cached) return cached;
        const img = new Image();
        img.addEventListener("load", () => renderOnce());
        img.src = `/api/content/${item.id}/asset?v=${refreshVersion}`;
        imageCache.set(item.id, img);
        return img;
    }

    function getCachedVideo(videoId) {
        const cached = videoCache.get(videoId);
        if (cached) return cached;
        const video = document.createElement("video");
        video.muted = true;
        video.playsInline = true;
        video.preload = "auto";
        video.src = `/api/content/${videoId}/video?v=${refreshVersion}`;
        video.addEventListener("seeked", () => renderOnce());
        video.addEventListener("loadeddata", () => renderOnce());
        videoCache.set(videoId, video);
        return video;
    }

    function updateAutoOverlay(slot) {
        const item = slot.item;
        if (item.type === "text_slide" && item.auto_mode) {
            autoText.hidden = false;
            autoText.textContent = formatAutoText(
                item.auto_mode,
                item.auto_format,
                new Date(),
            );
        } else {
            autoText.hidden = true;
        }
    }

    function sizeCanvasToStage() {
        const rect = stage.getBoundingClientRect();
        // Stage hasn't been laid out yet — bail and retry on the next
        // animation frame. Without this guard, the first renderOnce
        // (which fires synchronously inside refresh) set canvas.width=1
        // and the cached check below kept it stuck there even after
        // layout settled.
        if (rect.width < 2 || rect.height < 2) {
            requestAnimationFrame(renderOnce);
            return false;
        }
        const w = Math.round(rect.width);
        const h = Math.round(rect.height);
        if (canvas.width !== w) canvas.width = w;
        if (canvas.height !== h) canvas.height = h;
        return true;
    }

    function clearCanvas() {
        const ctx = canvas.getContext("2d");
        ctx.fillStyle = "#000";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
    }

    function setPlaying(next) {
        if (next === playing) return;
        playing = next;
        playBtn.textContent = playing ? "❚❚" : "▶";
        playBtn.setAttribute("aria-label", playing ? "pause" : "play");
        if (playing) {
            // syncActiveVideo (called from drawSlot) will start the
            // currently-active video on the next tick. Don't blanket-
            // play() the whole cache — non-active videos stay paused
            // so we don't decode three videos at once.
            lastTick = null;
            rafId = requestAnimationFrame(tick);
        } else {
            pauseAllVideosExcept(null);
            if (rafId) cancelAnimationFrame(rafId);
            rafId = null;
        }
    }

    function tick(now) {
        if (!playing) return;
        if (lastTick == null) lastTick = now;
        const dt = (now - lastTick) / 1000;
        lastTick = now;
        if (totalSec > 0) {
            position = (position + dt) % totalSec;
        }
        slider.value = String(position.toFixed(2));
        timeEl.textContent = formatRange(position, totalSec);
        renderOnce();
        rafId = requestAnimationFrame(tick);
    }

    playBtn.addEventListener("click", () => setPlaying(!playing));
    slider.addEventListener("input", () => {
        position = Number(slider.value) || 0;
        timeEl.textContent = formatRange(position, totalSec);
        renderOnce();
    });

    window.addEventListener("resize", renderOnce);
    refresh();

    return {
        refresh,
        stop: () => {
            stopped = true;
            setPlaying(false);
            window.removeEventListener("resize", renderOnce);
        },
    };
}

function formatRange(posSec, totalSec) {
    return `${formatSec(posSec)} / ${formatSec(totalSec)}`;
}

function formatSec(sec) {
    const total = Math.max(0, Math.floor(sec || 0));
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `${m}:${String(s).padStart(2, "0")}`;
}

// --- skin draw functions (ported from the retired simulator.js) ---

function drawForSkin(skin, ctx, w, h, srcData, signW, signH) {
    if (skin === "hub75") return drawHub75(ctx, w, h, srcData, signW, signH);
    if (skin === "ws281x") return drawWs281x(ctx, w, h, srcData, signW, signH);
    return drawPlain(ctx, w, h, srcData, signW, signH);
}

function drawPlain(ctx, w, h, srcData, signW, signH) {
    ctx.imageSmoothingEnabled = false;
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, w, h);
    const tmp = document.createElement("canvas");
    tmp.width = signW;
    tmp.height = signH;
    tmp.getContext("2d").putImageData(srcData, 0, 0);
    // Cover-fit: scale up until both dims meet/exceed the canvas, then
    // center-crop the overflow. No stretch, no black letterbox bars
    // when the slide aspect doesn't match the device aspect — the slide
    // fills the panel and the operator sees what would actually play.
    const scale = Math.max(w / signW, h / signH);
    const drawW = signW * scale;
    const drawH = signH * scale;
    const offX = (w - drawW) / 2;
    const offY = (h - drawH) / 2;
    ctx.drawImage(tmp, offX, offY, drawW, drawH);
}

function drawHub75(ctx, w, h, srcData, signW, signH) {
    ctx.fillStyle = "#0a0a0a";
    ctx.fillRect(0, 0, w, h);
    const cellW = w / signW;
    const cellH = h / signH;
    const gap = 0.18;
    const drawW = Math.max(1, cellW * (1 - gap));
    const drawH = Math.max(1, cellH * (1 - gap));
    const offX = (cellW - drawW) / 2;
    const offY = (cellH - drawH) / 2;
    const px = srcData.data;
    for (let y = 0; y < signH; y++) {
        for (let x = 0; x < signW; x++) {
            const i = (y * signW + x) * 4;
            const r = px[i], g = px[i + 1], b = px[i + 2];
            ctx.fillStyle = r + g + b < 18 ? "#111113" : `rgb(${r},${g},${b})`;
            ctx.fillRect(x * cellW + offX, y * cellH + offY, drawW, drawH);
        }
    }
}

function drawWs281x(ctx, w, h, srcData, signW, signH) {
    ctx.fillStyle = "#050505";
    ctx.fillRect(0, 0, w, h);
    const cellW = w / signW;
    const cellH = h / signH;
    const cell = Math.min(cellW, cellH);
    const coreR = cell * 0.28;
    const glowR = cell * 0.55;
    const px = srcData.data;
    for (let y = 0; y < signH; y++) {
        for (let x = 0; x < signW; x++) {
            const i = (y * signW + x) * 4;
            const r = px[i], g = px[i + 1], b = px[i + 2];
            const cx = x * cellW + cellW / 2;
            const cy = y * cellH + cellH / 2;
            if (r + g + b < 18) {
                ctx.fillStyle = "#0f0f12";
                ctx.beginPath();
                ctx.arc(cx, cy, coreR, 0, Math.PI * 2);
                ctx.fill();
                continue;
            }
            const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, glowR);
            grad.addColorStop(0, `rgba(${r},${g},${b},0.75)`);
            grad.addColorStop(1, `rgba(${r},${g},${b},0)`);
            ctx.fillStyle = grad;
            ctx.fillRect(cx - glowR, cy - glowR, glowR * 2, glowR * 2);
            ctx.fillStyle = `rgb(${r},${g},${b})`;
            ctx.beginPath();
            ctx.arc(cx, cy, coreR, 0, Math.PI * 2);
            ctx.fill();
        }
    }
}

// --- exports for tests ---

export { drawHub75, drawPlain, drawWs281x, formatSec, pickSkin };
