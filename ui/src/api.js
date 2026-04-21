// Thin client over the OpenMarquee REST API. Same-origin, no CORS, no auth.

export async function fetchHealth() {
    const response = await fetch("/healthz");
    if (!response.ok) {
        throw new Error(`/healthz returned ${response.status}`);
    }
    return await response.json();
}

/**
 * Upload a text slide. `payload` matches the backend TextSlideUpload schema:
 * name, text, text_color, background_color, png_base64.
 *
 * Returns the full TextSlide object (server-assigned id, created_at, etc.).
 */
export async function saveTextSlide(payload) {
    const response = await fetch("/api/content/text-slides", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    });
    if (!response.ok) {
        const detail = await response.text();
        throw new Error(`Save failed (${response.status}): ${detail}`);
    }
    return await response.json();
}

/**
 * Upload an image slide. `payload`: name, duration_ms, png_base64 (the
 * already-scaled PNG — browser does the scaling, backend just stores).
 */
export async function saveImage(payload) {
    const response = await fetch("/api/content/images", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    });
    if (!response.ok) {
        const detail = await response.text();
        throw new Error(`Save failed (${response.status}): ${detail}`);
    }
    return await response.json();
}

/** Fetch the full list of content items. */
export async function listContent() {
    const response = await fetch("/api/content");
    if (!response.ok) {
        throw new Error(`List failed (${response.status})`);
    }
    return await response.json();
}

/** Delete a content item by id. */
export async function deleteContent(id) {
    const response = await fetch(`/api/content/${id}`, { method: "DELETE" });
    if (!response.ok) {
        throw new Error(`Delete failed (${response.status})`);
    }
}

/**
 * Push a content item to the dev MockRenderer. Dev-only; replaced by the
 * real playback engine in Phase 5. Backend returns 204 on success.
 */
export async function playContent(id) {
    const response = await fetch(`/dev/play/${id}`, { method: "POST" });
    if (!response.ok) {
        throw new Error(`Play failed (${response.status})`);
    }
}

/** Get the current playback state: { is_running, current_item_id }. */
export async function getPlaybackState() {
    const response = await fetch("/api/playback/state");
    if (!response.ok) {
        throw new Error(`Playback state failed (${response.status})`);
    }
    return await response.json();
}

/** Start the playback loop — no-op if already running. */
export async function startPlayback() {
    const response = await fetch("/api/playback/start", { method: "POST" });
    if (!response.ok) {
        throw new Error(`Start failed (${response.status})`);
    }
}

/** Stop the playback loop — no-op if not running. */
export async function stopPlayback() {
    const response = await fetch("/api/playback/stop", { method: "POST" });
    if (!response.ok) {
        throw new Error(`Stop failed (${response.status})`);
    }
}
