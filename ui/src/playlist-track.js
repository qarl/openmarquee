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
 * @param {() => Promise<object>} options.fetchPlaylists — { playlists: { default: {items, item_ids}, ... } }
 * @param {(items: Array<{item_id, transition, transition_ms}>) => Promise<any>} options.onReorder
 *     — PUT /api/playlist with the canonical items shape
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
    // Closure-scoped lookup so the track Sortable's `onEnd` can re-skin a
    // cross-list drop (pallet → track) from the Sortable-clone's default
    // `.pallet-tile` shape into a proper `.track-block` *before* waiting
    // on the server round-trip. Refreshed on every refresh() call.
    let itemByIdRef = new Map();

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
            itemByIdRef = new Map(items.map((it) => [String(it.id), it]));
            const itemById = itemByIdRef;
            // v3 API returns `items: [{item_id, transition, transition_ms}]`;
            // fall back to the legacy `item_ids` shape for defensive reading.
            const playlistRaw = collection.playlists?.default?.items;
            const defaultEntries = Array.isArray(playlistRaw)
                ? playlistRaw.map((e) => ({
                      item_id: String(e.item_id),
                      transition: e.transition || "cut",
                      transition_ms: Number(e.transition_ms) || 500,
                  }))
                : (collection.playlists?.default?.item_ids || []).map((id) => ({
                      item_id: String(id),
                      transition: "cut",
                      transition_ms: 500,
                  }));

            trackEl.innerHTML = "";
            for (const entry of defaultEntries) {
                const item = itemById.get(entry.item_id);
                if (!item) continue; // stale ref — skip
                trackEl.appendChild(renderTrackBlock(item, entry));
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
            trackSortable = bindTrackSortable(
                trackEl,
                onReorder,
                saveAndRefresh,
                itemByIdRef,
            );
            palletSortable = bindPalletSortable(palletEl);
        } catch (err) {
            statusEl.textContent = `Could not load playlist: ${err.message}`;
        }
    }

    refresh();
    return { refresh };
}

function bindTrackSortable(trackEl, onReorder, saveAndRefresh, itemByIdRef) {
    return Sortable.create(trackEl, {
        group: { name: "playlist-track", pull: true, put: ["playlist-pallet"] },
        animation: 150,
        ghostClass: "track-ghost",
        filter: ".track-remove, .track-block-transition",
        preventOnFilter: false,
        onAdd: (evt) => {
            // Cross-list drop from the pallet → Sortable cloned a
            // `.pallet-tile` into the track. Re-skin in place to the
            // proper `.track-block` shape (with duration label + default
            // transition chrome) so the operator sees correct chrome
            // *immediately*, not after the save-refresh round-trip.
            const dropped = evt.item;
            const id = dropped?.dataset?.id;
            if (!id) return;
            const item = itemByIdRef.get(id);
            if (!item) return;
            // New entries land with default transitions; operator can
            // click the transition chip to cycle.
            const rebuilt = renderTrackBlock(item, {
                item_id: id,
                transition: "cut",
                transition_ms: 500,
            });
            dropped.replaceWith(rebuilt);
        },
        onEnd: () => {
            const entries = collectTrackEntries(trackEl);
            saveAndRefresh(() => onReorder(entries));
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

// The canonical-shape reader for save calls: returns the full
// `{item_id, transition, transition_ms}` tuple per track block in DOM
// order, so PUT /api/playlist can round-trip transitions too.
function collectTrackEntries(trackEl) {
    return Array.from(trackEl.querySelectorAll(".track-block[data-id]")).map(
        (li) => ({
            item_id: li.dataset.id,
            transition: li.dataset.transition || "cut",
            transition_ms: Number(li.dataset.transitionMs) || 500,
        }),
    );
}

function bindTrackRemoveButtons(trackEl, onReorder, saveAndRefresh) {
    for (const btn of trackEl.querySelectorAll(".track-remove")) {
        btn.addEventListener("click", () => {
            const block = btn.closest("[data-id]");
            if (!block) return;
            const removingId = block.dataset.id;
            const next = collectTrackEntries(trackEl).filter(
                (e) => e.item_id !== removingId,
            );
            saveAndRefresh(() => onReorder(next));
        });
    }
    // Transition chip: cycles cut ↔ fade on click. Uses the block's
    // dataset as the source of truth so collectTrackEntries picks up
    // the new value when we save.
    for (const chip of trackEl.querySelectorAll(".track-block-transition")) {
        chip.addEventListener("click", () => {
            const block = chip.closest(".track-block");
            if (!block) return;
            const current = block.dataset.transition || "cut";
            const next = current === "cut" ? "fade" : "cut";
            block.dataset.transition = next;
            chip.textContent = next;
            const entries = collectTrackEntries(trackEl);
            saveAndRefresh(() => onReorder(entries));
        });
    }
}

function renderTrackBlock(item, entry = { transition: "cut", transition_ms: 500 }) {
    const li = document.createElement("li");
    li.className = "track-block";
    li.dataset.id = String(item.id);
    li.dataset.transition = entry.transition;
    li.dataset.transitionMs = String(entry.transition_ms);

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
        <div class="track-block-meta">
            <span class="track-block-duration">${durationLabel}</span>
            <button type="button" class="track-block-transition"
                    title="Click to cycle transition (cut ↔ fade)">${entry.transition}</button>
        </div>
    `;
    return li;
}

function renderPalletTile(item) {
    const li = document.createElement("li");
    li.className = "pallet-tile";
    li.dataset.id = String(item.id);
    li.dataset.type = item.type;
    const safeName = escapeHtml(item.name || "Untitled");
    const typeBadge = escapeHtml(
        item.type === "video" ? "▶" : item.type === "image" ? "🖼" : "Aa",
    );
    const cacheKey = encodeURIComponent(item.created_at || String(item.id));
    // Every slide type has an "edit" affordance — clicking the ✎ opens
    // the appropriate subpage's editor in edit-existing mode (main.js
    // routes by `item.type`). For image + video, the editor's file
    // picker stays optional — metadata-only updates don't force a
    // re-upload.
    li.innerHTML = `
        <img class="pallet-tile-thumb" alt=""
             src="/api/content/${item.id}/asset?v=${cacheKey}">
        <div class="pallet-tile-name" title="${safeName}">${safeName}</div>
        <div class="pallet-tile-type" aria-hidden="true">${typeBadge}</div>
        <button type="button" class="pallet-tile-edit" title="Edit this slide">✎</button>
    `;
    li.querySelector(".pallet-tile-edit").addEventListener("click", (event) => {
        // Bubble a custom event so main.js (which owns the editors +
        // router) can navigate + pre-fill without playlist-track having
        // to import either.
        event.stopPropagation();
        document.dispatchEvent(
            new CustomEvent("openmarquee:edit-slide", {
                detail: { id: String(item.id), type: item.type },
            }),
        );
    });
    return li;
}

function escapeHtml(value) {
    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}
