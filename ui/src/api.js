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

/** Replace the entire playlist order with the given list of ids. */
export async function setPlaylistOrder(itemIds) {
    const response = await fetch("/api/playlist", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ item_ids: itemIds }),
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
