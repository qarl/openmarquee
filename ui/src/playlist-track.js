// Playlist page: horizontal timeline of the default playlist + a pallet
// of every saved slide at the bottom. Drag within the timeline to reorder;
// drag from the pallet onto the timeline to add; click × on a timeline
// block to remove.
//
// qarl's spec: single default playlist (named playlists are hidden for
// now), slide duration shown on the bottom of each track block, pallet
// = all available content.
//
// Backend still supports the multi-playlist collection (schedule rules
// ref by name) — this UI just pins to the "default" playlist. Named
// playlists can come back via a later commit if the schedule UX needs
// them visible.

import Sortable from "sortablejs";

const TEMPLATE = `
    <section class="playlist-track">
        <div class="playlist-track-header">
            <h2 class="playlist-track-heading">Default playlist</h2>
            <div class="playlist-track-playback"></div>
        </div>
        <p class="playlist-track-hint">
            Drag blocks to reorder; drag from the pallet below to add;
            × to remove. Duration shown under each block.
        </p>

        <div class="playlist-track-scroll" role="region" aria-label="playlist timeline">
            <ul class="playlist-track-list" role="list" data-empty-hint="Drag slides from the pallet below to build your playlist."></ul>
        </div>

        <h3 class="playlist-pallet-heading">All slides</h3>
        <ul class="playlist-pallet" role="list"></ul>

        <p class="playlist-track-status" role="status" aria-live="polite"></p>
    </section>
`;

/**
 * Mount the single-playlist track editor into `container`.
 *
 * @param {HTMLElement} container
 * @param {object} options
 * @param {() => Promise<Array>} options.fetchItems — all content items
 * @param {() => Promise<object>} options.fetchPlaylists — { playlists: { default: {item_ids}, ... } }
 * @param {(itemIds: string[]) => Promise<any>} options.onReorder — PUT /api/playlist
 * @param {object} [options.playback] — optional hooks forwarded to
 *     mountPlaybackControls (fetchState, onStart, onStop). When present
 *     the track header renders the shared Play / Stop controls.
 * @param {(container, options) => any} [options.mountPlaybackControls]
 *     — injected playback-controls mount (avoids a direct import so the
 *     module stays leaf-y in the dependency graph for tests).
 * @returns {{ refresh: () => Promise<void> }}
 */
export function mountPlaylistTrack(container, options) {
    const {
        fetchItems,
        fetchPlaylists,
        onReorder,
        playback,
        mountPlaybackControls,
    } = options;

    container.innerHTML = TEMPLATE;
    const trackEl = container.querySelector(".playlist-track-list");
    const palletEl = container.querySelector(".playlist-pallet");
    const statusEl = container.querySelector(".playlist-track-status");
    const playbackSlot = container.querySelector(".playlist-track-playback");

    if (playback && mountPlaybackControls) {
        mountPlaybackControls(playbackSlot, playback);
    }

    let trackSortable = null;
    let palletSortable = null;
    let saving = false;

    async function saveAndRefresh(work) {
        if (saving) return;
        saving = true;
        try {
            await work();
            await refresh();
        } catch (err) {
            statusEl.textContent = `Save failed: ${err.message}`;
            await refresh();
        } finally {
            saving = false;
        }
    }

    async function refresh() {
        statusEl.textContent = "";
        try {
            const [items, collection] = await Promise.all([
                fetchItems(),
                fetchPlaylists(),
            ]);
            const itemById = new Map(items.map((it) => [String(it.id), it]));
            const defaultIds = (
                collection.playlists?.default?.item_ids || []
            ).map(String);

            trackEl.innerHTML = "";
            for (const id of defaultIds) {
                const item = itemById.get(id);
                if (!item) continue; // stale ref — skip
                trackEl.appendChild(renderTrackBlock(item));
            }
            // Wire × buttons after the DOM is in place so each handler
            // sees the current trackEl children.
            bindTrackRemoveButtons(trackEl, onReorder, saveAndRefresh);

            palletEl.innerHTML = "";
            for (const item of items) {
                palletEl.appendChild(renderPalletTile(item));
            }

            if (trackSortable) trackSortable.destroy();
            if (palletSortable) palletSortable.destroy();
            trackSortable = bindTrackSortable(trackEl, onReorder, saveAndRefresh);
            palletSortable = bindPalletSortable(palletEl);
        } catch (err) {
            statusEl.textContent = `Could not load playlist: ${err.message}`;
        }
    }

    refresh();
    return { refresh };
}

function bindTrackSortable(trackEl, onReorder, saveAndRefresh) {
    return Sortable.create(trackEl, {
        group: { name: "playlist-track", pull: true, put: ["playlist-pallet"] },
        animation: 150,
        ghostClass: "track-ghost",
        filter: ".track-remove",
        preventOnFilter: false,
        onEnd: () => {
            const ids = collectTrackIds(trackEl);
            saveAndRefresh(() => onReorder(ids));
        },
    });
}

function bindPalletSortable(palletEl) {
    return Sortable.create(palletEl, {
        // Dragging out of the pallet creates a clone (pallet tile stays put)
        // so the same slide can be added to the track multiple times.
        group: {
            name: "playlist-pallet",
            pull: "clone",
            put: false,
        },
        sort: false,
        ghostClass: "pallet-ghost",
    });
}

function collectTrackIds(trackEl) {
    return Array.from(trackEl.querySelectorAll("[data-id]")).map(
        (el) => el.dataset.id,
    );
}

function bindTrackRemoveButtons(trackEl, onReorder, saveAndRefresh) {
    for (const btn of trackEl.querySelectorAll(".track-remove")) {
        btn.addEventListener("click", () => {
            const block = btn.closest("[data-id]");
            if (!block) return;
            const removingId = block.dataset.id;
            const next = collectTrackIds(trackEl).filter((id) => id !== removingId);
            saveAndRefresh(() => onReorder(next));
        });
    }
}

function renderTrackBlock(item) {
    const li = document.createElement("li");
    li.className = "track-block";
    li.dataset.id = String(item.id);
    const safeName = escapeHtml(item.name || "Untitled");
    const seconds = (Number(item.duration_ms) || 5000) / 1000;
    const durationLabel = `${
        Number.isInteger(seconds) ? seconds : seconds.toFixed(1)
    }s`;
    const cacheKey = encodeURIComponent(item.created_at || String(item.id));
    li.innerHTML = `
        <div class="track-block-thumb-wrap">
            <img class="track-block-thumb" alt=""
                 src="/api/content/${item.id}/asset?v=${cacheKey}">
            <button type="button" class="track-remove" aria-label="Remove from playlist" title="Remove from playlist">×</button>
        </div>
        <div class="track-block-name">${safeName}</div>
        <div class="track-block-duration">${durationLabel}</div>
    `;
    return li;
}

function renderPalletTile(item) {
    const li = document.createElement("li");
    li.className = "pallet-tile";
    li.dataset.id = String(item.id);
    const safeName = escapeHtml(item.name || "Untitled");
    const typeBadge = escapeHtml(
        item.type === "video" ? "▶" : item.type === "image" ? "🖼" : "Aa",
    );
    const cacheKey = encodeURIComponent(item.created_at || String(item.id));
    li.innerHTML = `
        <img class="pallet-tile-thumb" alt=""
             src="/api/content/${item.id}/asset?v=${cacheKey}">
        <div class="pallet-tile-name" title="${safeName}">${safeName}</div>
        <div class="pallet-tile-type" aria-hidden="true">${typeBadge}</div>
    `;
    return li;
}

function escapeHtml(value) {
    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}
