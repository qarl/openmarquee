// Flock panel: the fleet dashboard. One tile per openMarquee device —
// this one first (no controls, just a "you are here" marker) and then
// every device in this device's flock.json. Each tile includes a live
// thumbnail of what that sign is currently playing (polled against
// /api/playback/current-thumbnail on the remote).
//
// "+ New device" at the end opens the address-entry modal. Peer tiles
// have a sync toggle, an "Open there" link, and a × remove. Listing
// is passive — the backend's push + pull workers are what actually
// propagate media.

const THUMBNAIL_POLL_MS = 3000;

const SECTION_TEMPLATE = `
    <section class="flock">
        <h2 class="subpage-title">Flock</h2>
        <p class="flock-hint">
            All openMarquee devices in this flock. Each tile shows what
            that sign is currently playing. Toggle <strong>Sync</strong>
            on a device to keep media mirrored between here and there.
        </p>

        <div class="flock-tiles" role="list"></div>

        <p class="flock-status" role="status" aria-live="polite"></p>
    </section>

    <dialog class="flock-modal">
        <form method="dialog" class="flock-modal-form">
            <h3>Add device</h3>
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

function selfTileHTML(signName, syncEnabled) {
    const name = signName || "This device";
    const syncedClass = syncEnabled ? " flock-tile-synced" : "";
    return `
        <div class="flock-tile flock-tile-self${syncedClass}" role="listitem" data-origin="self">
            <div class="flock-tile-thumb-wrap">
                <img class="flock-tile-thumb" alt="Currently playing" data-origin="self">
                <div class="flock-tile-thumb-empty">Not playing</div>
            </div>
            <div class="flock-tile-body">
                <div class="flock-tile-name">${escapeAttr(name)}</div>
                <div class="flock-tile-address">this device</div>
            </div>
            <div class="flock-tile-controls">
                <label class="flock-tile-sync">
                    <input type="checkbox" class="flock-tile-self-sync-input"${syncEnabled ? " checked" : ""}>
                    <span>Sync</span>
                </label>
            </div>
        </div>
    `;
}

function peerTileHTML(peer) {
    const synced = peer.sync ? " flock-tile-synced" : "";
    const label = peer.name || peer.address;
    const address = peer.address;
    return `
        <div class="flock-tile${synced}" role="listitem"
             data-peer-id="${escapeAttr(peer.id)}"
             data-address="${escapeAttr(address)}">
            <button type="button" class="flock-tile-delete" aria-label="Forget device">×</button>
            <div class="flock-tile-thumb-wrap">
                <img class="flock-tile-thumb" alt="Currently playing on ${escapeAttr(label)}">
                <div class="flock-tile-thumb-empty">Not playing</div>
            </div>
            <div class="flock-tile-body">
                <div class="flock-tile-name">${escapeAttr(label)}</div>
                <div class="flock-tile-address">${escapeAttr(address)}</div>
            </div>
            <div class="flock-tile-controls">
                <label class="flock-tile-sync">
                    <input type="checkbox" class="flock-tile-sync-input"${peer.sync ? " checked" : ""}>
                    <span>Sync</span>
                </label>
                <a class="flock-tile-open" href="http://${escapeAttr(address)}/#/flock" target="_blank" rel="noopener">
                    Go there ↗
                </a>
            </div>
        </div>
    `;
}

function newTileHTML() {
    return `
        <button type="button" class="flock-tile flock-tile-new" aria-label="Add device">
            <span class="flock-tile-new-plus">+</span>
            <span class="flock-tile-new-label">New device</span>
        </button>
    `;
}

function thumbnailUrl(address, selfOrigin) {
    const base = selfOrigin ? "" : `http://${address}`;
    return `${base}/api/playback/current-thumbnail?t=${Date.now()}`;
}

/**
 * Mount the flock panel.
 *
 * @param {HTMLElement} container
 * @param {object} options
 * @param {() => Promise<{peers: object[]}>} options.fetchFlock
 * @param {() => Promise<object>} options.fetchSettings
 * @param {(address: string) => Promise<object>} options.onAdd
 * @param {(peerId: string, patch: object) => Promise<object>} options.onUpdate
 * @param {(peerId: string) => Promise<void>} options.onDelete
 */
