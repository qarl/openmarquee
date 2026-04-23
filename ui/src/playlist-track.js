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

// Mode-locking went away when videos became resolution-independent H.264
// MP4s that every renderer can consume. Keep the signature so existing
// callers don't explode; always return false.
function isModeLockedVideo() {
    return false;
}

const TEMPLATE = `
    <section class="playlist-track">
        <h2 class="subpage-title">Playlists</h2>
        <div class="playlist-browser-slot"></div>
        <div class="playlist-track-inline-preview"></div>
        <label class="field">
            <span>Playlist name</span>
            <input type="text" class="field-playlist-name" maxlength="64" pattern="[a-z0-9_-]+">
        </label>
        <p class="playlist-track-hint">
            Drag blocks to reorder; drag from the pallet below to add;
            × to remove. Click the duration to change it.
            Changes apply when you click <strong>Save playlist</strong>.
        </p>

        <div class="playlist-track-scroll" role="region" aria-label="playlist timeline">
            <ul class="playlist-track-list" role="list" data-empty-hint="Drag slides from the pallet below to build your playlist."></ul>
        </div>

        <h3 class="playlist-pallet-heading">All slides</h3>
        <ul class="playlist-pallet" role="list"></ul>

        <button type="button" class="primary playlist-save" disabled>Save playlist</button>
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
        onSavePlaylist,
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
    const nameEl = container.querySelector(".field-playlist-name");
    const saveBtn = container.querySelector(".playlist-save");
    // Heading element is gone from the template (the name input above
    // doubles as the playlist label). Keep the lookup for backward
    // compat: any caller that still injects a `[data-field="heading"]`
    // gets it updated; otherwise we no-op.
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
    let isDirty = false;
    // Bumped on every refresh() so tile thumbnail URLs force a refetch
    // (edits don't change created_at, so the HTTP cache would otherwise
    // serve stale bytes after an in-place save).
    let refreshVersion = 0;
    // Closure-scoped lookup so the track Sortable's `onEnd` can re-skin a
    // cross-list drop (pallet → track) from the Sortable-clone's default
    // `.pallet-tile` shape into a proper `.track-block` *before* waiting
    // on the server round-trip. Refreshed on every refresh() call.
    let itemByIdRef = new Map();

    function markDirty() {
        isDirty = true;
        saveBtn.disabled = false;
        statusEl.textContent = "Unsaved changes.";
    }
    function markClean() {
        isDirty = false;
        saveBtn.disabled = true;
    }

    nameEl.addEventListener("input", markDirty);

    function bindAddedBlockButtons() {
        // After a drag-add or reorder, fresh DOM nodes may not have
        // their click handlers wired yet. Re-bind everything; the
        // initial-render path uses these same fns.
        bindTrackRemoveButtons(trackEl, markDirty);
        bindTrackDurationButtons(trackEl, onUpdateDuration, refresh);
    }

    saveBtn.addEventListener("click", async () => {
        if (saving || !isDirty) return;
        saving = true;
        saveBtn.disabled = true;
        statusEl.textContent = "Saving…";
        try {
            const entries = collectTrackEntries(trackEl);
            const newName = (nameEl.value || "").trim();
            const originalName = resolveName();
            await onSavePlaylist({
                originalName,
                newName: newName || originalName,
                entries,
            });
            statusEl.textContent = "Saved.";
            markClean();
        } catch (err) {
            statusEl.textContent = `Save failed: ${err.message}`;
            saveBtn.disabled = false; // let the operator retry
        } finally {
            saving = false;
        }
    });

    async function refresh() {
        statusEl.textContent = "";
        refreshVersion += 1;
        // Destroy existing Sortables BEFORE re-rendering tiles. Sortable.js
        // `destroy()` strips the `draggable` attribute from every child it
        // managed — doing this AFTER rendering the new tiles would wipe
        // their draggable="false" and let native <img> drag preempt Sortable.
        if (trackSortable) { trackSortable.destroy(); trackSortable = null; }
        if (palletSortable) { palletSortable.destroy(); palletSortable = null; }
        try {
            const [items, collection] = await Promise.all([
                fetchItems(),
                fetchPlaylists(),
            ]);
            itemByIdRef = new Map(items.map((it) => [String(it.id), it]));
            const itemById = itemByIdRef;
            const activeName = resolveName();
            if (headingEl) {
                headingEl.textContent =
                    activeName === "default"
                        ? "Default playlist"
                        : activeName;
            }
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
            for (const entry of defaultEntries) {
                const item = itemById.get(entry.item_id);
                if (!item) continue; // stale ref — skip
                trackEl.appendChild(
                    renderTrackBlock(item, entry, { locked: false, cacheBust: refreshVersion }),
                );
            }
            // Wire × + transition buttons → mark dirty (no save until
            // the operator clicks Save playlist).
            bindTrackRemoveButtons(trackEl, markDirty);
            // Duration is a SLIDE attribute — auto-saves immediately
            // outside the playlist's draft flow.
            bindTrackDurationButtons(
                trackEl, onUpdateDuration, refresh,
            );

            // Sync the name input and reset dirty state to match the
            // freshly-loaded playlist.
            nameEl.value = activeName;
            nameEl.disabled = activeName === "default";
            markClean();
            statusEl.textContent = "";

            palletEl.innerHTML = "";
            for (const item of items) {
                palletEl.appendChild(
                    renderPalletTile(item, { locked: false, cacheBust: refreshVersion }),
                );
            }

            trackSortable = bindTrackSortable(
                trackEl,
                markDirty,
                itemByIdRef,
                bindAddedBlockButtons,
                () => refreshVersion,
            );
            palletSortable = bindPalletSortable(palletEl);
        } catch (err) {
            statusEl.textContent = `Could not load playlist: ${err.message}`;
        }
    }

    refresh();
    return { refresh };
}

function bindTrackSortable(trackEl, markDirty, itemByIdRef, rebindButtons, getCacheBust) {
    return Sortable.create(trackEl, {
        group: { name: "playlist-track", pull: true, put: ["playlist-pallet"] },
        animation: 150,
        ghostClass: "track-ghost",
        filter: ".track-remove, .track-block-transition, .track-block-duration",
        preventOnFilter: false,
        onAdd: (evt) => {
            // Cross-list drop from the pallet → Sortable cloned a
            // `.pallet-tile` into the track. Re-skin in place to the
            // proper `.track-block` shape (with duration label + default
            // transition chrome) so the operator sees correct chrome
            // *immediately*.
            const dropped = evt.item;
            const id = dropped?.dataset?.id;
            if (!id) return;
            const item = itemByIdRef.get(id);
            if (!item) return;
            const rebuilt = renderTrackBlock(
                item,
                { item_id: id, transition: "cut", transition_ms: 500 },
                { cacheBust: getCacheBust ? getCacheBust() : 0 },
            );
            dropped.replaceWith(rebuilt);
        },
        onEnd: () => {
            // Drop happened — re-wire button handlers on the (possibly
            // new) blocks so the operator can interact with the
            // dropped block immediately, then mark the playlist dirty
            // so Save lights up.
            if (rebindButtons) rebindButtons();
            markDirty();
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
        // Clicks on the edit / delete buttons must not initiate a drag —
        // otherwise Sortable intercepts the pointerdown and the button's
        // click handler never fires.
        filter: ".pallet-tile-edit, .pallet-tile-delete",
        preventOnFilter: false,
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

function bindTrackRemoveButtons(trackEl, markDirty) {
    for (const btn of trackEl.querySelectorAll(".track-remove")) {
        // Idempotent re-binding: skip if already wired (we may be
        // called multiple times after drag-adds).
        if (btn.dataset.bound === "1") continue;
        btn.dataset.bound = "1";
        btn.addEventListener("click", () => {
            const block = btn.closest("[data-id]");
            if (!block) return;
            block.remove();
            markDirty();
        });
    }
    // Transition chip: cycles cut ↔ fade on click. Source of truth is
    // the block's dataset so collectTrackEntries picks up the value at
    // Save time.
    for (const chip of trackEl.querySelectorAll(".track-block-transition")) {
        if (chip.dataset.bound === "1") continue;
        chip.dataset.bound = "1";
        chip.addEventListener("click", () => {
            const block = chip.closest(".track-block");
            if (!block) return;
            const current = block.dataset.transition || "cut";
            const next = current === "cut" ? "fade" : "cut";
            block.dataset.transition = next;
            chip.textContent = next;
            markDirty();
        });
    }
}

function bindTrackDurationButtons(trackEl, onUpdateDuration, refresh) {
    if (!onUpdateDuration) return;
    for (const btn of trackEl.querySelectorAll(".track-block-duration")) {
        if (btn.dataset.bound === "1") continue;
        btn.dataset.bound = "1";
        btn.addEventListener("click", async () => {
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
            // Duration is a slide-level attribute — auto-save
            // immediately, outside the playlist's draft flow.
            try {
                await onUpdateDuration(id, ms);
                if (refresh) await refresh();
            } catch (err) {
                console.error("[playlist-track] duration save failed:", err);
            }
        });
    }
}

function renderTrackBlock(
    item,
    entry = { transition: "cut", transition_ms: 500 },
    { locked = false, cacheBust = 0 } = {},
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
    const lockedBadge = locked
        ? `<span class="track-block-lock" title="Stored for a different output mode — won't play on this device">⚠</span>`
        : "";
    li.innerHTML = `
        <div class="track-block-thumb-wrap">
            <img class="track-block-thumb" alt=""
                 src="/api/content/${item.id}/asset?v=${cacheBust}">
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

