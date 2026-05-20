// Thin client over the openMarquee REST API. Same-origin; bearer-token
// auth via Batch 20.2's localStorage[openmarquee_auth_token] key, injected
// by apiFetch below.

// 20.3 / phase A.3 — keep this in sync with the literal string in
// ui/src/set-password.js + ui/src/login.js + ui/src/main.js. Pre-auth
// pages don't import api.js (so they can't import this constant), which
// is why the four sites currently inline-duplicate the literal. A future
// shared-constants module could DRY them up; not in scope here.
export const AUTH_TOKEN_KEY = "openmarquee_auth_token";

/**
 * 20.3 / phase A.3 — single-source fetch wrapper. Adds the bearer
 * token to every /api/* request and triggers a re-auth bounce on 401.
 *
 * Header injection: when localStorage has openmarquee_auth_token, we
 * stamp `Authorization: Bearer <token>` onto the request. No token →
 * no header (the backend will 401 if the route needs auth; we handle
 * that below).
 *
 * 401 handling: the backend rejects a token when its version prefix
 * doesn't match the current AuthState.token_version (change-password
 * bumped it, auth.json was wiped, etc.). We treat 401 as "this token
 * is dead": clear it from localStorage so the next reload doesn't
 * try the same dead token, then bounce to one of two destinations
 * based on the response's `detail` string. AuthMiddleware emits
 * "authentication not configured -- visit /welcome" when there is
 * no auth.json at all (fresh device) and "authentication required"
 * otherwise; the former routes to /welcome.html (operator has no
 * password to type yet), the latter to /login.html. `throw` so the
 * caller's then-chain doesn't proceed against a stale Response. The
 * bounce is suppressed when we're already on the destination page so
 * the post-401 redirect doesn't loop.
 *
 * Caveats (acceptable for v1, called out in the 20.3 dispatch):
 *   - Background sync paths (autosave, flock pulls) lose any in-flight
 *     POST if the 401 fires mid-request; the operator re-logs in and
 *     re-saves. The token-version-bump flow is rare enough (only on
 *     change-password) that a queue-and-replay isn't worth building.
 *   - Multi-tab logout: one tab clearing localStorage on 401 doesn't
 *     notify other tabs until their next fetch (which will also 401
 *     and trigger their own redirect). Eventual consistency; no
 *     cross-tab BroadcastChannel needed.
 */
export async function apiFetch(input, init = {}) {
    // 20.4: callers that own a 401 inline (e.g. the Change-secret form
    // where 401 means "wrong current_password", not "stale token")
    // pass `skipAuth401Redirect: true` so apiFetch leaves the response
    // alone -- no localStorage clear, no /login.html redirect, no
    // throw. Non-init key; we strip it before passing to fetch.
    const skip401 = init.skipAuth401Redirect === true;
    const { skipAuth401Redirect: _skip, ...fetchInit } = init;
    const headers = new Headers(fetchInit.headers || {});
    if (typeof localStorage !== "undefined") {
        try {
            const token = localStorage.getItem(AUTH_TOKEN_KEY);
            if (token) {
                headers.set("Authorization", `Bearer ${token}`);
            }
        } catch {
            // localStorage can throw in private-browsing contexts; the
            // request still goes out, it just won't carry the token.
        }
    }
    const response = await fetch(input, { ...fetchInit, headers });
    if (response.status === 401 && !skip401) {
        try {
            localStorage?.removeItem?.(AUTH_TOKEN_KEY);
        } catch { /* ignore */ }
        // First-run carve-out: AuthMiddleware emits a distinct detail
        // string when auth.json hasn't been created yet ("authentication
        // not configured -- visit /welcome"; see backend/openmarquee/
        // auth_middleware.py:174). Without this branch the fresh-Pi flow
        // bounces to /login.html with no password to type, leaving the
        // operator stuck (qarl caught on debug Pi 2026-05-16). The
        // configured-but-stale-token case still routes to /login.html.
        let dest = "/login.html";
        try {
            const peek = await response.clone().json();
            if (
                typeof peek?.detail === "string" &&
                peek.detail.includes("not configured")
            ) {
                dest = "/welcome.html";
            }
        } catch {
            // Response body not JSON, or clone() unavailable (some test
            // mocks); fall back to /login.html.
        }
        if (
            typeof window !== "undefined" &&
            window.location &&
            window.location.pathname !== dest
        ) {
            window.location.replace(dest);
        }
        // Throw so the caller's response-handling code doesn't continue
        // against a stale Response. When the redirect fires the page
        // is gone so the throw doesn't surface; in the suppressed-
        // redirect case (we're already on /login.html or /welcome.html)
        // the caller's catch block handles the failure inline.
        throw new Error("authentication required");
    }
    return response;
}

