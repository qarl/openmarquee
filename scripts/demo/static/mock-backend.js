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
        migrateSeedToV3(seed);
        return seed;
    }

    // §5.10a v3 (qarl 2026-05-01): TextSlide carries a `text_layers`
    // list instead of flat text / text_color / font_* / auto_* fields.
    // Old seed snapshots predate the migration; rewrite text_slide
    // entries to the layered shape on load so the demo doesn't ship a
    // broken editor until generate-seed.py is re-run against a v3
    // backend.
    function migrateSeedToV3(seedData) {
        for (const item of seedData.content || []) {
            if (item.type !== "text_slide") continue;
            if (Array.isArray(item.text_layers) && item.text_layers.length > 0) {
                continue; // already v3
            }
            item.text_layers = [
                {
                    text: item.text || "",
                    font_family: item.font_family || null,
                    font_size_px: item.font_size_px || null,
                    font_size_pct: item.font_size_pct || null,
                    text_color: item.text_color || "#FFFFFF",
                    auto_mode: item.auto_mode || null,
                    auto_format: item.auto_format || null,
                    box: item.box || { x: 0.1, y: 0.1, w: 0.8, h: 0.8 },
                },
            ];
            // Drop the legacy flat fields so the wire shape matches what
            // a real v3 backend serves; bg + slide-level fields stay.
            delete item.text;
            delete item.font_family;
            delete item.font_size_px;
            delete item.font_size_pct;
            delete item.text_color;
            delete item.auto_mode;
            delete item.auto_format;
            delete item.box;
        }
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
        // §5.10a v3 (2026-05-01): bump version key so visitors stuck on
        // pre-layered stored state get a fresh seed → next page load
        // shows them a working editor.
        const seedKey = `v3-${seed.generated_at || `v${seed.schema_version}`}`;
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
            // Stream takeover session shape: { id, started_at } when
            // active, null when idle. Set by POST /api/stream/start
            // and /takeover, cleared by /stop. Returned through
            // /api/stream/status (Phase A.2 wire shape).
            _streamSession: null,
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

        // PUT /api/content/text-slides/{id} — in-memory update so the
        // demo's autosave round-trips cleanly. Without this every layer
        // edit (text/color/font/motion/dynamic-source/box-drag) 403s,
        // updated_at never advances, and tile-thumb cachebust freezes
        // (qarl 2026-05-01 review #4 caught this — local uvicorn worked
        // fine, demo was broken). Asset.png CAN'T re-rasterize here
        // (no Python in the browser), so the stored thumbnail shows
        // whatever it showed at seed time — but the wire shape matches
        // what a real backend serves, the cachebust bumps, and the
        // operator's edit persists in-session.
        const textSlidePutMatch = pathname.match(
            /^\/api\/content\/text-slides\/([^/]+)$/,
        );
        if (textSlidePutMatch && method === "PUT") {
            const id = textSlidePutMatch[1];
            const item = state.content.find((c) => c.id === id);
            if (!item || item.type !== "text_slide") {
                return notFound("no text slide");
            }
            let body;
            try {
                body = await request.json();
            } catch {
                return forbidden("invalid JSON body");
            }
            // Merge slide-level fields + replace text_layers wholesale.
            // png_base64 + the wire-side validators are skipped — the
            // mock trusts whatever the editor sends, which is fine
            // because the editor IS the only writer.
            for (const key of [
                "name",
                "duration_ms",
                "background_color",
                "background_image_slide_id",
                "background_video_slide_id",
                "transition",
                "transition_ms",
            ]) {
                if (key in body) item[key] = body[key];
            }
            if (Array.isArray(body.text_layers)) {
                item.text_layers = body.text_layers;
            }
            item.updated_at = new Date().toISOString();
            saveState();
            return jsonResponse(item);
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
                // §5.10a v3: auto_mode/auto_format moved onto text_layers.
                // Read off layer[0] for the playback-state surface; falls
                // back to the legacy root-level fields for any image/video
                // slide stub the mock might serve.
                current_item_auto_mode:
                    cur?.text_layers?.[0]?.auto_mode ?? cur?.auto_mode ?? null,
                current_item_auto_format:
                    cur?.text_layers?.[0]?.auto_format ?? cur?.auto_format ?? null,
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
        if (pathname === "/api/flock/discover" && method === "GET") {
            // Phase B.5: magicDNS auto-discovery. Demo doesn't have
            // a Tailnet to enumerate; return a small handcrafted set
            // of plausible Tailnet hostnames so the Add Peer modal
            // shows the affordance live. One is already in the
            // operator's flock (already_in_flock=true), letting the
            // verifier see the disable-already-added behavior; one
            // is fresh.
            const known = new Set(
                (state.flock_peers || []).map((p) => p.address),
            );
            const synthetic = [
                { hostname: "lobby", address: "lobby.ts.net" },
                { hostname: "back-room", address: "back-room.ts.net" },
            ];
            return jsonResponse({
                source: "tailscale",
                candidates: synthetic.map((c) => ({
                    ...c,
                    already_in_flock: known.has(c.address),
                })),
            });
        }
        if (pathname === "/api/flock/hello" && method === "POST") {
            // Phase B gossip-on-add (§13) — receiver-side parity with
            // the production /api/flock/hello. A peer is introducing
            // itself (or another peer) to us; we add the address to
            // local flock state if not already present and DO NOT
            // cascade further (loop-prevention invariant — gossip
            // fan-out only fires from the operator-driven POST
            // /api/flock entry point, never on inbound hellos).
            //
            // Idempotent: duplicate hellos for the same address are
            // 204 no-ops, since gossip races can introduce the same
            // peer twice (once via reciprocal, once via forward).
            const helloBody = await request.json().catch(() => ({}));
            const helloAddr = String(helloBody.address || "").trim().toLowerCase();
            if (!helloAddr) {
                return jsonResponse(
                    { detail: "address required" },
                    { status: 422 },
                );
            }
            if (!state.flock_peers.some((p) => p.address === helloAddr)) {
                // Mirror the FlockPeer shape the demo uses elsewhere.
                state.flock_peers.push({
                    id: crypto.randomUUID(),
                    address: helloAddr,
                    name: null,
                    sync: false,
                    added_at: new Date().toISOString(),
                    last_seen_at: null,
                    model: null,
                    mode: null,
                    signal: null,
                    uptime: null,
                    items_behind: null,
                });
            }
            return new Response(null, { status: 204 });
        }
        if (pathname === "/api/system/info" && method === "GET") {
            // Phase B.1 endpoint. Demo doesn't have a real /proc/* to
            // read; report the same SELF_PLACEHOLDER-shaped values the
            // backend's _FALLBACK_* sentinels emit on a Mac dev box.
            // 'source: "demo"' tags this distinctly from production's
            // 'fallback' so a verifier probe can tell them apart.
            return jsonResponse({
                model: "Pi Zero 2 W",
                mode: "hdmi-1080",
                signal: 100,
                uptime: "up since boot",
                source: "demo",
                // Demo defaults to landscape 1920×1080, no rotation.
                // Real seed snapshots can override (regen seed.json).
                display_width: 1920,
                display_height: 1080,
                display_rotation: 0,
            });
        }

        // --- stream (live phone-camera takeover, SYSTEM_SPEC §5.11) ---
        // simulateOnly mode in the panel skips the /start /stop
        // /takeover round trips entirely — they're never called from
        // a demo-mode bundle. /status IS still called as the panel's
        // pre-flight check, so we answer it here. Always idle so the
        // operator gets the standard Go Live flow rather than the
        // take-over branch (which works, but is less interesting as a
        // first-look demo).
        // /api/stream/{status,start,stop,takeover} — wire-shape match
        // with the real backend. The current demo bundle is mounted
        // with simulateOnly=true (see ui/src/main.js wiring against
        // isDemoMode), so /start /stop /takeover are bypassed and
        // only /status is exercised today. The full handler set is
        // here anyway so a future demo that flips simulateOnly=false
        // (e.g. to exercise A.2's wire-driven Elapsed semantics)
        // works against the mock without further changes.
        //
        // Session state lives on _streamSession at module scope of
        // this handler closure — analogous to the per-process state
        // a real backend's StreamManager owns. Same in-process
        // stamping pattern as /api/flock's last_seen_at fix.
        const HARDWARE_TIER = {
            name: "basic",
            max_width: 854,
            max_height: 480,
            max_fps: 30,
        };
        if (pathname === "/api/stream/status" && method === "GET") {
            const session = state._streamSession;
            return jsonResponse({
                state: session ? "active" : "idle",
                session_id: session ? session.id : null,
                // Phase A.2: started_at is the wall-clock UTC ISO 8601
                // timestamp at session creation, null when idle.
                started_at: session ? session.started_at : null,
                tier: HARDWARE_TIER,
            });
        }
        if (pathname === "/api/stream/start" && method === "POST") {
            if (state._streamSession) {
                return jsonResponse(
                    {
                        detail: {
                            error: "stream_already_active",
                            active_session_id: state._streamSession.id,
                        },
                    },
                    { status: 409 },
                );
            }
            state._streamSession = {
                id: crypto.randomUUID(),
                started_at: new Date().toISOString(),
            };
            return jsonResponse({
                session_id: state._streamSession.id,
                sdp_answer: "v=0\r\nfake-mock-answer\r\n",
                started_at: state._streamSession.started_at,
            });
        }
        if (pathname === "/api/stream/takeover" && method === "POST") {
            // Force-replace any existing session with a fresh one.
            state._streamSession = {
                id: crypto.randomUUID(),
                started_at: new Date().toISOString(),
            };
            return jsonResponse({
                session_id: state._streamSession.id,
                sdp_answer: "v=0\r\nfake-mock-answer\r\n",
                started_at: state._streamSession.started_at,
            });
        }
        if (pathname === "/api/stream/stop" && method === "POST") {
            const body = await request.json().catch(() => ({}));
            if (
                !state._streamSession ||
                state._streamSession.id !== body.session_id
            ) {
                return jsonResponse(
                    { detail: `no active session ${body.session_id}` },
                    { status: 404 },
                );
            }
            state._streamSession = null;
            return new Response(null, { status: 204 });
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
            // Stamp last_seen_at = now on every peer at GET time so the
            // demo's flock panel renders peers as ONLINE regardless of
            // how long the visitor has had the demo open. The 2026-04-28
            // flock-panel rewrite reads peer freshness from a 30s window
            // off last_seen_at (real-device semantics: a probe-recently-
            // succeeded signal). Storing a static timestamp in seed.json
            // would expire after 30s; storing null would mark every peer
            // offline forever. Stamping at GET keeps the demo alive
            // indefinitely without seed-shape gymnastics.
            //
            // Phase B.3: synthesize items_behind too. The new
            // FAKE_PEERS in generate-seed.py carry items_behind
            // directly, but seed.json predates the regeneration on
            // many demo deploys; fall back to an address-keyed map
            // so the demo Flock tab exercises the new affordance
            // even on stale seeds. Three cases at a glance: lobby
            // mid-backlog (3), lab-corner caught up (0), cafeteria
            // sync=False (null — UI ignores).
            const ITEMS_BEHIND_FALLBACK = {
                "lobby.ts.net": 3,
                "lab-corner.ts.net": 0,
            };
            const now = new Date().toISOString();
            const peers = (state.flock_peers || []).map((p) => {
                let itemsBehind;
                if (p.items_behind !== undefined) {
                    // Post-regen seed already carries the value.
                    itemsBehind = p.items_behind;
                } else if (p.sync === false) {
                    // sync=False → meaningless; UI hides the
                    // "K behind" affordance and shows "standalone".
                    itemsBehind = null;
                } else {
                    // Fall back to the address-keyed demo map.
                    // Unknown address → null (looks unprobed).
                    itemsBehind = ITEMS_BEHIND_FALLBACK[p.address] ?? null;
                }
                return { ...p, last_seen_at: now, items_behind: itemsBehind };
            });
            return jsonResponse({ schema_version: 1, peers });
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
        if (u.pathname === "/api/playback/current-thumbnail") {
            if (peer.current_thumbnail_content_id) {
                return assetResponse(peer.current_thumbnail_content_id, "png", {
                    "Cache-Control": "no-store",
                });
            }
            return noContent(204);
        }
        if (u.pathname === "/api/system/info") {
            // Demo peers are uniformly landscape 1920×1080 unless the
            // seed snapshot explicitly carries display_* fields per
            // peer. Falls back to the local-mock defaults so the flock
            // panel's per-peer rotation fetch resolves cleanly instead
            // of hitting an unreachable origin.
            return jsonResponse({
                model: peer.model || "Pi Zero 2 W",
                mode: peer.mode || "hdmi-1080",
                signal: peer.signal != null ? peer.signal : 100,
                uptime: peer.uptime || "up since boot",
                source: "demo",
                display_width: peer.display_width || 1920,
                display_height: peer.display_height || 1080,
                display_rotation: peer.display_rotation || 0,
            });
        }
        return null;
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
        // Mirror list_in_playlist_order(..., include_orphans=True) post
        // the 9e7d7b7 backend fix: default playlist's items first (in
        // playlist order, with patched transitions), then every storage
        // item not yet emitted (sorted by id) — items in OTHER playlists
        // AND true orphans alike. Pre-2026-04-28 this only added true
        // orphans, which made non-default playlist content (Freedom's
        // FREE/YOUR/SIGN slides) invisible to the pallets / bg-picker.
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
        const extras = state.content
            .filter((c) => !used.has(c.id))
            .sort((a, b) => String(a.id).localeCompare(String(b.id)));
        ordered.push(...extras);
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