function renderPalletTile(item, { locked = false, cacheBust = 0 } = {}) {
    const li = document.createElement("li");
    li.className = locked ? "pallet-tile pallet-tile--locked" : "pallet-tile";
    li.dataset.id = String(item.id);
    li.dataset.type = item.type;
    const safeName = escapeHtml(item.name || "Untitled");
    const typeBadge = escapeHtml(
        item.type === "video" ? "▶" : item.type === "image" ? "🖼" : "Aa",
    );
    const lockedBadge = locked
        ? `<span class="pallet-tile-lock" title="Stored for a different output mode — won't play on this device">⚠</span>`
        : "";
    // Every slide type has an "edit" affordance — clicking the ✎ opens
    // the appropriate subpage's editor in edit-existing mode (main.js
    // routes by `item.type`). For image + video, the editor's file
    // picker stays optional — metadata-only updates don't force a
    // re-upload.
    li.innerHTML = `
        <img class="pallet-tile-thumb" alt="" draggable="false"
             src="/api/content/${item.id}/asset?v=${cacheBust}">
        <div class="pallet-tile-name" title="${safeName}">${safeName}</div>
        <div class="pallet-tile-type" aria-hidden="true">${typeBadge}</div>
        ${lockedBadge}
        <button type="button" class="pallet-tile-edit" title="Edit this slide">✎</button>
        <button type="button" class="pallet-tile-delete"
                aria-label="Delete ${safeName}" title="Delete ${safeName}">×</button>
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
    li.querySelector(".pallet-tile-delete").addEventListener("click", (event) => {
        event.stopPropagation();
        document.dispatchEvent(
            new CustomEvent("openmarquee:delete-slide", {
                detail: { id: String(item.id), type: item.type, name: item.name },
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