/**
 * Bug 3+4 (qarl 2026-05-16): append `?token=<bearer>` to a media URL
 * so a bare `<img src>` / `<video src>` reaches the auth-gated
 * `/api/content/{id}/asset` and `/api/content/{id}/video` endpoints.
 * AuthMiddleware accepts the query-param token for those routes
 * specifically (see backend/openmarquee/auth_middleware.py).
 *
 * Handles URLs that already have a query string (e.g. `?v=...` cache
 * buster) — appends `&token=...` instead of `?token=...`. When there
 * is no token in localStorage (pre-auth), returns the URL untouched
 * so the resulting 401 surfaces via the normal apiFetch redirect.
 */
export function mediaSrc(path) {
    let token = "";
    if (typeof localStorage !== "undefined") {
        try {
            token = localStorage.getItem(AUTH_TOKEN_KEY) || "";
        } catch { /* private-browsing localStorage throws; degrade */ }
    }
    if (!token) return path;
    const sep = path.includes("?") ? "&" : "?";
    return `${path}${sep}token=${encodeURIComponent(token)}`;
}

/**
 * 15.3: Best-effort "pull a one-line operator-friendly message out of a
 * non-OK response." Handles three shapes FastAPI emits:
 *   - 422 ValidationError -> `{detail: [{msg, loc, ...}, ...]}` (array)
 *   - 4xx with `raise HTTPException(detail="...")` -> `{detail: "..."}` (string)
 *   - 5xx with no JSON body or bytes -> falls back to `status statusText`
 *
 * Used at every non-OK throw site so the operator gets a useful message
 * instead of a raw JSON dump. addFlockPeer used to be the only caller
 * with this pattern; sweep #7 #4 hoisted it for uniformity.
 */
export async function extractDetailMessage(response) {
    try {
        const body = await response.json();
        if (typeof body.detail === "string") return body.detail;
        if (Array.isArray(body.detail) && body.detail[0]?.msg) {
            return body.detail[0].msg;
        }
    } catch {
        /* not JSON -- fall through to the status-only fallback */
    }
    return `${response.status} ${response.statusText}`.trim();
}

/**
 * Upload a text slide. `payload` matches the backend `TextSlideUpload`
 * schema (api.py) — see that class for the full field list (it grew
 * past anything worth enumerating here when Phase 5b added
 * `background_video_slide_id`). Returns the full TextSlide object
 * (server-assigned id, created_at, etc.).
 */
export async function saveTextSlide(payload) {
    const response = await apiFetch("/api/content/text-slides", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    });
    if (!response.ok) {
        const detail = await extractDetailMessage(response);
        throw new Error(`Save failed (${response.status}): ${detail}`);
    }
    return await response.json();
}

/**
 * Upload an image slide. `payload` matches the backend `ImageUpload`
 * schema (api.py). The operator's source PNG/JPG bytes go in verbatim
 * via `image_base64` — backend keeps full resolution, playback
 * cover-fits to panel dims at slide entry.
 */
export async function saveImage(payload) {
    const response = await apiFetch("/api/content/images", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    });
    if (!response.ok) {
        const detail = await extractDetailMessage(response);
        throw new Error(`Save failed (${response.status}): ${detail}`);
    }
    return await response.json();
}

/**
 * Upload a video. `payload` matches the backend `VideoUpload` schema
 * (api.py). Carries the thumbnail (`png_base64`) + the H.264 MP4 bytes
 * (`mp4_base64`, ≤ 1080p — Pi Zero 2 W's hardware decoder ceiling).
 */
export async function saveVideo(payload) {
    const response = await apiFetch("/api/content/videos", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    });
    if (!response.ok) {
        const detail = await extractDetailMessage(response);
        throw new Error(`Save video failed (${response.status}): ${detail}`);
    }
    return await response.json();
}

