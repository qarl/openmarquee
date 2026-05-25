import { apiFetch } from "./api.js";

// Flock panel: the fleet dashboard. Per the 2026-04-28 design handoff
// (claude.ai/design): one card per openMarquee device — this one first
// (with a "this device" pulse pill, no Edit, no overflow), then every
// peer added to the flock. Each card carries a live LED-preview-style
// thumbnail polled from the peer's /api/playback/current-thumbnail.
//
// Imperative push is intentionally not a verb in this UI — sync is a
// declarative checkbox on each card, and the per-peer pill ("syncing"
// / "standalone" / "offline") is observed state. The combo lets the
// operator notice intent-vs-reality mismatches without giving them a
// "force push" button that would lie about what the system can do.
//
// Per-peer Edit button just sends the browser to the peer's tailnet
// URL (`location.href = http://<address>/`) — no in-app context swap,
// no privileged "home device". All peers are equal; the upper-left
// sign name re-renders from each device's own /api/settings.
//
// "+ New device" stays at the end of the grid for hand-adding peers
// (Tailscale magic-DNS auto-discovery is Phase B). The × delete from
// the prior tile UI moved into the per-peer overflow ⋯ menu as
// "Forget device" — same outcome, less prominent.

const THUMBNAIL_POLL_MS = 3000;

// last_seen freshness window: peers seen within the last 30 seconds
// are "online". Matches the existing flock-card chrome polling
// behavior so the two views stay consistent.
const ONLINE_THRESHOLD_MS = 30_000;

// Pretty labels for the design's mode slugs ("hub75-128x64", etc.).
// Falls back to the slug verbatim when an unknown mode comes in from
// a peer running a newer firmware. Phase A peers all report null
// (mode is Phase-B-populated), so this is mostly forward-looking.
const MODE_LABELS = {
    "hub75-64x32": "HUB75 64×32",
    "hub75-128x64": "HUB75 128×64",
    "hdmi-720": "HDMI 720p",
    "hdmi-1080": "HDMI 1080p",
    "hdmi-4k": "HDMI 4K",
    // Backend's /api/system/info emits "ws281x-strip" (matching the
    // OutputMode literal "ws281x"); legacy "ws2812-strip" key kept
    // alive for any peer-supplied slugs that pre-date the rename.
    "ws281x-strip": "WS2812B strip",
    "ws2812-strip": "WS2812B strip",
    "composite": "Composite",
};

const SECTION_TEMPLATE = `
    <section class="flock">
        <div class="om-page-head">
            <div>
                <span class="om-eyebrow flock-eyebrow"></span>
                <h1>Flock</h1>
                <p>Every openMarquee on your local network. Sync media to one or all — works offline, no cloud.</p>
            </div>
        </div>

        <div class="om-flock-grid" role="list"></div>

        <p class="flock-status" role="status" aria-live="polite"></p>
    </section>

    <dialog class="flock-modal" aria-labelledby="flock-modal-title">
        <form method="dialog" class="flock-modal-form">
            <h3 id="flock-modal-title">Add device</h3>
            <div class="flock-discover-section" hidden>
                <p class="flock-discover-label">On your Tailnet:</p>
                <ul class="flock-discover-list"></ul>
                <p class="flock-discover-divider">— or type one manually —</p>
            </div>
            <label class="field">
                <span>Tailscale hostname or IP</span>
                <input type="text" class="flock-address" maxlength="253"
                       placeholder="lobby.tailnet-xyz.ts.net" required>
            </label>
            <p class="flock-modal-hint">
                Optional <code>:port</code> allowed (for non-default HTTP ports).
            </p>
            <p class="flock-modal-error" role="alert"></p>
            <div class="flock-modal-actions">
                <button type="button" class="flock-modal-cancel">Cancel</button>
                <button type="submit" class="primary flock-modal-submit">Add</button>
            </div>
        </form>
    </dialog>
`;

