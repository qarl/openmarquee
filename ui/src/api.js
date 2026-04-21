// Thin client over the openMarquee REST API. Same-origin, no CORS, no auth.

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

/**
 * Upload a video. `payload`: name, duration_ms, pipeline, transition,
 * png_base64 (thumbnail), mp4_base64 (MP4 H.264 bytes).
 */
export async function saveVideo(payload) {
    const response = await fetch("/api/content/videos", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    });
    if (!response.ok) {
        const detail = await response.text();
        throw new Error(`Save video failed (${response.status}): ${detail}`);
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

/** Fetch a single content item by id (for the editor's edit-existing flow). */
export async function fetchContentItem(id) {
    const response = await fetch(`/api/content/${id}`);
    if (!response.ok) {
        throw new Error(`Fetch item failed (${response.status})`);
    }
    return await response.json();
}

/**
 * Update an existing text slide. `id` is the slide's UUID; the payload
 * matches the same shape as saveTextSlide. Preserves the UUID + created_at
 * so playlist / schedule references keep pointing at the same content.
 */
export async function updateTextSlide(id, payload) {
    const response = await fetch(`/api/content/text-slides/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    });
    if (!response.ok) {
        const detail = await response.text();
        throw new Error(`Update failed (${response.status}): ${detail}`);
    }
    return await response.json();
}

/**
 * Update an existing image slide. `png_base64` may be null to keep the
 * stored PNG untouched (metadata-only update).
 */
export async function updateImage(id, payload) {
    const response = await fetch(`/api/content/images/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    });
    if (!response.ok) {
        const detail = await response.text();
        throw new Error(`Update image failed (${response.status}): ${detail}`);
    }
    return await response.json();
}

/**
 * Update an existing video slide. Both `png_base64` (thumbnail) and
 * `mp4_base64` may be null to keep the stored bytes — useful for
 * metadata-only edits where you don't want to re-upload 50 MB.
 */
export async function updateVideo(id, payload) {
    const response = await fetch(`/api/content/videos/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    });
    if (!response.ok) {
        const detail = await response.text();
        throw new Error(`Update video failed (${response.status}): ${detail}`);
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

/**
 * Replace the entire playlist contents. Accepts either:
 *  - an array of UUID strings (legacy, each entry gets default
 *    transitions — same wire shape that existed pre-v3), or
 *  - an array of `{item_id, transition, transition_ms}` objects (v3
 *    canonical — lets the caller carry transition data).
 */
export async function setPlaylistOrder(entriesOrIds) {
    const body =
        Array.isArray(entriesOrIds) &&
        entriesOrIds.length > 0 &&
        typeof entriesOrIds[0] === "object"
            ? { items: entriesOrIds }
            : { item_ids: entriesOrIds };
    const response = await fetch("/api/playlist", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
    });
    if (!response.ok) {
        const detail = await response.text();
        throw new Error(`Reorder failed (${response.status}): ${detail}`);
    }
    return await response.json();
}

/** Fetch the full named-playlist collection: { schema_version, playlists: {...} }. */
export async function listPlaylists() {
    const response = await fetch("/api/playlists");
    if (!response.ok) {
        throw new Error(`Playlists fetch failed (${response.status})`);
    }
    return await response.json();
}

/** Create or replace a named playlist with the given item ids. */
export async function savePlaylistByName(name, itemIds) {
    const response = await fetch(
        `/api/playlists/${encodeURIComponent(name)}`,
        {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ item_ids: itemIds }),
        },
    );
    if (!response.ok) {
        const detail = await response.text();
        throw new Error(`Save playlist failed (${response.status}): ${detail}`);
    }
    return await response.json();
}

/** Delete a named playlist. */
export async function deletePlaylistByName(name) {
    const response = await fetch(`/api/playlists/${encodeURIComponent(name)}`, {
        method: "DELETE",
    });
    if (!response.ok) {
        throw new Error(`Delete playlist failed (${response.status})`);
    }
}

/**
 * Generate a new background via the OpenAI-backed endpoint. On 503 the
 * caller should treat this as "feature unavailable" (no API key on the
 * device) rather than a hard failure.
 */
export async function generateBackground({ prompt, name }) {
    const response = await fetch("/api/backgrounds/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt, name }),
    });
    if (!response.ok) {
        let detail = "";
        try {
            const body = await response.json();
            detail = body?.detail || "";
        } catch {
            detail = await response.text();
        }
        const err = new Error(
            `Background generation failed (${response.status}): ${detail}`,
        );
        err.status = response.status;
        throw err;
    }
    return await response.json();
}

/** Fetch the current schedule (rules + default_playlist_name). */
export async function getSchedule() {
    const response = await fetch("/api/schedules");
    if (!response.ok) {
        throw new Error(`Schedule fetch failed (${response.status})`);
    }
    return await response.json();
}

/** Fetch the device system settings. */
export async function getSettings() {
    const response = await fetch("/api/settings");
    if (!response.ok) {
        throw new Error(`Settings fetch failed (${response.status})`);
    }
    return await response.json();
}

/** Replace the device system settings. */
export async function saveSettings(settings) {
    const response = await fetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(settings),
    });
    if (!response.ok) {
        const detail = await response.text();
        throw new Error(`Save settings failed (${response.status}): ${detail}`);
    }
    return await response.json();
}

/** Replace the schedule with the given object (rules + default_playlist_name). */
export async function saveSchedule(schedule) {
    const response = await fetch("/api/schedules", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(schedule),
    });
    if (!response.ok) {
        const detail = await response.text();
        throw new Error(`Save schedule failed (${response.status}): ${detail}`);
    }
    return await response.json();
}
