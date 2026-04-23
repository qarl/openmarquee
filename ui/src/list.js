// Saved-slides list: thumbnails + per-item Play and Delete + drag-to-reorder.
// Reads from the /api/content endpoint on mount and on demand via the returned
// `refresh` function so the editor can ping us after a save. When the user
// reorders via drag, we extract the new order from the DOM and PUT it to
// /api/playlist.

import Sortable from "sortablejs";

const LIST_TEMPLATE = `
    <section class="list">
        <h2 class="list-heading">Saved slides</h2>
        <p class="list-hint">Drag to reorder. Order drives playback.</p>
        <ul class="slides" role="list"></ul>
        <p class="list-status" role="status" aria-live="polite"></p>
    </section>
`;

const EMPTY_COPY = "No slides yet. Type something above and hit Save.";

/**
 * Mount the saved-slides list into `container`.
 *
 * @param {HTMLElement} container — parent element (emptied and replaced).
 * @param {object} options
 * @param {() => Promise<Array>} options.fetchItems — returns the list.
 * @param {(id: string) => Promise<void>} options.onPlay — invoked when Play is clicked.
 * @param {(id: string) => Promise<void>} options.onDelete — invoked when Delete is clicked.
 * @param {(itemIds: string[]) => Promise<void>} options.onReorder — invoked with
 *     the new id order after a drag that actually changed position. Tests that
 *     don't care can pass `vi.fn()` / `() => Promise.resolve()`.
 * @returns {{ refresh: () => Promise<void> }} — caller can trigger a reload.
 */
export function mountList(container, { fetchItems, onPlay, onDelete, onReorder }) {
    container.innerHTML = LIST_TEMPLATE;
    const listEl = container.querySelector(".slides");
    const statusEl = container.querySelector(".list-status");
    let sortable = null;
    // Message to surface after the next refresh completes — lets us show an
    // error that would otherwise be clobbered by the auto-refresh.
    let pendingStatus = "";

    async function refresh() {
        statusEl.textContent = pendingStatus;
        pendingStatus = "";
        // Destroy the previous Sortable BEFORE re-rendering. Sortable.destroy
        // strips `draggable` off every child it managed; doing this after
        // renderItems would wipe any draggable="false" intent on fresh DOM.
        // No problem today (renderItems doesn't set draggable on thumbs)
        // but re-ordering to match the playlist-track refresh pattern so a
        // future "add native-drag suppression to thumbs" edit doesn't
        // silently lose its effect.
        if (sortable) { sortable.destroy(); sortable = null; }
        try {
            const items = await fetchItems();
            renderItems(listEl, items, {
                onPlay: wrap(onPlay, statusEl, "Play"),
                onDelete: async (id) => {
                    await wrap(onDelete, statusEl, "Delete")(id);
                    await refresh();
                },
            });
            if (items.length === 0) {
                // Don't overwrite a pending error (refresh-on-error case).
                if (!statusEl.textContent) statusEl.textContent = EMPTY_COPY;
            }

            // Rebind drag-reorder on every render; the <ul> contents just got
            // replaced wholesale.
            sortable = items.length > 1 ? bindSortable(listEl, onReorder, statusEl, refresh) : null;
        } catch (err) {
            statusEl.textContent = `Could not load slides: ${err.message}`;
        }
    }

    refresh();
    return { refresh };
}

function bindSortable(listEl, onReorder, statusEl, refresh) {
    return Sortable.create(listEl, {
        animation: 150,
        // Whole row is draggable, but buttons + interactive controls opt out
        // via `filter` so taps on Play/Delete don't start a drag.
        filter: "button, input, textarea",
        preventOnFilter: false,
        ghostClass: "slide-drag-ghost",
        onEnd: async (evt) => {
            // No-op drops (drag and return to same slot) shouldn't PUT.
            if (evt.oldIndex === evt.newIndex) return;
            const ids = Array.from(listEl.querySelectorAll(".slide")).map(
                (li) => li.dataset.id,
            );
            try {
                await onReorder(ids);
            } catch (err) {
                // Stash the message so the refresh() below doesn't clobber it.
                const msg = `Reorder failed: ${err.message}`;
                await (async () => {
                    // Arrow IIFE just to keep this readable — refresh() resets
                    // statusEl from pendingStatus, which we set here first.
                })();
                // eslint-disable-next-line no-param-reassign
                statusEl.textContent = msg;
                // Revert to server truth, but preserve the error:
                await refreshPreservingStatus(refresh, statusEl);
            }
        },
    });
}

async function refreshPreservingStatus(refresh, statusEl) {
    const saved = statusEl.textContent;
    await refresh();
    // refresh() emptied the status; restore the error if there was one.
    if (saved) statusEl.textContent = saved;
}

function wrap(fn, statusEl, label) {
    return async (id) => {
        statusEl.textContent = `${label}…`;
        try {
            await fn(id);
            statusEl.textContent = ""; // Quiet success — only errors stick around.
        } catch (err) {
            statusEl.textContent = `${label} failed: ${err.message}`;
            throw err;
        }
    };
}

function renderItems(listEl, items, { onPlay, onDelete }) {
    listEl.innerHTML = "";
    for (const item of items) {
        listEl.appendChild(renderItem(item, { onPlay, onDelete }));
    }
}

function renderItem(item, { onPlay, onDelete }) {
    const li = document.createElement("li");
    li.className = "slide";
    li.dataset.id = String(item.id);

    const thumb = document.createElement("img");
    thumb.className = "slide-thumb";
    thumb.alt = "";
    // Per-slide cache key: created_at (or id, if not present) means the browser
    // can actually cache thumbnails between renders. A future update endpoint
    // would bump created_at (or an updated_at field) so re-saved slides get a
    // fresh fetch without invalidating everyone else.
    const cacheKey = encodeURIComponent(item.created_at || String(item.id));
    thumb.src = `/api/content/${item.id}/asset?v=${cacheKey}`;

    const meta = document.createElement("div");
    meta.className = "slide-meta";
    const name = document.createElement("div");
    name.className = "slide-name";
    name.textContent = item.name || "Untitled";
    const text = document.createElement("div");
    text.className = "slide-text";
    text.textContent = item.text || "";
    meta.append(name, text);

    const actions = document.createElement("div");
    actions.className = "slide-actions";

    const playBtn = document.createElement("button");
    playBtn.type = "button";
    playBtn.textContent = "Play";
    playBtn.addEventListener("click", () => onPlay(item.id));

    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.className = "danger";
    deleteBtn.textContent = "Delete";
    deleteBtn.addEventListener("click", () => onDelete(item.id));

    actions.append(playBtn, deleteBtn);
    li.append(thumb, meta, actions);
    return li;
}
