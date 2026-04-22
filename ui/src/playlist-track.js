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

const PANEL_OUTPUT_MODES = new Set(["hub75", "ws281x", "composite"]);

/**
 * Map a device output mode to the VideoSlide.pipeline value that's
 * playable on it. Mirror of backend openmarquee.settings.pipeline_for_output_mode
 * so the UI can flag mode-locked slides without a server round-trip.
 */
function pipelineForOutputMode(outputMode) {
    return PANEL_OUTPUT_MODES.has(outputMode) ? "raw_frames" : "h264_mp4";
}

function isModeLockedVideo(item, outputMode) {
    if (!item || item.type !== "video" || !outputMode) return false;
    return item.pipeline !== pipelineForOutputMode(outputMode);
}

const TEMPLATE = `
    <section class="playlist-track">
        <h2 class="subpage-title">Playlists</h2>
        <div class="playlist-browser-slot"></div>
        <div class="playlist-track-header">
            <h3 class="playlist-track-heading" data-field="heading">Default playlist</h3>
        </div>
        <div class="playlist-track-inline-preview"></div>
        <p class="playlist-track-hint">
            Drag blocks to reorder; drag from the pallet below to add;
            × to remove. Click the duration to change it.
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
 * @param {object} [options.inlinePreview] — optional injection:
 *     `{ width, height, outputMode, mount(slot, dims) }`. When set, the
 *     mount is called once right under the header and is expected to
 *     render its own transport controls (play / scrub / time).
 * @param {string} [options.outputMode] — current device output_mode
 *     ("hdmi" / "hub75" / "ws281x" / "composite"). Videos whose stored
 *     pipeline doesn't match the mode get a mode-locked badge on their
 *     pallet + track tiles and won't play until re-uploaded.
 * @returns {{ refresh: () => Promise<void> }}
 */
export function mountPlaylistTrack(container, options) {
    const {
        fetchItems,
        fetchPlaylists,
        onReorder,
        onUpdateDuration,
        inlinePreview,
        outputMode,
        getCurrentPlaylistName,
        playlistBrowser,
    } = options;
    // Fallback when the caller doesn't want multi-playlist — always
    // operate on "default" like the pre-multi UI did.
    const resolveName = getCurrentPlaylistName || (() => "default");

    container.innerHTML = TEMPLATE;
    const trackEl = container.querySelector(".playlist-track-list");
    const palletEl = container.querySelector(".playlist-pallet");
    const statusEl = container.querySelector(".playlist-track-status");
    const headingEl = container.querySelector('[data-field="heading"]');
    const inlinePreviewSlot = container.querySelector(
        ".playlist-track-inline-preview",
    );
    const playlistBrowserSlot = container.querySelector(
        ".playlist-browser-slot",
    );

    if (inlinePreview && inlinePreview.mount) {
        inlinePreview.mount(inlinePreviewSlot, {
            width: inlinePreview.width,
            height: inlinePreview.height,
            outputMode: inlinePreview.outputMode,
        });
    }

    if (playlistBrowser && playlistBrowser.mount) {
        playlistBrowser.mount(playlistBrowserSlot);
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
            const activeName = resolveName();
            headingEl.textContent =
                activeName === "default"
                    ? "Default playlist"
                    : activeName;
            // v3 API returns `items: [{item_id, transition, transition_ms}]`;
            // fall back to the legacy `item_ids` shape for defensive reading.
            const active = collection.playlists?.[activeName];
            const playlistRaw = active?.items;
            const defaultEntries = Array.isArray(playlistRaw)
                ? playlistRaw.map((e) => ({
                      item_id: String(e.item_id),
                      transition: e.transition || "cut",
                      transition_ms: Number(e.transition_ms) || 500,
                  }))
                : (active?.item_ids || []).map((id) => ({
                      item_id: String(id),
                      transition: "cut",
                      transition_ms: 500,
                  }));

            trackEl.innerHTML = "";
            let lockedInTrackCount = 0;
            for (const entry of defaultEntries) {
                const item = itemById.get(entry.item_id);
                if (!item) continue; // stale ref — skip
                const locked = isModeLockedVideo(item, outputMode);
                if (locked) lockedInTrackCount++;
                trackEl.appendChild(renderTrackBlock(item, entry, { locked }));
            }
            // Wire × buttons after the DOM is in place so each handler
            // sees the current trackEl children.
            bindTrackRemoveButtons(trackEl, onReorder, saveAndRefresh);
            bindTrackDurationButtons(
                trackEl, onUpdateDuration, saveAndRefresh,
            );

            palletEl.innerHTML = "";
            for (const item of items) {
                palletEl.appendChild(
                    renderPalletTile(item, {
                        locked: isModeLockedVideo(item, outputMode),
                    }),
                );
            }

            if (lockedInTrackCount > 0 && outputMode) {
                const expected = pipelineForOutputMode(outputMode);
                statusEl.textContent =
                    `⚠ ${lockedInTrackCount} video${lockedInTrackCount === 1 ? "" : "s"} in this playlist ` +
                    `won't play on this device (output mode: ${outputMode}, expects ${expected}). ` +
                    `Re-upload after changing output mode to play them here.`;
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

