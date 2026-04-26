// Mock backend for the openMarquee demo.
//
// Intercepts every fetch('/api/...') the real UI makes and either:
//   - returns canned GET data from demo/seed.json + demo/assets/ on disk
//   - applies mutations (playlist edits, sync toggles, etc.) to a
//     localStorage-backed state bundle
//   - 403s all upload-shaped routes so visitors can't push content
//     into a publicly-shared demo.
//
// Everything lives per-visitor. A "Reset demo" button clears the
// localStorage key so the seed reloads fresh.
//
// Required load order: this script must register its fetch wrapper
// BEFORE main.js imports api.js — demo/index.html loads it with a
// synchronous <script> tag placed above the module script.

(() => {
    const LS_KEY = "openmarquee-demo:v1";
    const SEED_URL = "./seed.json";
    const ASSET_BASE = "./assets/";

    // In-memory copies; populated from localStorage (or seed) during init.
    let state = null;
    let seed = null;
    let readyPromise = null;

    const nativeFetch = window.fetch.bind(window);

    async function loadSeed() {
        if (seed) return seed;
        const r = await nativeFetch(SEED_URL);
        if (!r.ok) throw new Error(`seed.json fetch failed: ${r.status}`);
        seed = await r.json();
        return seed;
    }

    function loadStoredState() {
        try {
            const raw = localStorage.getItem(LS_KEY);
            if (!raw) return null;
            return JSON.parse(raw);
        } catch {
            return null;
        }
    }

    function saveState() {
        try {
            localStorage.setItem(LS_KEY, JSON.stringify(state));
        } catch {
            /* quota exceeded — fallbacks are fine for a demo */
        }
    }

    async function initState() {
        await loadSeed();
        const stored = loadStoredState();
        // Versioned by seed.generated_at — a new seed roll-out invalidates
        // every visitor's stale state automatically. Falls back to
        // schema_version for older seeds that pre-date the stamp.
        const seedKey = seed.generated_at || `v${seed.schema_version}`;
        if (stored && stored.__demo_version === seedKey) {
            state = stored;
        } else {
            state = freshState(seedKey);
            saveState();
        }
    }

    function freshState(seedKey) {
        // Deep-clone so mutations don't write back into `seed`.
        return {
            __demo_version: seedKey,
            content: clone(seed.content),
            playlists: clone(seed.playlists),
            schedules: clone(seed.schedules),
            settings: clone(seed.settings),
            flock_peers: clone(seed.flock_peers),
            // Simulated "currently playing" — first item of default playlist.
            playback: {
                is_running: true,
                started_at: Date.now(),
            },
        };
    }

    function clone(v) {
        return JSON.parse(JSON.stringify(v));
    }

    function jsonResponse(body, init = {}) {
        return new Response(JSON.stringify(body), {
            status: init.status || 200,
            headers: { "Content-Type": "application/json" },
        });
    }

    function noContent(status = 204) {
        return new Response(null, { status });
    }

    function forbidden(detail) {
        return jsonResponse({ detail }, { status: 403 });
    }

    function notFound(detail) {
        return jsonResponse({ detail }, { status: 404 });
    }

    // --- playback simulation -----------------------------------------------
    // The demo rotates through the default playlist on a clock so
    // /api/playback/state returns a plausible live value.

    // Stable UUID for the default playlist — matches backend's
    // openmarquee.playlist.DEFAULT_PLAYLIST_ID + ui/src/constants.js.
    const DEFAULT_PLAYLIST_ID = "00000000-0000-4000-8000-000000000001";

    function findPlaylistById(playlistId) {
        const list = state.playlists.playlists || [];
        return list.find((p) => String(p.id) === String(playlistId)) || null;
    }

    function defaultPlaylistItemIds() {
        const pl = findPlaylistById(DEFAULT_PLAYLIST_ID);
        return (pl?.items || []).map((it) => it.item_id);
    }

    function currentPlaybackItem() {
        const ids = defaultPlaylistItemIds();
        if (!ids.length) return null;
        const items = state.content.filter((c) => ids.includes(c.id));
        if (!items.length) return null;
        // Each slide plays for its own duration_ms. Cycle based on elapsed time.
        const elapsed = Date.now() - (state.playback.started_at || Date.now());
        let acc = 0;
        const totals = items.map((i) => i.duration_ms || 5000);
        const totalMs = totals.reduce((a, b) => a + b, 0);
        const phase = elapsed % totalMs;
        for (let i = 0; i < items.length; i++) {
            acc += totals[i];
            if (phase < acc) return items[i];
        }
        return items[0];
    }

    function firstDefaultPlaylistItem() {
        const ids = defaultPlaylistItemIds();
        if (!ids.length) return null;
        return state.content.find((c) => c.id === ids[0]) || null;
    }

    // --- asset fetching ----------------------------------------------------
    // Baked PNGs live under ./assets/<id>.png (.mp4 for videos). Return
    // a Response whose body is the file contents served through the
    // native fetch.

    async function assetResponse(id, ext = "png", headers = {}) {
        const r = await nativeFetch(`${ASSET_BASE}${id}.${ext}`);
        if (!r.ok) return notFound(`no asset for ${id}`);
        // Clone the response so we can set CORS/cache headers cleanly.
        const blob = await r.blob();
        const mime = ext === "mp4" ? "video/mp4" : "image/png";
        return new Response(blob, {
            status: 200,
            headers: { "Content-Type": mime, ...headers },
        });
    }

    // --- route table -------------------------------------------------------

    async function route(url, request) {
        const { pathname, searchParams } = url;
        const method = request.method.toUpperCase();

        // Only intercept /api/* and /healthz + /dev/*. Let ./seed.json,
        // ./assets/*, and /dist/* pass through to the native fetch.
        if (!/^\/(api|healthz|dev)(\/|$)/.test(pathname)) return null;

        // Health check.
        if (pathname === "/healthz") {
            return jsonResponse({ status: "alive", version: "demo" });
        }

        // --- content read path ---
        if (pathname === "/api/content" && method === "GET") {
            return jsonResponse(orderedContent());
        }
        const contentIdMatch = pathname.match(
            /^\/api\/content\/([^/]+)(\/(asset|video|duration))?$/,
        );
        if (contentIdMatch) {
            const [, id, , sub] = contentIdMatch;
            const item = state.content.find((c) => c.id === id);
            if (!sub && method === "GET") {
                return item ? jsonResponse(item) : notFound("no content");
            }
            if (sub === "asset" && method === "GET") {
                return assetResponse(id);
            }
            if (sub === "video" && method === "GET") {
                return assetResponse(id, "mp4");
            }
            if (sub === "duration" && method === "PATCH") {
                if (!item) return notFound("no content");
                const body = await request.json();
                item.duration_ms = Number(body.duration_ms) || item.duration_ms;
                saveState();
                return jsonResponse(item);
            }
            if (method === "DELETE") {
                return forbidden("This is a read-only demo — content can't be deleted.");
            }
        }

        // --- content write path: blocked ---
        if (
            (pathname === "/api/content/text-slides" && method === "POST") ||
            (pathname === "/api/content/images" && method === "POST") ||
            (pathname === "/api/content/videos" && method === "POST") ||
            /^\/api\/content\/(text-slides|images|videos)\/[^/]+$/.test(pathname) ||
            pathname === "/api/backgrounds/generate"
        ) {
            return forbidden("Uploads are disabled in the demo.");
        }

        // --- backgrounds: providers list (read-only) ---
        if (pathname === "/api/backgrounds/providers" && method === "GET") {
            // Empty list disables the AI-generate UI gracefully — the bg
            // picker shows the provider dropdown empty rather than 404ing.
            return jsonResponse({ providers: [] });
        }

        // --- playback ---
        if (pathname === "/api/playback/state" && method === "GET") {
            const cur = currentPlaybackItem();
            return jsonResponse({
                is_running: true,
                current_item_id: cur?.id || null,
                current_item_type: cur?.type || null,
                current_item_transition: cur?.transition || null,
                current_item_transition_ms: cur?.transition_ms || null,
                current_item_auto_mode: cur?.auto_mode ?? null,
                current_item_auto_format: cur?.auto_format ?? null,
                current_playlist_id: DEFAULT_PLAYLIST_ID,
            });
        }
        if (pathname === "/api/playback/current-thumbnail" && method === "GET") {
            const first = firstDefaultPlaylistItem();
            if (!first) return noContent(204);
            return assetResponse(first.id, "png", { "Cache-Control": "no-store" });
        }
        if (pathname === "/api/playback/start" && method === "POST") {
            state.playback.is_running = true;
            state.playback.started_at = Date.now();
            saveState();
            return noContent();
        }
        if (pathname === "/api/playback/stop" && method === "POST") {
            state.playback.is_running = false;
            saveState();
            return noContent();
        }
        if (/^\/dev\/play\/[^/]+$/.test(pathname)) {
            return noContent(204);
        }

        // --- playlists (UUID-keyed v4 collection) ---
        if (pathname === "/api/playlists" && method === "GET") {
            return jsonResponse(state.playlists);
        }
        if (pathname === "/api/playlists" && method === "POST") {
            const body = await request.json();
            const items = normalizePlaylistItems(body);
            const playlist = {
                id: uuid4(),
                name: String(body.name || ""),
                items,
                item_ids: items.map((e) => e.item_id),
            };
            state.playlists.playlists = state.playlists.playlists || [];
            state.playlists.playlists.push(playlist);
            saveState();
            return jsonResponse(playlist, { status: 201 });
        }
        // Legacy single-playlist shortcut — operates on the default by id.
        if (pathname === "/api/playlist" && method === "GET") {
            const pl = findPlaylistById(DEFAULT_PLAYLIST_ID);
            return jsonResponse(pl || {
                id: DEFAULT_PLAYLIST_ID,
                name: "default",
                items: [],
                item_ids: [],
            });
        }
        if (pathname === "/api/playlist" && method === "PUT") {
            const body = await request.json();
            const items = normalizePlaylistItems(body);
            const existing = findPlaylistById(DEFAULT_PLAYLIST_ID);
            const playlist = {
                id: DEFAULT_PLAYLIST_ID,
                name: existing?.name || "default",
                items,
                item_ids: items.map((e) => e.item_id),
            };
            upsertPlaylist(playlist);
            saveState();
            return jsonResponse(playlist);
        }
        const playlistMatch = pathname.match(/^\/api\/playlists\/([^/]+)$/);
        if (playlistMatch) {
            const playlistId = decodeURIComponent(playlistMatch[1]);
            const existing = findPlaylistById(playlistId);
            if (method === "GET") {
                if (!existing) return notFound(`no playlist with id ${playlistId}`);
                return jsonResponse(existing);
            }
            if (method === "PUT") {
                const body = await request.json();
                const items = normalizePlaylistItems(body);
                const playlist = {
                    id: playlistId,
                    // Preserve existing name when caller doesn't override.
                    name:
                        body.name !== undefined && body.name !== null
                            ? String(body.name)
                            : existing?.name || "",
                    items,
                    item_ids: items.map((e) => e.item_id),
                };
                upsertPlaylist(playlist);
                saveState();
                return jsonResponse(playlist);
            }
            if (method === "DELETE") {
                if (!existing) return notFound(`no playlist with id ${playlistId}`);
                state.playlists.playlists = (state.playlists.playlists || []).filter(
                    (p) => String(p.id) !== String(playlistId),
                );
                saveState();
                return noContent();
            }
        }

        // --- schedules ---
        if (pathname === "/api/schedules" && method === "GET") {
            return jsonResponse(state.schedules);
        }
        if (pathname === "/api/schedules" && method === "PUT") {
            const body = await request.json();
            state.schedules = body;
            saveState();
            return jsonResponse(body);
        }

        // --- settings ---
        if (pathname === "/api/settings" && method === "GET") {
            return jsonResponse(state.settings);
        }
        if (pathname === "/api/settings" && method === "PUT") {
            const body = await request.json();
            state.settings = { ...state.settings, ...body };
            saveState();
            return jsonResponse(state.settings);
        }
        if (pathname === "/api/system/display-dims" && method === "GET") {
            return jsonResponse({ width: 1920, height: 1080, source: "demo" });
        }
        if (pathname === "/api/system/wifi-scan" && method === "GET") {
            return jsonResponse({
                networks: [
                    { ssid: "Kitchen WiFi", signal_dbm: -52 },
                    { ssid: "Guest", signal_dbm: -68 },
                ],
            });
        }

        // --- flock ---
        if (pathname === "/api/flock" && method === "GET") {
            return jsonResponse({ schema_version: 1, peers: state.flock_peers });
        }
        if (pathname === "/api/flock" && method === "POST") {
            const body = await request.json();
            const addr = String(body.address || "").trim().toLowerCase();
            if (!addr) {
                return jsonResponse(
                    { detail: "address required" },
                    { status: 422 },
                );
            }
            if (state.flock_peers.some((p) => p.address === addr)) {
                return jsonResponse(
                    { detail: `peer ${addr} already in flock` },
                    { status: 409 },
                );
            }
            const peer = {
                id: crypto.randomUUID(),
                address: addr,
                name: null,
                sync: false,
                added_at: new Date().toISOString(),
                last_seen_at: null,
            };
            state.flock_peers.push(peer);
            saveState();
            return jsonResponse(peer, { status: 201 });
        }
        const flockPatchMatch = pathname.match(/^\/api\/flock\/([^/]+)$/);
        if (flockPatchMatch) {
            const id = flockPatchMatch[1];
            const peer = state.flock_peers.find((p) => p.id === id);
            if (!peer) return notFound("no peer");
            if (method === "PATCH") {
                const body = await request.json();
                if (body.sync !== undefined) peer.sync = !!body.sync;
                if (body.name !== undefined) peer.name = body.name;
                saveState();
                return jsonResponse(peer);
            }
            if (method === "DELETE") {
                state.flock_peers = state.flock_peers.filter((p) => p.id !== id);
                saveState();
                return noContent();
            }
        }
        // Fake-peer current-thumbnail: the Flock tile polls
        // http://<peer-address>/api/playback/current-thumbnail. In the
        // demo those URLs would fail (cross-origin, unreachable), so
        // the UI falls back to the "Not playing" overlay. To give the
        // demo some life, we intercept ABSOLUTE-origin requests for
        // fake peer addresses and return their assigned thumbnail.

        return null;  // let through
    }

    // When the UI polls peer thumbnails it uses a full http:// URL with
    // the peer's address. We can't intercept those through the normal
    // pathname-based router — they'd hit the remote origin and fail.
    // Match them here instead.
    async function maybeServeFakePeerThumb(request) {
        let u;
        try {
            u = new URL(request.url);
        } catch {
            return null;
        }
        const sameOrigin = u.origin === location.origin;
        if (sameOrigin) return null;
        const peer = state.flock_peers.find(
            (p) => p.address && `http://${p.address}` === u.origin,
        );
        if (!peer) return null;
        if (u.pathname !== "/api/playback/current-thumbnail") return null;
        if (peer.current_thumbnail_content_id) {
            return assetResponse(peer.current_thumbnail_content_id, "png", {
                "Cache-Control": "no-store",
            });
        }
        return noContent(204);
    }

    function normalizePlaylistItems(body) {
        // Accept both the canonical `items` shape (objects with transition
        // metadata) and the legacy `item_ids` shape (UUID strings).
        if (Array.isArray(body.items)) {
            return body.items.map((e) => ({
                item_id: String(e.item_id),
                transition: e.transition || "cut",
                transition_ms: Number(e.transition_ms) || 500,
            }));
        }
        if (Array.isArray(body.item_ids)) {
            return body.item_ids.map((id) => ({
                item_id: String(id),
                transition: "cut",
                transition_ms: 500,
            }));
        }
        return [];
    }

    function upsertPlaylist(playlist) {
        const list = state.playlists.playlists || [];
        const idx = list.findIndex((p) => String(p.id) === String(playlist.id));
        if (idx >= 0) list[idx] = playlist;
        else list.push(playlist);
        state.playlists.playlists = list;
    }

    function uuid4() {
        // Tiny RFC 4122 v4 generator — adequate for demo state.
        return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
            const r = (Math.random() * 16) | 0;
            const v = c === "x" ? r : (r & 0x3) | 0x8;
            return v.toString(16);
        });
    }

    function orderedContent() {
        // Mirror list_in_playlist_order(..., include_orphans=True):
        // default playlist's items first (in playlist order, with
        // patched transitions), then every storage item not referenced
        // by ANY playlist (sorted by id). UI pallets + bg-picker expect
        // the full library; playback stays strict on its own code path.
        const list = state.playlists.playlists || [];
        const pl = findPlaylistById(DEFAULT_PLAYLIST_ID) || { items: [] };
        const byId = new Map(state.content.map((c) => [c.id, c]));
        const ordered = [];
        const used = new Set();
        for (const entry of pl.items || []) {
            const item = byId.get(entry.item_id);
            if (!item || used.has(entry.item_id)) continue;
            used.add(entry.item_id);
            ordered.push({
                ...item,
                transition: entry.transition,
                transition_ms: entry.transition_ms,
            });
        }
        const allReferenced = new Set();
        // collection is now a list of playlists (v4 shape).
        for (const p of list) {
            for (const e of p.items || []) allReferenced.add(e.item_id);
        }
        const orphans = state.content
            .filter((c) => !used.has(c.id) && !allReferenced.has(c.id))
            .sort((a, b) => String(a.id).localeCompare(String(b.id)));
        ordered.push(...orphans);
        return ordered;
    }

    // Install the fetch wrapper.
    window.fetch = async function (input, fetchInit) {
        if (!readyPromise) readyPromise = initState();
        await readyPromise;

        const request =
            input instanceof Request ? input : new Request(input, fetchInit);
        // Cross-origin fake-peer thumbnail lookups.
        const fake = await maybeServeFakePeerThumb(request);
        if (fake) return fake;
        let url;
        try {
            url = new URL(request.url);
        } catch {
            return nativeFetch(input, fetchInit);
        }
        if (url.origin !== location.origin) {
            return nativeFetch(input, fetchInit);
        }
        const handled = await route(url, request);
        if (handled) return handled;
        return nativeFetch(input, fetchInit);
    };

    // Expose a reset hook for the demo's "Reset" button.
    window.__openmarquee_demo_reset = function () {
        localStorage.removeItem(LS_KEY);
        location.reload();
    };
})();
