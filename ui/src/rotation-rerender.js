// FYS bug 7 — bulk re-render stored slide thumbnails on a rotation change.
//
// The dashboard slide-browser tiles pin `aspect-ratio: var(--device-aspect)`,
// so when the operator flips display_rotation to 90/270 the tile reshapes to
// portrait. A slide's stored asset.png, however, was rasterized for the OLD
// orientation — at 90/270 a landscape PNG gets cover-cropped to a centre
// strip ("content wrong"). rasterizeAtTarget is now rotation-aware so every
// FUTURE save is laid out correctly; this module fixes the slides that were
// ALREADY saved by re-rendering + re-uploading their asset.png at the new
// orientation.
//
// Trigger: main.js listens for `openmarquee:settings-updated`, compares the
// new display_rotation against the previous value tracked here, and calls
// rerenderAllSlidesForRotation() only on an actual CHANGE. Rotation changes
// are rare, so a bounded one-shot over N slides is acceptable cost.
//
// Resilience: each slide is re-rendered inside its own try/catch — one slide
// failing (corrupt asset, decode error, transient network) logs a warning
// and the loop continues with the rest.
//
// Per slide TYPE:
//   - text_slide: stateFromItem(item) → rasterizeAtTarget(state, rotation)
//       → updateTextSlide. The text_layers boxes are fractional (0..1) so
//       the scene re-flows naturally into the swapped portrait canvas.
//   - video: the asset.png is a cover-fit of the first video frame. We
//       re-fetch the stored MP4, extract its first frame, cover-fit it onto
//       a canvas at the new (rotation-aware) orientation, and PUT it back
//       via updateVideo (mp4_base64 omitted → server keeps the MP4 bytes).
//   - image: SKIPPED on purpose. An image slide's asset.png is the
//       operator's SOURCE picture stored verbatim (full resolution) — it is
//       NOT a pre-cropped thumbnail. The dashboard tile's CSS `object-fit:
//       cover` already crops the source to whatever aspect the tile is, and
//       the device cover-fits at playout. Re-rendering would bake a portrait
//       crop into the source and permanently destroy resolution. So image
//       thumbnails are already correct under any rotation; nothing to do.
//   - vlc_stream: SKIPPED — no client-side asset; the thumbnail card is
//       synthesised server-side and carries no orientation.

import {
    fetchContentItem,
    listContent,
    mediaSrc,
    updateTextSlide,
    updateVideo,
} from "./api.js";
import { drawFirstFrameToCanvas } from "./video-upload.js";
import { canvasToBase64 } from "./image-upload.js";
import { rasterizeAtTarget } from "./rasterize.js";
import { stateFromItem } from "./state-from-item.js";

// The fixed rasterize canvas dims (kept in sync with rasterize.js — the
// landscape master size; rotation-aware callers swap W/H themselves).
const RASTERIZE_W = 3840;
const RASTERIZE_H = 2160;

/**
 * Re-render a single text slide's asset.png for `rotation` and PUT it back.
 * Rebuilds the TextSlideUpload payload from the slide's existing wire
 * fields (full-replacement PUT) plus a fresh rotation-aware PNG.
 */
async function rerenderTextSlide(item, rotation) {
    // background_image_slide_id is left unresolved (null) — the editor's
    // performSave already only embeds the bg image into the PNG when the
    // image is loaded; for a headless bulk pass we render without the
    // referenced bg slide. The reference itself is preserved on the wire
    // payload so playout still composites it; only the dashboard
    // thumbnail loses the bg-slide layer, an acceptable trade for a
    // background that is itself another slide.
    const state = stateFromItem(item, null);
    const png_base64 = rasterizeAtTarget(state, rotation);
    // Payload shape mirrors editor.js performSave EXACTLY — the proven
    // text-slide PUT body. `transition` / `transition_ms` are playlist-
    // item fields, NOT TextSlide content fields, so they are not sent.
    // The stored item's text_layers are already in the wire shape, so
    // they pass through unchanged.
    const payload = {
        name: item.name || "Untitled",
        background_color: item.background_color || "#000000",
        background_image_slide_id: item.background_image_slide_id || null,
        background_video_slide_id: item.background_video_slide_id || null,
        background_pattern: item.background_pattern || null,
        duration_ms: item.duration_ms ?? 5000,
        text_layers: Array.isArray(item.text_layers) ? item.text_layers : [],
        png_base64,
    };
    await updateTextSlide(String(item.id), payload);
}

