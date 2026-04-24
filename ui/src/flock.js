// Flock panel: manage the set of peer openMarquee devices this one
// knows about, and optionally keeps in media-sync with.
//
// "+ New" takes a Tailscale hostname or bare IPv4 (optionally host:port)
// and POSTs to /api/flock. Each peer tile has a sync toggle, an
// "Open there" link (new tab to that peer's UI), and a × delete.
//
// Listing is passive — the backend's pull worker is what actually
// propagates changes. This panel is just the operator-facing way to
// build the peer list.

const SECTION_TEMPLATE = `
    <section class="flock">
        <h2 class="subpage-title">Flock</h2>
        <p class="flock-hint">
            Peer openMarquee devices. Toggle <strong>Sync</strong> to keep
            media in sync with a peer (push-on-change + periodic pull).
        </p>

        <div class="flock-tiles" role="list"></div>

        <p class="flock-status" role="status" aria-live="polite"></p>
    </section>

    <dialog class="flock-modal">
        <form method="dialog" class="flock-modal-form">
            <h3>Add peer</h3>
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

function peerLabel(peer) {
    return peer.name || peer.address;
}

function peerTileHTML(peer) {
    const synced = peer.sync ? " flock-tile-synced" : "";
    const syncLabel = peer.sync ? "Syncing" : "Not synced";
    const seenNote = peer.last_seen_at
        ? `Last seen ${new Date(peer.last_seen_at).toLocaleString()}`
        : "Never contacted";
    return `
        <div class="flock-tile${synced}" role="listitem" data-peer-id="${peer.id}">
            <div class="flock-tile-body">
                <div class="flock-tile-name">${peerLabel(peer)}</div>
                <div class="flock-tile-address">${peer.address}</div>
                <div class="flock-tile-seen">${seenNote}</div>
            </div>
            <div class="flock-tile-controls">
                <label class="flock-tile-sync">
                    <input type="checkbox" class="flock-tile-sync-input"${peer.sync ? " checked" : ""}>
                    <span>${syncLabel}</span>
                </label>
                <a class="flock-tile-open" href="http://${peer.address}/" target="_blank" rel="noopener">
                    Open there ↗
                </a>
                <button type="button" class="flock-tile-delete" aria-label="Remove peer">×</button>
            </div>
        </div>
    `;
}

function newTileHTML() {
    return `
        <button type="button" class="flock-tile flock-tile-new" aria-label="Add peer">
            <span class="flock-tile-new-plus">+</span>
            <span class="flock-tile-new-label">New peer</span>
        </button>
    `;
}

/**
 * Mount the flock panel.
 *
 * @param {HTMLElement} container
 * @param {object} options
 * @param {() => Promise<{peers: object[]}>} options.fetchFlock
 * @param {(address: string) => Promise<object>} options.onAdd
 * @param {(peerId: string, patch: object) => Promise<object>} options.onUpdate
 * @param {(peerId: string) => Promise<void>} options.onDelete
 */
export function mountFlock(container, { fetchFlock, onAdd, onUpdate, onDelete }) {
    container.innerHTML = SECTION_TEMPLATE;
    const tilesEl = container.querySelector(".flock-tiles");
    const statusEl = container.querySelector(".flock-status");
    const modal = container.querySelector(".flock-modal");
    const modalForm = modal.querySelector(".flock-modal-form");
    const addressInput = modal.querySelector(".flock-address");
    const modalError = modal.querySelector(".flock-modal-error");
    const modalCancel = modal.querySelector(".flock-modal-cancel");

    function setStatus(text, { error = false } = {}) {
        statusEl.textContent = text;
        statusEl.classList.toggle("error", !!error);
    }

    async function render() {
        try {
            const flock = await fetchFlock();
            const peers = flock.peers || [];
            const peersHTML = peers.map(peerTileHTML).join("");
            tilesEl.innerHTML = peersHTML + newTileHTML();
            setStatus(
                peers.length
                    ? `${peers.length} peer${peers.length === 1 ? "" : "s"}`
                    : "No peers yet — add one to start syncing media.",
            );
        } catch (err) {
            setStatus(`Couldn't load flock: ${err.message}`, { error: true });
        }
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
        } catch (err) {
            modalError.textContent = err.message || "Couldn't add peer.";
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
            if (!window.confirm(`Forget peer "${label}"?`)) return;
            try {
                await onDelete(peerId);
                await render();
            } catch (err) {
                setStatus(`Delete failed: ${err.message}`, { error: true });
            }
        }
    });

    tilesEl.addEventListener("change", async (event) => {
        const syncInput = event.target.closest(".flock-tile-sync-input");
        if (!syncInput) return;
        const tile = syncInput.closest(".flock-tile");
        const peerId = tile.dataset.peerId;
        try {
            await onUpdate(peerId, { sync: syncInput.checked });
            await render();
        } catch (err) {
            setStatus(`Couldn't update sync: ${err.message}`, { error: true });
            // Revert the checkbox — re-render will settle the UI.
            await render();
        }
    });

    render();

    return {
        refresh: render,
    };
}