export function mountFlock(
    container,
    { fetchFlock, fetchSettings, onAdd, onUpdate, onUpdateSelfSync, onDelete },
) {
    container.innerHTML = SECTION_TEMPLATE;
    const tilesEl = container.querySelector(".flock-tiles");
    const statusEl = container.querySelector(".flock-status");
    const modal = container.querySelector(".flock-modal");
    const modalForm = modal.querySelector(".flock-modal-form");
    const addressInput = modal.querySelector(".flock-address");
    const modalError = modal.querySelector(".flock-modal-error");
    const modalCancel = modal.querySelector(".flock-modal-cancel");

    // Interval handle for the tile-thumbnail poller. Cleared + restarted
    // on every render so torn-down tiles stop firing loads.
    let pollTimer = null;

    function setStatus(text, { error = false } = {}) {
        statusEl.textContent = text;
        statusEl.classList.toggle("error", !!error);
    }

    async function loadThumbViaFetch(img, url) {
        // fetch → blob → createObjectURL, instead of letting the browser
        // fetch the <img src> itself. Two wins: (a) goes through any
        // in-page fetch wrappers (the demo's mock backend intercepts
        // here), and (b) reads 204 / 404 as "not playing" without
        // triggering the noisy onerror path. The remote thumbnail
        // endpoint sets Access-Control-Allow-Origin: * so cross-origin
        // peer fetches still work on a real device.
        try {
            const r = await fetch(url);
            if (r.status !== 200) throw new Error(`HTTP ${r.status}`);
            const blob = await r.blob();
            const prev = img.dataset.blobUrl;
            const objUrl = URL.createObjectURL(blob);
            img.dataset.blobUrl = objUrl;
            img.src = objUrl;
            if (prev) URL.revokeObjectURL(prev);
        } catch {
            // No content → let the "Not playing" overlay stay up.
            const prev = img.dataset.blobUrl;
            delete img.dataset.blobUrl;
            img.removeAttribute("src");
            img.dispatchEvent(new Event("error"));
            if (prev) URL.revokeObjectURL(prev);
        }
    }

    function refreshThumbnails() {
        const imgs = tilesEl.querySelectorAll(".flock-tile-thumb");
        for (const img of imgs) {
            const tile = img.closest(".flock-tile");
            if (!tile) continue;
            const origin = tile.dataset.origin;
            if (origin === "self") {
                loadThumbViaFetch(img, thumbnailUrl(null, true));
            } else if (tile.dataset.address) {
                loadThumbViaFetch(img, thumbnailUrl(tile.dataset.address, false));
            }
        }
    }

    function stopPolling() {
        if (pollTimer !== null) {
            clearInterval(pollTimer);
            pollTimer = null;
        }
    }

    async function render() {
        stopPolling();
        let selfName = "This device";
        let selfSyncEnabled = true;
        let peers = [];
        try {
            const [settings, flock] = await Promise.all([
                fetchSettings(),
                fetchFlock(),
            ]);
            selfName = settings.sign_name || "This device";
            selfSyncEnabled = settings.flock_sync_enabled !== false;
            peers = flock.peers || [];
        } catch (err) {
            setStatus(`Couldn't load flock: ${err.message}`, { error: true });
            return;
        }
        const peersHTML = peers.map(peerTileHTML).join("");
        tilesEl.innerHTML =
            selfTileHTML(selfName, selfSyncEnabled) + peersHTML + newTileHTML();

        // Hide the "Not playing" overlay on successful thumbnail load;
        // show it on error (204 / 404 / network).
        for (const img of tilesEl.querySelectorAll(".flock-tile-thumb")) {
            const empty = img.parentElement.querySelector(
                ".flock-tile-thumb-empty",
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

        const count = peers.length;
        setStatus(
            count
                ? `${count} peer device${count === 1 ? "" : "s"}`
                : "No peer devices yet — add one to start syncing media.",
        );

        refreshThumbnails();
        pollTimer = setInterval(refreshThumbnails, THUMBNAIL_POLL_MS);
    }

    function openAddModal() {
        modalError.textContent = "";
        addressInput.value = "";
        modal.showModal();
        addressInput.focus();
    }

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
            // Backend probes the new device's sign_name in the background;
            // re-render after a beat so the tile flips to the friendlier
            // name.
            setTimeout(() => { render().catch(() => {}); }, 1500);
        } catch (err) {
            modalError.textContent = err.message || "Couldn't add device.";
        }
    });

    tilesEl.addEventListener("click", async (event) => {
        const newBtn = event.target.closest(".flock-tile-new");
        if (newBtn) {
            openAddModal();
            return;
        }
        const delBtn = event.target.closest(".flock-tile-delete");
        if (delBtn) {
            const tile = delBtn.closest(".flock-tile");
            const peerId = tile.dataset.peerId;
            const label = tile.querySelector(".flock-tile-name")?.textContent;
            if (!window.confirm(`Forget device "${label}"?`)) return;
            try {
                await onDelete(peerId);
                await render();
            } catch (err) {
                setStatus(`Delete failed: ${err.message}`, { error: true });
            }
        }
    });

    tilesEl.addEventListener("change", async (event) => {
        const selfSync = event.target.closest(".flock-tile-self-sync-input");
        if (selfSync) {
            try {
                await onUpdateSelfSync(selfSync.checked);
                await render();
            } catch (err) {
                setStatus(
                    `Couldn't update sync: ${err.message}`,
                    { error: true },
                );
                await render();
            }
            return;
        }
        const syncInput = event.target.closest(".flock-tile-sync-input");
        if (!syncInput) return;
        const tile = syncInput.closest(".flock-tile");
        const peerId = tile.dataset.peerId;
        try {
            await onUpdate(peerId, { sync: syncInput.checked });
            await render();
        } catch (err) {
            setStatus(`Couldn't update sync: ${err.message}`, { error: true });
            await render();
        }
    });

    render();

    return {
        refresh: render,
        stop: stopPolling,
    };
}