function escapeAttr(s) {
    return String(s).replace(/[&"<>]/g, (c) =>
        ({ "&": "&amp;", '"': "&quot;", "<": "&lt;", ">": "&gt;" }[c]),
    );
}

// "X minute ago" → "Xm ago"; "0 minutes" → "just now". Single-source
// helper for the eyebrow's freshness signal.
function relativeMinutes(when) {
    if (!when) return null;
    const ts = new Date(when).getTime();
    if (Number.isNaN(ts)) return null;
    const minutes = Math.max(0, Math.round((Date.now() - ts) / 60_000));
    if (minutes <= 0) return "just now";
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    return `${Math.round(hours / 24)}d ago`;
}

function isPeerOnline(peer) {
    if (!peer.last_seen_at) return false;
    const ts = new Date(peer.last_seen_at).getTime();
    if (Number.isNaN(ts)) return false;
    return Date.now() - ts < ONLINE_THRESHOLD_MS;
}

// Phase A pill taxonomy:
//   - "syncing"    sync=true && online
//   - "sync paused" sync=true && offline (intent on, reality off)
//   - "standalone" sync=false && online
//   - "offline"    sync=false && offline
// Sync-state pill on the peer card. When sync=True and the peer is
// reachable, surface the items_behind count in place of the generic
// 'syncing' label whenever there's a backlog (Phase B.3 — items_behind
// is populated by the backend's pull_from_peer pre-apply count). When
// the operator is caught up (items_behind = 0), the pill stays at
// 'syncing'. items_behind = null means "never pulled" or "sync just
// flipped on" — display as plain 'syncing' until the first pull
// lands a real number.
function syncStatePillForPeer(peer) {
    const online = isPeerOnline(peer);
    if (peer.sync && online) {
        if (peer.items_behind != null && peer.items_behind > 0) {
            const noun = peer.items_behind === 1 ? "item" : "items";
            return { label: `${peer.items_behind} ${noun} behind`, cls: "good" };
        }
        return { label: "syncing", cls: "good" };
    }
    if (peer.sync && !online) return { label: "sync paused", cls: "bad" };
    if (!peer.sync && online) return { label: "standalone", cls: "" };
    return { label: "offline", cls: "bad" };
}

// Self pill mirrors the global flock_sync_enabled flag: when on, we
// participate in pushes/pulls; when off, we're islanded by intent.
function syncStatePillForSelf(syncEnabled) {
    if (syncEnabled) return { label: "syncing", cls: "good" };
    return { label: "standalone", cls: "" };
}

// Cards have a thumb area we render with a real PNG from the peer's
// /api/playback/current-thumbnail (or the local one for self). The
// existing flock chrome polled the same endpoint — kept verbatim
// since the design's LedPreview component is for the editor's
// what-if simulator, not for live thumbs.
function thumbnailUrl(address, selfOrigin) {
    const base = selfOrigin ? "" : `http://${address}`;
    return `${base}/api/playback/current-thumbnail?t=${Date.now()}`;
}

// Default impl for fetchPeerSystemInfo. Tests inject their own. The
// peer's /api/system/info is the source of truth for that peer's
// rotation-applied display dims; the flock card reads it once on
// panel render and pushes the resulting aspect onto the thumb.
//
// 3s AbortController bound — a black-holed peer (TCP timeout) would
// otherwise leave the promise hanging until the page closes, and each
// re-render piles on more.
const PEER_INFO_TIMEOUT_MS = 3_000;
async function defaultFetchPeerSystemInfo(address) {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), PEER_INFO_TIMEOUT_MS);
    try {
        const r = await fetch(`http://${address}/api/system/info`, {
            signal: ctl.signal,
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return await r.json();
    } finally {
        clearTimeout(t);
    }
}

// Self-card telemetry fallbacks. Used when /api/system/info hasn't
// loaded yet (mount-time gap before the fetch lands) or when it
// fails (older backend, network blip). Phase B.1 endpoint provides
// the production values; B.2 wired the consumer call. Sentinels are
// kept in sync with backend's _FALLBACK_{MODEL,SIGNAL,UPTIME} in
// api_system.py — the wire shape is the contract; both sides need
// the same fallback so a partial-failure render reads consistent.
const SELF_PLACEHOLDER_MODEL = "Pi Zero 2 W";
const SELF_PLACEHOLDER_SIGNAL = 100;
const SELF_PLACEHOLDER_UPTIME = "up since boot";

function selfCardHTML({
    name,
    syncEnabled,
    mode,
    model = SELF_PLACEHOLDER_MODEL,
    signal = SELF_PLACEHOLDER_SIGNAL,
    uptime = SELF_PLACEHOLDER_UPTIME,
}) {
    const pill = syncStatePillForSelf(syncEnabled);
    const modeLabel = MODE_LABELS[mode] || (mode || "—");
    return `
        <div class="om-peer-card this" role="listitem" data-origin="self">
            <div class="om-peer-head">
                <span class="om-peer-dot"></span>
                <span class="om-peer-name">${escapeAttr(name)}</span>
                <span class="om-pill live"><span class="om-pulse"></span>this device</span>
            </div>
            <div class="om-peer-thumb">
                <img class="om-peer-thumb-img" alt="Currently playing" data-origin="self">
                <div class="om-peer-thumb-empty">— idle —</div>
            </div>
            <div class="om-peer-stats">
                <span>${escapeAttr(modeLabel)}</span>
                <span style="text-align:right;"><b>${signal}%</b> wifi</span>
                <span>${escapeAttr(model)}</span>
                <span style="text-align:right;">${escapeAttr(uptime)}</span>
            </div>
            <div class="om-peer-actions">
                <span class="om-pill ${pill.cls}">${escapeAttr(pill.label)}</span>
                <label class="om-peer-sync-toggle">
                    <input type="checkbox" class="flock-self-sync-input"${syncEnabled ? " checked" : ""}>
                    <span>Sync</span>
                </label>
            </div>
        </div>
    `;
}

function peerCardHTML(peer) {
    const online = isPeerOnline(peer);
    const offlineClass = online ? "" : " offline";
    const pill = syncStatePillForPeer(peer);
    const label = peer.name || peer.address;
    const modeLabel = MODE_LABELS[peer.mode] || (peer.mode || "—");
    const signal = peer.signal != null ? `<b>${peer.signal}%</b> wifi` : "—";
    const model = peer.model || "—";
    const uptime = peer.uptime ? `up <b>${escapeAttr(peer.uptime)}</b>` : "—";
    return `
        <div class="om-peer-card${offlineClass}" role="listitem"
             data-peer-id="${escapeAttr(peer.id)}"
             data-address="${escapeAttr(peer.address)}">
            <div class="om-peer-head">
                <span class="om-peer-dot${online ? "" : " off"}"></span>
                <span class="om-peer-name">${escapeAttr(label)}</span>
                ${online ? "" : `<span class="om-pill bad">offline</span>`}
            </div>
            <div class="om-peer-thumb">
                <img class="om-peer-thumb-img" alt="Currently playing on ${escapeAttr(label)}">
                <div class="om-peer-thumb-empty">${online ? "— idle —" : "no signal"}</div>
            </div>
            <div class="om-peer-stats">
                <span>${escapeAttr(modeLabel)}</span>
                <span style="text-align:right;">${signal}</span>
                <span>${escapeAttr(model)}</span>
                <span style="text-align:right;">${uptime}</span>
            </div>
            <div class="om-peer-actions">
                <span class="om-pill ${pill.cls}">${escapeAttr(pill.label)}</span>
                <label class="om-peer-sync-toggle">
                    <input type="checkbox" class="flock-peer-sync-input"${peer.sync ? " checked" : ""}>
                    <span>Sync</span>
                </label>
                <button type="button" class="om-btn sm flock-peer-edit"${online ? "" : " disabled"}>Edit</button>
                <button type="button" class="om-btn sm flock-peer-deflock">Deflock</button>
            </div>
        </div>
    `;
}

function newDeviceCardHTML() {
    return `
        <button type="button" class="om-peer-card flock-new-device" aria-label="Add device">
            <span class="flock-new-plus">+</span>
            <span class="flock-new-label">New device</span>
        </button>
    `;
}

/**
 * Mount the flock panel. Re-rendered idempotently on every external
 * change (sync toggle, peer add/delete, settings change).
 *
 * @param {HTMLElement} container
 * @param {object} options
 * @param {() => Promise<{peers: object[]}>} options.fetchFlock
 * @param {() => Promise<object>} options.fetchSettings
 * @param {() => Promise<object>} [options.fetchSystemInfo]
 *     /api/system/info — Phase B.1. Returns the local device's
 *     {model, mode, signal, uptime, source} for the self-card.
 *     Optional injection seam for tests; defaults to the api.js
 *     wrapper. A failure here falls back to SELF_PLACEHOLDER_*
 *     constants so the panel still renders.
 * @param {() => Promise<{candidates: object[], source: string}>} [options.fetchDiscover]
 *     /api/flock/discover — Phase B.5. Returns Tailnet peers the
 *     operator could add to the flock. Called on Add Peer modal
 *     open. Optional; failure or "none" source hides the discover
 *     list and the modal falls back to manual-typed entry only
 *     (Phase A behavior).
 * @param {(address: string) => Promise<object>} options.onAdd
 * @param {(peerId: string, patch: object) => Promise<object>} options.onUpdate
 * @param {(enabled: boolean) => Promise<void>} options.onUpdateSelfSync
 * @param {(peerId: string) => Promise<void>} options.onDelete
 */
export function mountFlock(
    container,
    {
        fetchFlock,
        fetchSettings,
        fetchSystemInfo,
        fetchDiscover,
        fetchPeerSystemInfo = defaultFetchPeerSystemInfo,
        onAdd,
        onUpdate,
        onUpdateSelfSync,
        onDelete,
    },
) {
    container.innerHTML = SECTION_TEMPLATE;
    const gridEl = container.querySelector(".om-flock-grid");
    const eyebrowEl = container.querySelector(".flock-eyebrow");
    const statusEl = container.querySelector(".flock-status");
    const modal = container.querySelector(".flock-modal");
    const modalForm = modal.querySelector(".flock-modal-form");
    const addressInput = modal.querySelector(".flock-address");
    const modalError = modal.querySelector(".flock-modal-error");
    const modalCancel = modal.querySelector(".flock-modal-cancel");
    const discoverSection = modal.querySelector(".flock-discover-section");
    const discoverList = modal.querySelector(".flock-discover-list");

    let pollTimer = null;
    // Per-peer display-dim cache. Keyed on peer.id (the stable hardware
    // identity — peer.address can churn on DHCP). Display dims are a
    // property of the device, not the panel state, so first-sight fetch
    // is enough; subsequent renders apply from cache without hitting
    // the peer's /api/system/info (which costs a TCP slot up to 3s if
    // the peer is black-holed). Instance-scoped (not module-scoped) so
    // tests that inject their own fetchPeerSystemInfo don't bleed
    // across mount-instances. Cleared in stop() for the panel-close
    // teardown. Stale-on-hardware-swap is an acceptable tradeoff
    // (operator-rare event).
    const peerDimsCache = new Map();

    function setStatus(text, { error = false } = {}) {
        statusEl.textContent = text;
        statusEl.classList.toggle("error", !!error);
    }

    async function loadThumbViaFetch(img, url, { selfOrigin } = {}) {
        // Same blob-via-fetch pattern as the prior flock chrome — goes
        // through any in-page fetch wrapper (the demo's mock backend
        // intercepts here), and reads 204/404 as "not playing" without
        // an onerror flicker. Cross-origin peer reads are allowed by
        // the peer backend's CORS allowlist (cors_headers_for_origin in
        // api_playback.py) — the operator's flock-membership list is
        // the cross-origin auth boundary.
        //
        // Bug 5 (qarl 2026-05-16): self-origin reads need the bearer
        // token (AuthMiddleware gates /api/playback/current-thumbnail).
        // apiFetch stamps Authorization: Bearer <token> from
        // localStorage, with skipAuth401Redirect so a stale token here
        // shows the placeholder rather than bouncing the operator out
        // of the flock panel to /login.html. Peer reads STILL use plain
        // fetch — our token wouldn't authenticate against a peer's
        // AuthState; the peer-side whitelist (auth_middleware.py's
        // _WHITELIST_EXACT) is what lets cross-flock reads succeed.
        try {
            const r = selfOrigin
                ? await apiFetch(url, { skipAuth401Redirect: true })
                : await fetch(url);
            if (r.status !== 200) throw new Error(`HTTP ${r.status}`);
            const blob = await r.blob();
            const prev = img.dataset.blobUrl;
            const objUrl = URL.createObjectURL(blob);
            img.dataset.blobUrl = objUrl;
            img.src = objUrl;
            if (prev) URL.revokeObjectURL(prev);
        } catch {
            const prev = img.dataset.blobUrl;
            delete img.dataset.blobUrl;
            img.removeAttribute("src");
            img.dispatchEvent(new Event("error"));
            if (prev) URL.revokeObjectURL(prev);
        }
    }

    // Revoke every blob URL currently held on a thumbnail <img> in the
    // grid. Called before render() overwrites gridEl.innerHTML (which
    // would otherwise detach the imgs WITHOUT revoking their blob URLs
    // — the browser keeps the blobs in memory until document unload)
    // and from stop() for the panel-close teardown path. The in-poll
    // revoke at loadThumbViaFetch's `prev` lookup handles steady-state
    // replacements where the img stays attached.
    function revokeAllThumbs() {
        for (const img of gridEl.querySelectorAll("img[data-blob-url]")) {
            URL.revokeObjectURL(img.dataset.blobUrl);
        }
    }

    function refreshThumbnails() {
        const imgs = gridEl.querySelectorAll(".om-peer-thumb-img");
        for (const img of imgs) {
            const card = img.closest(".om-peer-card");
            if (!card) continue;
            if (card.dataset.origin === "self") {
                loadThumbViaFetch(img, thumbnailUrl(null, true), { selfOrigin: true });
            } else if (card.dataset.address && !card.classList.contains("offline")) {
                loadThumbViaFetch(img, thumbnailUrl(card.dataset.address, false), { selfOrigin: false });
            }
        }
    }

    function stopPolling() {
        if (pollTimer !== null) {
            clearInterval(pollTimer);
            pollTimer = null;
        }
    }

    function paintEyebrow(peers) {
        // Total signs = self + all peers; online count includes self
        // (assumed always online since it's serving this panel) plus
        // any peer whose last_seen is within the freshness window.
        const onlinePeers = peers.filter(isPeerOnline).length;
        const total = peers.length + 1;
        const online = onlinePeers + 1;
        let text = `${online} of ${total} sign${total === 1 ? "" : "s"} online`;
        // Most-recent peer last_seen drives the "last sync Xm ago"
        // freshness signal. No peers → omit the second clause; new
        // devices land with last_seen=null and would otherwise read
        // "last sync — ago" which is uglier than nothing.
        let mostRecent = null;
        for (const p of peers) {
            if (!p.last_seen_at) continue;
            const ts = new Date(p.last_seen_at).getTime();
            if (Number.isNaN(ts)) continue;
            if (mostRecent === null || ts > mostRecent) mostRecent = ts;
        }
        if (mostRecent !== null) {
            const rel = relativeMinutes(new Date(mostRecent).toISOString());
            if (rel) text += ` · last sync ${rel}`;
        }
        eyebrowEl.textContent = text;
    }

    async function render() {
        stopPolling();
        let selfName = "This device";
        let selfSyncEnabled = true;
        let selfMode = null;
        let selfModel;
        let selfSignal;
        let selfUptime;
        let peers = [];
        try {
            // Phase B.2: fetch settings + flock + system-info concurrently.
            // System-info failure is non-fatal (older backend, network
            // blip) — selfCardHTML's parameter defaults fall back to
            // SELF_PLACEHOLDER_* constants, mirroring the production
            // backend's _FALLBACK_{MODEL,SIGNAL,UPTIME} sentinels in
            // api_system.py. allSettled lets the panel render even if
            // /api/system/info 404s on a server that pre-dates this
            // commit.
            const fetchInfo = fetchSystemInfo || (() => Promise.reject());
            const [settingsResult, flockResult, infoResult] = await Promise.allSettled([
                fetchSettings(),
                fetchFlock(),
                fetchInfo(),
            ]);
            if (settingsResult.status !== "fulfilled") {
                throw new Error(
                    settingsResult.reason?.message || "settings fetch failed",
                );
            }
            if (flockResult.status !== "fulfilled") {
                throw new Error(
                    flockResult.reason?.message || "flock fetch failed",
                );
            }
            const settings = settingsResult.value;
            const flock = flockResult.value;
            selfName = settings.sign_name || "This device";
            selfSyncEnabled = settings.flock_sync_enabled !== false;
            peers = flock.peers || [];
            // System info populates the self-card's mode + telemetry. The
            // server's /api/system/info already formats mode as a slug
            // matching MODE_LABELS keys (hdmi-1080 / hub75-128x64 /
            // ws281x-strip / etc.), so the prior manual output_mode →
            // slug derivation in this function is gone.
            if (infoResult.status === "fulfilled") {
                const info = infoResult.value;
                selfMode = info.mode || null;
                selfModel = info.model;
                selfSignal = info.signal;
                selfUptime = info.uptime;
            }
        } catch (err) {
            setStatus(`Couldn't load flock: ${err.message}`, { error: true });
            return;
        }

        paintEyebrow(peers);
        revokeAllThumbs();
        gridEl.innerHTML =
            selfCardHTML({
                name: selfName,
                syncEnabled: selfSyncEnabled,
                mode: selfMode,
                model: selfModel,
                signal: selfSignal,
                uptime: selfUptime,
            }) +
            peers.map(peerCardHTML).join("") +
            newDeviceCardHTML();

        for (const img of gridEl.querySelectorAll(".om-peer-thumb-img")) {
            const empty = img.parentElement.querySelector(
                ".om-peer-thumb-empty",
            );
            img.addEventListener("load", () => {
                img.classList.add("is-loaded");
                empty.classList.add("is-hidden");
            });
            img.addEventListener("error", () => {
                img.classList.remove("is-loaded");
                empty.classList.remove("is-hidden");
            });
        }

        if (peers.length === 0) {
            setStatus("No peer devices yet — add one to start syncing media.");
        } else {
            setStatus("");
        }

        refreshThumbnails();
        applyPeerAspects(peers);
        pollTimer = setInterval(refreshThumbnails, THUMBNAIL_POLL_MS);
    }

    // Each peer's display_rotation is fetched live (not persisted on
    // FlockPeer) — qarl's call: "just query the peers when the panel
    // opens. everyone here is online and well connected." Per-peer
    // failure falls back to the local --device-aspect already applied
    // by the .om-peer-thumb CSS rule, so an offline / blocked peer
    // just looks like the local sign instead of crashing the panel.
    function applyPeerAspects(peers) {
        for (const peer of peers) {
            if (!isPeerOnline(peer)) continue;
            const peerIdSel = `.om-peer-card[data-peer-id="${escapeAttr(peer.id)}"]`;
            if (!gridEl.querySelector(peerIdSel)) continue;
            // Re-resolve the thumb element rather than capture it —
            // a re-render between fetch start + resolve replaces
            // the DOM under us, so write to whatever is current.
            const applyAspect = (dims) => {
                const live = gridEl.querySelector(`${peerIdSel} .om-peer-thumb`);
                if (live) {
                    live.style.aspectRatio = `${dims.display_width} / ${dims.display_height}`;
                }
            };
            const cached = peerDimsCache.get(peer.id);
            if (cached) {
                applyAspect(cached);
                continue;
            }
            fetchPeerSystemInfo(peer.address)
                .then((info) => {
                    if (!info || !info.display_width || !info.display_height) {
                        return;
                    }
                    peerDimsCache.set(peer.id, {
                        display_width: info.display_width,
                        display_height: info.display_height,
                    });
                    applyAspect(info);
                })
                .catch(() => {
                    // Fall back to --device-aspect.
                });
        }
    }

    async function refreshDiscoverList() {
        // Phase B.5: populate the discover section with Tailnet
        // candidates from /api/flock/discover. Hidden + early-bail
        // when fetchDiscover isn't injected (test-only); also hidden
        // when the response is empty or source="none" (dev box,
        // tailscale binary missing). Failure of the fetch is silent
        // — modal still works for manual-typed entry, matching the
        // Phase A behavior.
        if (!fetchDiscover) {
            discoverSection.hidden = true;
            return;
        }
        let result;
        try {
            result = await fetchDiscover();
        } catch {
            discoverSection.hidden = true;
            return;
        }
        const candidates = result?.candidates || [];
        if (candidates.length === 0) {
            discoverSection.hidden = true;
            return;
        }
        discoverList.innerHTML = candidates
            .map((c) => {
                const disabled = c.already_in_flock ? " disabled" : "";
                const suffix = c.already_in_flock
                    ? ` <span class="flock-discover-already">(already added)</span>`
                    : "";
                return `
                    <li class="flock-discover-item">
                        <button type="button"
                                class="flock-discover-pick${c.already_in_flock ? " is-disabled" : ""}"
                                data-address="${escapeAttr(c.address)}"${disabled}>
                            <b>${escapeAttr(c.hostname)}</b>
                            <span class="flock-discover-addr">${escapeAttr(c.address)}</span>
                            ${suffix}
                        </button>
                    </li>
                `;
            })
            .join("");
        discoverSection.hidden = false;
    }

    function openAddModal() {
        modalError.textContent = "";
        addressInput.value = "";
        // Pre-clear the discover section so a slow fetch doesn't
        // flash the prior open's stale candidates.
        discoverSection.hidden = true;
        discoverList.innerHTML = "";
        modal.showModal();
        addressInput.focus();
        // Fire-and-forget; the modal renders + works manually even
        // if discover never resolves.
        refreshDiscoverList().catch(() => {
            discoverSection.hidden = true;
        });
    }

    // Click-to-pick: a candidate row populates the address input
    // (operator can still edit before hitting Add). Disabled rows
    // (already_in_flock) are non-interactive via the button's
    // disabled attribute.
    discoverList.addEventListener("click", (event) => {
        const btn = event.target.closest(".flock-discover-pick");
        if (!btn || btn.disabled) return;
        addressInput.value = btn.dataset.address || "";
        addressInput.focus();
    });

    modalCancel.addEventListener("click", () => modal.close());

    modalForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        const address = addressInput.value.trim();
        if (!address) return;
        modalError.textContent = "";
        try {
            await onAdd(address);
            modal.close();
            await render();
            // The backend probes the new peer for sign_name in the
            // background; re-render after a beat so the card flips
            // from "raw address" to the friendly name.
            setTimeout(() => { render().catch(() => {}); }, 1500);
        } catch (err) {
            modalError.textContent = err.message || "Couldn't add device.";
        }
    });

    gridEl.addEventListener("click", async (event) => {
        const newBtn = event.target.closest(".flock-new-device");
        if (newBtn) {
            openAddModal();
            return;
        }
        const editBtn = event.target.closest(".flock-peer-edit");
        if (editBtn) {
            const card = editBtn.closest(".om-peer-card");
            const address = card.dataset.address;
            if (address) {
                // Browser-native navigate to the peer's UI. Back button
                // returns here. Each device renders itself; no in-app
                // context swap, no privileged "home device".
                //
                // Address is interpolated unescaped — backend's
                // FLOCK_ADDRESS_PATTERN (flock.py) restricts it to a
                // hostname / IPv4 + optional :port shape, no schemes
                // or paths, so the resulting URL is well-formed by
                // construction. Adding `encodeURIComponent` would
                // double-encode a value that's already validated.
                window.location.href = `http://${address}/`;
            }
            return;
        }
        const deflockBtn = event.target.closest(".flock-peer-deflock");
        if (deflockBtn) {
            // qarl's domain verb: add-to-flock → "deflock" the inverse.
            // Confirm step is mandatory (re-adding requires re-typing
            // the peer's address, so an accidental tap has real cost).
            // Native confirm() over a custom modal — conventional and
            // hard to mis-tap on mobile.
            const card = deflockBtn.closest(".om-peer-card");
            const peerId = card.dataset.peerId;
            const label = card.querySelector(".om-peer-name")?.textContent;
            // i18n: phase-deferred. Operator-visible strings here +
            // setStatus below will need the eventual i18n pass; for
            // now embed the peer label directly.
            if (!window.confirm(`Remove ${label} from your flock?`)) return;
            try {
                await onDelete(peerId);
                await render();
            } catch (err) {
                setStatus(`Deflock failed: ${err.message}`, { error: true });
            }
            return;
        }
    });

    gridEl.addEventListener("change", async (event) => {
        const selfSync = event.target.closest(".flock-self-sync-input");
        if (selfSync) {
            try {
                await onUpdateSelfSync(selfSync.checked);
                await render();
            } catch (err) {
                setStatus(`Couldn't update sync: ${err.message}`, { error: true });
                await render();
            }
            return;
        }
        const peerSync = event.target.closest(".flock-peer-sync-input");
        if (!peerSync) return;
        const card = peerSync.closest(".om-peer-card");
        const peerId = card.dataset.peerId;
        try {
            await onUpdate(peerId, { sync: peerSync.checked });
            await render();
        } catch (err) {
            setStatus(`Couldn't update sync: ${err.message}`, { error: true });
            await render();
        }
    });

    render();

    return {
        refresh: render,
        stop: () => {
            stopPolling();
            revokeAllThumbs();
            peerDimsCache.clear();
        },
    };
}