/** Fetch the full list of content items. */
export async function listContent() {
    const response = await apiFetch("/api/content");
    if (!response.ok) {
        throw new Error(`List failed (${response.status})`);
    }
    return await response.json();
}

/** Fetch a single content item by id (for the editor's edit-existing flow). */
export async function fetchContentItem(id) {
    const response = await apiFetch(`/api/content/${id}`);
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
    const response = await apiFetch(`/api/content/text-slides/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    });
    if (!response.ok) {
        const detail = await extractDetailMessage(response);
        throw new Error(`Update failed (${response.status}): ${detail}`);
    }
    return await response.json();
}

/**
 * Update an existing image slide. `png_base64` may be null to keep the
 * stored PNG untouched (metadata-only update).
 */
export async function updateImage(id, payload) {
    const response = await apiFetch(`/api/content/images/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    });
    if (!response.ok) {
        const detail = await extractDetailMessage(response);
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
    const response = await apiFetch(`/api/content/videos/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    });
    if (!response.ok) {
        const detail = await extractDetailMessage(response);
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
    const response = await apiFetch(`/api/content/${id}/duration`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ duration_ms: Math.round(durationMs) }),
    });
    if (!response.ok) {
        const detail = await extractDetailMessage(response);
        throw new Error(`Update duration failed (${response.status}): ${detail}`);
    }
    return await response.json();
}

/** Delete a content item by id. */
export async function deleteContent(id) {
    const response = await apiFetch(`/api/content/${id}`, { method: "DELETE" });
    if (!response.ok) {
        throw new Error(`Delete failed (${response.status})`);
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
    const response = await apiFetch("/api/playback/state");
    if (!response.ok) {
        throw new Error(`Playback state failed (${response.status})`);
    }
    return await response.json();
}

/** Start the playback loop — no-op if already running. */
export async function startPlayback() {
    const response = await apiFetch("/api/playback/start", { method: "POST" });
    if (!response.ok) {
        throw new Error(`Start failed (${response.status})`);
    }
}

/** Stop the playback loop — no-op if not running. */
export async function stopPlayback() {
    const response = await apiFetch("/api/playback/stop", { method: "POST" });
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

/** Fetch the full playlist collection: { schema_version, playlists: [...] }. */
export async function listPlaylists() {
    const response = await apiFetch("/api/playlists");
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
    const response = await apiFetch("/api/playlists", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(_encodePlaylistBody(entries, name)),
    });
    if (!response.ok) {
        const detail = await extractDetailMessage(response);
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
    const response = await apiFetch(
        `/api/playlists/${encodeURIComponent(id)}`,
        {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(_encodePlaylistBody(entries, name)),
        },
    );
    if (!response.ok) {
        const detail = await extractDetailMessage(response);
        throw new Error(`Save playlist failed (${response.status}): ${detail}`);
    }
    return await response.json();
}

/** Delete a playlist by id. */
export async function deletePlaylistById(id) {
    const response = await apiFetch(`/api/playlists/${encodeURIComponent(id)}`, {
        method: "DELETE",
    });
    if (!response.ok) {
        throw new Error(`Delete playlist failed (${response.status})`);
    }
}

/**
 * Generate a new background via the provider-pluggable endpoint.
 * Default provider is Pollinations.ai (free, no API key); the device's
 * settings can swap in Hugging Face / local Stable Diffusion via a
 * registry edit (backgrounds.py). On 503 the caller should treat this
 * as "provider unavailable / not configured" rather than a hard
 * failure. Prompt is capped at 500 chars (BackgroundGenerateRequest)
 * — longer payloads 422 with a JSON-safe detail.
 */
export async function generateBackground({ prompt, name }) {
    const response = await apiFetch("/api/backgrounds/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt, name }),
    });
    if (!response.ok) {
        const detail = await extractDetailMessage(response);
        const err = new Error(
            `Background generation failed (${response.status}): ${detail}`,
        );
        err.status = response.status;
        throw err;
    }
    return await response.json();
}

/** Fetch the current schedule (rules + default_playlist_id, post-UUID). */
export async function getSchedule() {
    const response = await apiFetch("/api/schedules");
    if (!response.ok) {
        throw new Error(`Schedule fetch failed (${response.status})`);
    }
    return await response.json();
}

/** Fetch the device system settings. */
export async function getSettings() {
    const response = await apiFetch("/api/settings");
    if (!response.ok) {
        throw new Error(`Settings fetch failed (${response.status})`);
    }
    return await response.json();
}

/** Compute the effective display dims after applying display_rotation.
 *
 * Rotation 0 / 180 → physical orientation matches the panel; dims pass
 * through. Rotation 90 / 270 → the panel is mounted portrait, so the
 * sign actually outputs height-by-width — swap so previews match the
 * installed orientation. Per §5.9 ('Live Preview') every preview canvas
 * pins its aspect ratio to display_width × display_height; this helper
 * is the single source of truth for "what dims should I render at?"
 * across stream preview, slide editor previews, inline preview canvas,
 * and the panel-level resolvePanelDims dispatch.
 *
 * Returns { width, height } as numbers. Returns null when the input
 * settings shape doesn't have parseable positive width/height — caller
 * decides what to fall back to (resolvePanelDims uses 128×96; the
 * stream preview lets CSS default 9/16 stick).
 */
export function effectiveDisplayDims(settings) {
    const w = Number(settings?.display_width);
    const h = Number(settings?.display_height);
    const rotation = Number(settings?.display_rotation || 0);
    if (!Number.isFinite(w) || w <= 0) return null;
    if (!Number.isFinite(h) || h <= 0) return null;
    if (rotation === 90 || rotation === 270) {
        return { width: h, height: w };
    }
    return { width: w, height: h };
}

/** Read the local device's flock-self-card payload — model / mode /
 * signal / uptime + a source field. Phase B.1 endpoint; the flock
 * panel's self-card consumes this on mount to replace its hardcoded
 * SELF_PLACEHOLDER_* values with real /proc/* reads. */
export async function getSystemInfo() {
    const response = await apiFetch("/api/system/info");
    if (!response.ok) {
        throw new Error(`System info fetch failed (${response.status})`);
    }
    return await response.json();
}

/** Tailnet auto-discovery for the Add Peer modal — Phase B.5.
 * Returns { candidates: [{address, hostname, already_in_flock}, ...],
 * source: 'tailscale' | 'none' }. Source 'none' means no Tailscale
 * binary on this device; UI falls back to manual-typed entry. */
export async function getFlockDiscover() {
    const response = await apiFetch("/api/flock/discover");
    if (!response.ok) {
        throw new Error(`Flock discover failed (${response.status})`);
    }
    return await response.json();
}

/** Replace the device system settings. */
export async function saveSettings(settings) {
    const response = await apiFetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(settings),
    });
    if (!response.ok) {
        const detail = await extractDetailMessage(response);
        throw new Error(`Save settings failed (${response.status}): ${detail}`);
    }
    return await response.json();
}

/** Replace the schedule with the given object (rules + default_playlist_id). */
export async function saveSchedule(schedule) {
    const response = await apiFetch("/api/schedules", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(schedule),
    });
    if (!response.ok) {
        const detail = await extractDetailMessage(response);
        throw new Error(`Save schedule failed (${response.status}): ${detail}`);
    }
    return await response.json();
}


/* --- Flock: peer openMarquee devices for mesh media sync. --- */

export async function listFlock() {
    const response = await apiFetch("/api/flock");
    if (!response.ok) {
        throw new Error(`List flock failed (${response.status})`);
    }
    return await response.json();
}

export async function addFlockPeer(address) {
    const response = await apiFetch("/api/flock", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ address }),
    });
    if (!response.ok) {
        // 422 bodies are FastAPI validation envelopes ({detail: [...]}); 409
        // bodies are {detail: "..."}. The shared helper handles both shapes.
        // 422 specifically gets a friendlier hand-written message because the
        // raw "expected ASCII / max length" pydantic noise is meaningless to
        // the operator who just typed a bad hostname.
        let message = await extractDetailMessage(response);
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
    const response = await apiFetch(`/api/flock/${peerId}`, {
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
    const response = await apiFetch(`/api/flock/${peerId}`, { method: "DELETE" });
    if (!response.ok) {
        throw new Error(`Remove peer failed (${response.status})`);
    }
}


/* --- Stream: live phone-camera takeover (SYSTEM_SPEC §5.11). --- */

// Negotiation timeout for /start + /takeover. Tailscale on a flat tailnet
// completes the SDP+ICE round trip in well under a second; 15s is a wide
// margin that still surfaces a wedged backend or a lossy WAN link before
// the operator stares at "Connecting…" forever. Without this, an unhealthy
// fetch hangs the panel indefinitely (QA finding from Phase 12.2 review).
const STREAM_NEGOTIATE_TIMEOUT_MS = 15_000;

/**
 * Current stream state. Shape:
 *   { state: "idle" | "active",
 *     session_id: string | null,
 *     tier: { name, max_width, max_height, max_fps } }
 *
 * Polled before Go Live so the panel can switch to a "Take over"
 * affordance when another phone owns the screen — without a separate
 * round trip after the 409.
 */
export async function getStreamStatus() {
    const response = await apiFetch("/api/stream/status");
    if (!response.ok) {
        throw new Error(`Stream status failed (${response.status})`);
    }
    return await response.json();
}

/**
 * Start a stream session. Phone-published SDP offer goes in; SDP answer
 * + session_id come back. Throws a structured error on 409 carrying
 * `active_session_id` so the caller can switch to the take-over flow
 * without re-polling /status.
 */
export async function startStream(sdpOffer) {
    const response = await apiFetch("/api/stream/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sdp_offer: sdpOffer }),
        signal: AbortSignal.timeout(STREAM_NEGOTIATE_TIMEOUT_MS),
    });
    if (response.status === 409) {
        const body = await response.json();
        const detail = body?.detail || {};
        const err = new Error("stream_already_active");
        err.code = detail.error || "stream_already_active";
        err.activeSessionId = detail.active_session_id || null;
        err.status = 409;
        throw err;
    }
    if (!response.ok) {
        const detail = await extractDetailMessage(response);
        throw new Error(`Stream start failed (${response.status}): ${detail}`);
    }
    return await response.json();
}

/**
 * Start a VLC takeover. The Pi pulls the RTSP URL the operator's VLC
 * is publishing — there is no SDP, so the response's `sdp_answer` is
 * null; only `session_id` + `started_at` matter. Throws the same
 * structured 409 error as startStream when another source already
 * owns the screen.
 */
export async function startRtspStream(url) {
    const response = await apiFetch("/api/stream/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: "rtsp", url }),
        signal: AbortSignal.timeout(STREAM_NEGOTIATE_TIMEOUT_MS),
    });
    if (response.status === 409) {
        const body = await response.json();
        const detail = body?.detail || {};
        const err = new Error("stream_already_active");
        err.code = detail.error || "stream_already_active";
        err.activeSessionId = detail.active_session_id || null;
        err.status = 409;
        throw err;
    }
    if (!response.ok) {
        const detail = await extractDetailMessage(response);
        throw new Error(`Stream start failed (${response.status}): ${detail}`);
    }
    return await response.json();
}

/**
 * Force-stop any active session and start a new one in the same call.
 * Same response shape as startStream — phone applies the answer.
 */
export async function takeoverStream(sdpOffer) {
    const response = await apiFetch("/api/stream/takeover", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sdp_offer: sdpOffer }),
        signal: AbortSignal.timeout(STREAM_NEGOTIATE_TIMEOUT_MS),
    });
    if (!response.ok) {
        const detail = await extractDetailMessage(response);
        throw new Error(`Stream takeover failed (${response.status}): ${detail}`);
    }
    return await response.json();
}

/**
 * Force-stop any active session and start a VLC takeover in the same
 * call. Same response shape as startRtspStream (sdp_answer is null).
 */
export async function takeoverRtspStream(url) {
    const response = await apiFetch("/api/stream/takeover", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: "rtsp", url }),
        signal: AbortSignal.timeout(STREAM_NEGOTIATE_TIMEOUT_MS),
    });
    if (!response.ok) {
        const detail = await extractDetailMessage(response);
        throw new Error(`Stream takeover failed (${response.status}): ${detail}`);
    }
    return await response.json();
}

/**
 * Stop a stream by session id. 404 if it isn't the currently-active
 * session — caller's local state is stale; usually safe to swallow.
 */
export async function stopStream(sessionId) {
    const response = await apiFetch("/api/stream/stop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId }),
    });
    if (!response.ok && response.status !== 404) {
        const detail = await extractDetailMessage(response);
        throw new Error(`Stream stop failed (${response.status}): ${detail}`);
    }
}