/**
 * Re-render a video slide's first-frame thumbnail for `rotation` and PUT it
 * back. Fetches the stored MP4, extracts frame 0, cover-fits it onto a
 * canvas sized for the new orientation. mp4_base64 is omitted so the server
 * retains the existing video bytes (metadata-only PNG swap).
 */
async function rerenderVideoSlide(item, rotation) {
    const portrait = rotation === 90 || rotation === 270;
    const canvas = document.createElement("canvas");
    canvas.width = portrait ? RASTERIZE_H : RASTERIZE_W;
    canvas.height = portrait ? RASTERIZE_W : RASTERIZE_H;
    // drawFirstFrameToCanvas takes a Blob/File; fetch the stored MP4 as a
    // Blob via the token-stamped media URL. It paints cover-fit to the
    // canvas dims, so the thumbnail matches the new tile aspect.
    const res = await fetch(mediaSrc(`/api/content/${item.id}/video`));
    if (!res.ok) {
        throw new Error(`video fetch failed (${res.status})`);
    }
    const blob = await res.blob();
    await drawFirstFrameToCanvas(blob, canvas);
    const payload = {
        name: item.name || "Video",
        duration_ms: item.duration_ms ?? 10000,
        png_base64: canvasToBase64(canvas),
        // null → server keeps the stored MP4 (see VideoUpdate in api.py).
        mp4_base64: null,
    };
    await updateVideo(String(item.id), payload);
}

/**
 * Re-render every stored slide's asset.png for the new display_rotation.
 *
 * Bounded one-shot. Per-slide failures are caught + logged so one bad
 * slide can't abort the rest. Returns a summary {total, rerendered,
 * skipped, failed} — handy for callers that want to surface a status.
 *
 * @param {number} rotation — the NEW display_rotation (0/90/180/270).
 * @param {object} [deps] — injectable for tests; defaults to the real API.
 */
export async function rerenderAllSlidesForRotation(rotation, deps = {}) {
    const fetchList = deps.listContent || listContent;
    const fetchItem = deps.fetchContentItem || fetchContentItem;
    const rerenderText = deps.rerenderTextSlide || rerenderTextSlide;
    const rerenderVideo = deps.rerenderVideoSlide || rerenderVideoSlide;

    const summary = { total: 0, rerendered: 0, skipped: 0, failed: 0 };
    let items;
    try {
        items = await fetchList();
    } catch (err) {
        console.warn("[rotation-rerender] could not list content:", err);
        return summary;
    }
    if (!Array.isArray(items)) return summary;
    summary.total = items.length;

    for (const listItem of items) {
        const id = listItem && listItem.id;
        const type = listItem && listItem.type;
        if (!id) {
            summary.skipped += 1;
            continue;
        }
        // image / vlc_stream carry no orientation-baked thumbnail that
        // rotation can break — see the module header.
        if (type !== "text_slide" && type !== "video") {
            summary.skipped += 1;
            continue;
        }
        try {
            // The list endpoint already returns full ContentItems, but
            // re-fetch the single item so we work against the freshest
            // copy (a concurrent edit between list + rerender is rare but
            // cheap to guard against).
            const item = await fetchItem(String(id));
            if (item.type === "text_slide") {
                await rerenderText(item, rotation);
            } else if (item.type === "video") {
                await rerenderVideo(item, rotation);
            }
            summary.rerendered += 1;
        } catch (err) {
            summary.failed += 1;
            console.warn(
                `[rotation-rerender] slide ${id} (${type}) failed:`,
                err,
            );
        }
    }
    return summary;
}