function bindTrackDurationButtons(trackEl, onUpdateDuration, saveAndRefresh) {
    if (!onUpdateDuration) return;
    for (const btn of trackEl.querySelectorAll(".track-block-duration")) {
        btn.addEventListener("click", () => {
            const block = btn.closest("[data-id]");
            if (!block) return;
            const id = block.dataset.id;
            const currentMs = Number(btn.dataset.durationMs) || 5000;
            const currentSec = Math.round((currentMs / 1000) * 10) / 10;
            // Browser prompt is unglamorous but unambiguous and works
            // on touch + desktop. Upgrade to a popover if it grates.
            const next = window.prompt(
                "Duration in seconds:",
                String(currentSec),
            );
            if (next == null) return;
            const seconds = Number(next);
            if (!Number.isFinite(seconds) || seconds <= 0) return;
            const ms = Math.round(seconds * 1000);
            saveAndRefresh(() => onUpdateDuration(id, ms));
        });
    }
}

function renderTrackBlock(
    item,
    entry = { transition: "cut", transition_ms: 500 },
    { locked = false } = {},
) {
    const li = document.createElement("li");
    li.className = locked ? "track-block track-block--locked" : "track-block";
    li.dataset.id = String(item.id);
    li.dataset.transition = entry.transition;
    li.dataset.transitionMs = String(entry.transition_ms);

    const safeName = escapeHtml(item.name || "Untitled");
    const seconds = (Number(item.duration_ms) || 5000) / 1000;
    const durationLabel = `${
        Number.isInteger(seconds) ? seconds : seconds.toFixed(1)
    }s`;
    const cacheKey = encodeURIComponent(item.created_at || String(item.id));
    const lockedBadge = locked
        ? `<span class="track-block-lock" title="Stored for a different output mode — won't play on this device">⚠</span>`
        : "";
    li.innerHTML = `
        <div class="track-block-thumb-wrap">
            <img class="track-block-thumb" alt=""
                 src="/api/content/${item.id}/asset?v=${cacheKey}">
            ${lockedBadge}
            <button type="button" class="track-remove" aria-label="Remove from playlist" title="Remove from playlist">×</button>
        </div>
        <div class="track-block-name">${safeName}</div>
        <div class="track-block-meta">
            <button type="button" class="track-block-duration"
                    title="Click to change this slide's duration"
                    data-duration-ms="${Number(item.duration_ms) || 5000}">${durationLabel}</button>
            <button type="button" class="track-block-transition"
                    title="Click to cycle transition (cut ↔ fade)">${entry.transition}</button>
        </div>
    `;
    return li;
}

function renderPalletTile(item, { locked = false } = {}) {
    const li = document.createElement("li");
    li.className = locked ? "pallet-tile pallet-tile--locked" : "pallet-tile";
    li.dataset.id = String(item.id);
    li.dataset.type = item.type;
    const safeName = escapeHtml(item.name || "Untitled");
    const typeBadge = escapeHtml(
        item.type === "video" ? "▶" : item.type === "image" ? "🖼" : "Aa",
    );
    const cacheKey = encodeURIComponent(item.created_at || String(item.id));
    const lockedBadge = locked
        ? `<span class="pallet-tile-lock" title="Stored for a different output mode — won't play on this device">⚠</span>`
        : "";
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
        ${lockedBadge}
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
