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
        // Cross-fade into the next slot if we're within the fade window.
        const timeInto = position - slot.startSec;
        const timeLeft = slot.endSec - position;
        const fadeSec = slot.transition === "fade" ? slot.transition_ms / 1000 : 0;
        if (fadeSec > 0 && timeLeft < fadeSec && idx < timeline.length - 1) {
            const alpha = 1 - timeLeft / fadeSec; // 0 → 1 as we near the cut
            const ctx = canvas.getContext("2d");
            ctx.globalAlpha = alpha;
            drawSlot(timeline[idx + 1]);
            ctx.globalAlpha = 1;
        }
        updateAutoOverlay(slot);
        // Silence unused for now.
        void timeInto;
    }

    function drawSlot(slot) {
        const item = slot.item;
        if (item.type === "video") {
            syncActiveVideo(item, slot);
            drawVideo(item, slot);
        } else {
            // Switched away from a playing video — pause it so audio-less
            // h264 doesn't keep decoding off-screen.
            pauseAllVideosExcept(null);
            activeVideoId = null;
            drawImage(item);
        }
    }

    function syncActiveVideo(item, slot) {
        const video = getCachedVideo(item);
        const offsetInto = position - slot.startSec;
        const isNewActive = activeVideoId !== item.id;
        if (isNewActive) {
            // Slot just changed. Pause every other video, point this one
            // at the slot offset, and (if we're playing) let it run.
            pauseAllVideosExcept(item.id);
            activeVideoId = item.id;
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
        const video = getCachedVideo(item);
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

    function getCachedImage(item) {
        const cached = imageCache.get(item.id);
        if (cached) return cached;
        const img = new Image();
        img.addEventListener("load", () => renderOnce());
        img.src = `/api/content/${item.id}/asset?v=${refreshVersion}`;
        imageCache.set(item.id, img);
        return img;
    }

    function getCachedVideo(item) {
        const cached = videoCache.get(item.id);
        if (cached) return cached;
        const video = document.createElement("video");
        video.muted = true;
        video.playsInline = true;
        video.preload = "auto";
        video.src = `/api/content/${item.id}/video?v=${refreshVersion}`;
        video.addEventListener("seeked", () => renderOnce());
        video.addEventListener("loadeddata", () => renderOnce());
        videoCache.set(item.id, video);
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
