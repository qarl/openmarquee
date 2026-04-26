// Service worker for the demo. It handles the one thing mock-backend.js
// CAN'T handle from the window scope: <img src="..."> requests, which
// bypass window.fetch entirely. Rewrites:
//   /api/content/<id>/asset                → ./assets/<id>.png
//   /api/content/<id>/video                → ./assets/<id>.mp4
//   /api/playback/current-thumbnail        → first asset of seeded default playlist
//   http://<fake-peer>/api/playback/current-thumbnail → baked asset
//
// The SW is deliberately narrow — only image-shaped paths. Everything
// else (GETs, mutations) stays in mock-backend.js which is easier to
// debug in the window's DevTools.

const SW_VERSION = "2";
const SEED_URL = "./seed.json";
let seedPromise = null;

function loadSeed() {
    if (!seedPromise) {
        seedPromise = fetch(SEED_URL)
            .then((r) => r.json())
            .catch(() => null);
    }
    return seedPromise;
}

self.addEventListener("install", (event) => {
    // Warm the seed cache so the first <img> request doesn't race.
    event.waitUntil(loadSeed());
    self.skipWaiting();
});

self.addEventListener("activate", (event) => {
    event.waitUntil(self.clients.claim());
});

function firstDefaultItemId(seed) {
    // playlists.playlists is a list keyed by UUID id (post commit 577acc9
    // UUID refactor). Find the default by name.
    const list = seed?.playlists?.playlists || [];
    const pl = list.find((p) => p?.name === "default");
    const ids = pl?.item_ids || (pl?.items || []).map((it) => it.item_id);
    return (ids && ids[0]) || null;
}

function fakePeerThumbId(seed, origin) {
    // Fake peer addresses look like "http://lobby.ts.net". Match them
    // to their current_thumbnail_content_id so each tile gets a
    // distinct live-looking image.
    const peers = seed?.flock_peers || [];
    for (const peer of peers) {
        if (peer.address && `http://${peer.address}` === origin) {
            return peer.current_thumbnail_content_id || null;
        }
    }
    return null;
}

async function rewriteForThumbnail(request) {
    const seed = await loadSeed();
    if (!seed) return null;

    const url = new URL(request.url);
    const sameOrigin = url.origin === self.location.origin;

    // /api/content/<id>/asset → ./assets/<id>.png
    // /api/content/<id>/video → ./assets/<id>.mp4
    if (sameOrigin) {
        const assetMatch = url.pathname.match(
            /^\/api\/content\/([^/]+)\/(asset|video)$/,
        );
        if (assetMatch) {
            const [, id, sub] = assetMatch;
            const ext = sub === "video" ? "mp4" : "png";
            return new Request(`./assets/${id}.${ext}`, { method: "GET" });
        }
        if (url.pathname === "/api/playback/current-thumbnail") {
            const id = firstDefaultItemId(seed);
            if (id) return new Request(`./assets/${id}.png`, { method: "GET" });
        }
    }

    // Cross-origin fake peers.
    if (!sameOrigin && url.pathname === "/api/playback/current-thumbnail") {
        const id = fakePeerThumbId(seed, url.origin);
        if (id) {
            const assetUrl = new URL(`./assets/${id}.png`, self.location.href);
            return new Request(assetUrl, { method: "GET" });
        }
    }
    return null;
}

self.addEventListener("fetch", (event) => {
    // Only intercept GETs — writes go through window.fetch and land in
    // mock-backend.js. Also bail on non-http schemes (blob:, data:).
    const { request } = event;
    if (request.method !== "GET") return;
    if (!/^https?:/.test(request.url)) return;

    event.respondWith(
        (async () => {
            const rewritten = await rewriteForThumbnail(request);
            if (rewritten) {
                try {
                    // Forward original request headers (Range matters —
                    // <video> issues range requests for MP4 playback +
                    // seeking, and the asset server returns 206 with
                    // Accept-Ranges/Content-Range that must reach the
                    // media element intact).
                    const upstreamReq = new Request(rewritten, {
                        method: "GET",
                        headers: request.headers,
                    });
                    const r = await fetch(upstreamReq);
                    if (r.ok) {
                        // Preserve upstream headers (Content-Length,
                        // Accept-Ranges, Content-Range, Content-Type),
                        // overriding only Cache-Control so a redeploy
                        // with a different seed doesn't serve stale bytes.
                        const headers = new Headers(r.headers);
                        headers.set("Cache-Control", "no-store");
                        return new Response(r.body, {
                            status: r.status,
                            statusText: r.statusText,
                            headers,
                        });
                    }
                } catch {
                    /* fall through to a 204 */
                }
                // Asset missing → 204 so the UI's "Not playing" overlay
                // shows instead of a broken-image icon.
                return new Response(null, { status: 204 });
            }
            return fetch(request);
        })(),
    );
});
