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
 * Upload an image slide. `payload`: name, duration_ms, image_base64
 * (the operator's source PNG/JPG bytes verbatim — backend keeps full
 * resolution, playback cover-fits to panel dims at slide entry).
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
 * Upload a video. `payload`: name, duration_ms, transition, png_base64
 * (thumbnail), mp4_base64 (H.264 MP4 bytes, ≤ 1080p).
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

/**
 * Patch just the duration_ms on any content item type. Used by the
 * Playlists-panel duration chip — saves the operator from re-PUTting
 * the whole asset for a one-field change.
 */
export async function patchSlideDuration(id, durationMs) {
    const response = await fetch(`/api/content/${id}/duration`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ duration_ms: Math.round(durationMs) }),
    });
    if (!response.ok) {
        const detail = await response.text();
        throw new Error(`Update duration failed (${response.status}): ${detail}`);
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

/**
 * Get the current playback state. Shape mirrors backend `PlaybackState`
 * (api_playback.py): {
 *   is_running, current_item_id, current_item_type,
 *   current_item_transition, current_item_transition_ms,
 *   current_item_auto_mode, current_item_auto_format,
 *   current_playlist_id
 * }. current_item_type is the ContentItem discriminator — "text_slide",
 * "image", or "video" — so the live-preview UI knows which element to
 * render.
 *
 * Note: the response uses `is_running` (not `running`) and
 * `current_playlist_id` (not `current_playlist_name`); resolve the id
 * → display name via fetchPlaylistList() if you need the name.
 */
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
 * Encode a playlist's items + (optional) name as the wire body. Accepts
 * either an array of UUID strings (legacy — each entry gets default
 * transitions) or an array of `{item_id, transition, transition_ms}`
 * objects (v3 canonical).
 */
function _encodePlaylistBody(entriesOrIds, name) {
    const body =
        Array.isArray(entriesOrIds) &&
        entriesOrIds.length > 0 &&
        typeof entriesOrIds[0] === "object"
            ? { items: entriesOrIds }
            : { item_ids: entriesOrIds };
    if (name !== undefined && name !== null) body.name = name;
    return body;
}

/**
 * Replace the default playlist's contents — the legacy single-playlist
 * shorthand. Operates on `/api/playlist` which always targets the
 * server's DEFAULT_PLAYLIST_ID. For non-default playlists, use
 * `savePlaylistById`.
 */
export async function setDefaultPlaylistOrder(entriesOrIds) {
    const response = await fetch("/api/playlist", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(_encodePlaylistBody(entriesOrIds)),
    });
    if (!response.ok) {
        const detail = await response.text();
        throw new Error(`Reorder failed (${response.status}): ${detail}`);
    }
    return await response.json();
}

/** Fetch the full playlist collection: { schema_version, playlists: [...] }. */
export async function listPlaylists() {
    const response = await fetch("/api/playlists");
    if (!response.ok) {
        throw new Error(`Playlists fetch failed (${response.status})`);
    }
    return await response.json();
}

/**
 * Create a new playlist. Server assigns a fresh UUID and returns the
 * full Playlist object including its `id`.
 */
export async function createPlaylist({ name, entries }) {
    const response = await fetch("/api/playlists", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(_encodePlaylistBody(entries, name)),
    });
    if (!response.ok) {
        const detail = await response.text();
        throw new Error(`Create playlist failed (${response.status}): ${detail}`);
    }
    return await response.json();
}

/**
 * Replace a playlist's name and/or items. The id is the immutable key —
 * a rename of `name` here NEVER changes the id, so any schedule rule
 * referencing this playlist keeps working.
 *
 * Pass `name: undefined` to leave the existing name unchanged.
 */
export async function savePlaylistById(id, { name, entries }) {
    const response = await fetch(
        `/api/playlists/${encodeURIComponent(id)}`,
        {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(_encodePlaylistBody(entries, name)),
        },
    );
    if (!response.ok) {
        const detail = await response.text();
        throw new Error(`Save playlist failed (${response.status}): ${detail}`);
    }
    return await response.json();
}

/** Delete a playlist by id. */
export async function deletePlaylistById(id) {
    const response = await fetch(`/api/playlists/${encodeURIComponent(id)}`, {
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


/* --- Flock: peer openMarquee devices for mesh media sync. --- */

export async function listFlock() {
    const response = await fetch("/api/flock");
    if (!response.ok) {
        throw new Error(`List flock failed (${response.status})`);
    }
    return await response.json();
}

export async function addFlockPeer(address) {
    const response = await fetch("/api/flock", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ address }),
    });
    if (!response.ok) {
        // 422 bodies are FastAPI validation envelopes ({detail: [...]}); 409
        // bodies are {detail: "..."}. Pull out a one-line reason so the
        // modal doesn't dump a regex at the operator.
        let message = `HTTP ${response.status}`;
        try {
            const body = await response.json();
            if (typeof body.detail === "string") {
                message = body.detail;
            } else if (Array.isArray(body.detail) && body.detail[0]?.msg) {
                message = body.detail[0].msg;
            }
        } catch {
            /* not JSON — keep the status code */
        }
        if (response.status === 422) {
            message = `Invalid address — expected a hostname or IP (optionally host:port).`;
        }
        throw new Error(message);
    }
    return await response.json();
}

export async function updateFlockPeer(peerId, { sync, name } = {}) {
    const body = {};
    if (sync !== undefined) body.sync = sync;
    if (name !== undefined) body.name = name;
    const response = await fetch(`/api/flock/${peerId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
    });
    if (!response.ok) {
        throw new Error(`Update peer failed (${response.status})`);
    }
    return await response.json();
}

export async function deleteFlockPeer(peerId) {
    const response = await fetch(`/api/flock/${peerId}`, { method: "DELETE" });
    if (!response.ok) {
        throw new Error(`Remove peer failed (${response.status})`);
    }
}
