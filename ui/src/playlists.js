// Named-playlists manager.
//
// The saved-slides list handles the default playlist (upload-auto-append +
// drag-reorder). This module handles everything else:
//
//   - create / delete named playlists
//   - drag items between named playlists (shared Sortable group) — moves
//     membership from source to destination in one gesture
//   - drag-to-reorder within a playlist
//   - per-item × removes from that playlist
//   - an "Add item…" dropdown (for items not yet in the list) as a non-drag
//     alternative, so touch users and keyboard users have a path
//
// Every change auto-saves the affected playlist(s) and refreshes from the
// server — no Save button, no stale UI. The default playlist is deliberately
// excluded here; it's edited via Saved slides.

import Sortable from "sortablejs";

const SECTION_TEMPLATE = `
    <section class="playlists">
        <h2 class="playlists-heading">Named playlists</h2>
        <p class="playlists-hint">
            Drag items between playlists to move them. The
            <em>default</em> playlist is managed in the Saved slides section
            above.
        </p>
        <ul class="playlists-list" role="list"></ul>
        <form class="playlists-create" autocomplete="off">
            <label class="field">
                <span>New playlist name</span>
                <input type="text" class="playlists-create-name"
                       placeholder="e.g. lunch" maxlength="64"
                       pattern="[a-z0-9_-]+">
            </label>
            <button type="submit" class="primary playlists-create-btn">Create</button>
        </form>
        <p class="playlists-status" role="status" aria-live="polite"></p>
    </section>
`;

const DEFAULT_PLAYLIST_NAME = "default";
const SORTABLE_GROUP = "openmarquee-playlist-items";

/**
 * Mount the playlists manager into `container`.
 *
 * @param {HTMLElement} container — parent (emptied + replaced).
 * @param {object} options
 * @param {() => Promise<Array>} options.fetchItems — all content items
 * @param {() => Promise<object>} options.fetchPlaylists — { playlists: {...} }
 * @param {(name: string, itemIds: string[]) => Promise<void>} options.onSave
 * @param {(name: string) => Promise<void>} options.onDelete
 * @returns {{ refresh: () => Promise<void> }}
 */
export function mountPlaylistsManager(
    container,
    { fetchItems, fetchPlaylists, onSave, onDelete },
) {
    container.innerHTML = SECTION_TEMPLATE;
    const listEl = container.querySelector(".playlists-list");
    const statusEl = container.querySelector(".playlists-status");
    const createForm = container.querySelector(".playlists-create");
    const createNameEl = container.querySelector(".playlists-create-name");

    // One Sortable per card, keyed by playlist name. Destroy+recreate on
    // refresh so we don't leak listeners across renders.
    const sortables = new Map();
    // Single-flight lock: refresh() swaps DOM wholesale, so a second drag or
    // add/remove landing mid-save would reference detached nodes and clobber
    // state. Drop reentrant calls instead of queueing; the user can just
    // redo the gesture on the refreshed UI.
    let saving = false;

    function destroySortables() {
        for (const s of sortables.values()) s.destroy();
        sortables.clear();
    }

    async function saveAndRefresh(work) {
        if (saving) return;
        saving = true;
        try {
            await work();
            await refresh();
        } catch (err) {
            statusEl.textContent = `Save failed: ${err.message}`;
            // Pull fresh server state so the UI can't diverge on a partial
            // failure (e.g. source playlist saved, dest playlist rejected).
            await refresh();
        } finally {
            saving = false;
        }
    }

    async function refresh() {
        statusEl.textContent = "";
        destroySortables();
        try {
            const [items, collection] = await Promise.all([
                fetchItems(),
                fetchPlaylists(),
            ]);
            const allPlaylists = collection.playlists || {};
            const names = Object.keys(allPlaylists)
                .filter((n) => n !== DEFAULT_PLAYLIST_NAME)
                .sort();

            listEl.innerHTML = "";
            const itemById = new Map(items.map((it) => [String(it.id), it]));

            for (const name of names) {
                const card = renderPlaylistCard(
                    name,
                    allPlaylists[name].item_ids || [],
                    items,
                    itemById,
                );
                listEl.appendChild(card);

                const itemsEl = card.querySelector(".playlist-items");

                // Reading ids back from the live DOM — not from the render-time
                // snapshot — so remove/add can never compose with a concurrent
                // drag to lose an item. `saving` locks out races across
                // handlers; within a single handler we still want the freshest
                // truth.
                card.querySelectorAll(".playlist-item-remove").forEach((btn) => {
                    const itemId = btn.closest(".playlist-item").dataset.id;
                    btn.addEventListener("click", () => {
                        const current = collectIds(itemsEl);
                        const next = current.filter((x) => x !== itemId);
                        saveAndRefresh(() => onSave(name, next));
                    });
                });

                const selectEl = card.querySelector(".playlist-add-select");
                selectEl.addEventListener("change", () => {
                    const id = selectEl.value;
                    if (!id) return;
                    const current = collectIds(itemsEl);
                    if (current.includes(id)) return;
                    saveAndRefresh(() => onSave(name, [...current, id]));
                });

                card.querySelector(".playlist-delete").addEventListener("click", async () => {
                    if (
                        !window.confirm(
                            `Delete playlist "${name}"? Schedule rules pointing at it will play empty.`,
                        )
                    ) {
                        return;
                    }
                    saveAndRefresh(() => onDelete(name));
                });
                const sortable = Sortable.create(itemsEl, {
                    group: SORTABLE_GROUP,
                    animation: 150,
                    ghostClass: "playlist-item-ghost",
                    // Opt drag out of the × button + anywhere else inside the
                    // row that's interactive.
                    filter: "button",
                    preventOnFilter: false,
                    onEnd: async (evt) => {
                        const fromCard = evt.from.closest(".playlist-card");
                        const toCard = evt.to.closest(".playlist-card");
                        if (!fromCard || !toCard) return;
                        const fromName = fromCard.dataset.name;
                        const toName = toCard.dataset.name;

                        // No-op: same list, same slot.
                        if (fromName === toName && evt.oldIndex === evt.newIndex) {
                            return;
                        }

                        if (fromName === toName) {
                            // Reorder within one playlist — save new order only.
                            saveAndRefresh(() => onSave(fromName, collectIds(evt.to)));
                        } else {
                            // Cross-playlist move — save both sides.
                            saveAndRefresh(async () => {
                                await onSave(fromName, collectIds(evt.from));
                                await onSave(toName, collectIds(evt.to));
                            });
                        }
                    },
                });
                sortables.set(name, sortable);
            }

            if (names.length === 0) {
                statusEl.textContent =
                    'No named playlists yet. Create one below (e.g. "lunch") to use schedule rules.';
            }
        } catch (err) {
            statusEl.textContent = `Could not load playlists: ${err.message}`;
        }
    }

    createForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        const name = createNameEl.value.trim();
        if (!name) return;
        if (name === DEFAULT_PLAYLIST_NAME) {
            statusEl.textContent = "Default is reserved — managed in Saved slides.";
            return;
        }
        statusEl.textContent = `Creating ${name}…`;
        try {
            await onSave(name, []);
            createNameEl.value = "";
            statusEl.textContent = "";
            await refresh();
        } catch (err) {
            statusEl.textContent = `Create failed: ${err.message}`;
        }
    });

    refresh();
    return { refresh };
}

// Read the item ids out of a .playlist-items <ul> in DOM order. Sortable has
// already reparented the dragged <li> by the time onEnd fires, so both
// `evt.from` and `evt.to` yield post-move state.
function collectIds(ulEl) {
    return Array.from(ulEl.querySelectorAll(".playlist-item")).map(
        (li) => li.dataset.id,
    );
}

function renderPlaylistCard(name, rawMemberIds, allItems, itemById) {
    const li = document.createElement("li");
    li.className = "playlist-card";
    li.dataset.name = name;

    const memberIds = rawMemberIds.map(String);
    const memberSet = new Set(memberIds);
    const safeName = escapeHtml(name);

    li.innerHTML = `
        <div class="playlist-header">
            <h3 class="playlist-name">${safeName}</h3>
            <span class="playlist-count">${memberIds.length} item${memberIds.length === 1 ? "" : "s"}</span>
            <button type="button" class="danger playlist-delete" aria-label="Delete playlist ${safeName}">Delete</button>
        </div>
        <ul class="playlist-items" role="list" data-empty-hint="Drag items here from another playlist, or use Add item below."></ul>
        <div class="playlist-add">
            <label>
                <span class="playlist-add-label">Add item</span>
                <select class="playlist-add-select">
                    <option value="">Add item…</option>
                </select>
            </label>
        </div>
    `;

    const itemsEl = li.querySelector(".playlist-items");
    for (const id of memberIds) {
        itemsEl.appendChild(renderMemberItem(id, itemById.get(id), name));
    }

    const selectEl = li.querySelector(".playlist-add-select");
    let addable = 0;
    for (const item of allItems) {
        const id = String(item.id);
        if (memberSet.has(id)) continue;
        const opt = document.createElement("option");
        opt.value = id;
        opt.textContent = item.name || "Untitled";
        selectEl.appendChild(opt);
        addable++;
    }
    // Nothing left to add — disable the dropdown so it's not a misleading focus
    // stop for keyboard users.
    if (addable === 0) selectEl.disabled = true;

    return li;
}

function renderMemberItem(id, item, playlistName) {
    const li = document.createElement("li");
    li.className = "playlist-item";
    li.dataset.id = id;
    const itemName = (item && item.name) || "(missing)";
    const label = escapeHtml(itemName);
    const removeLabel = escapeHtml(`Remove ${itemName} from ${playlistName}`);
    li.innerHTML = `
        <span class="playlist-item-handle" aria-hidden="true">⋮⋮</span>
        <span class="playlist-item-label">${label}</span>
        <button type="button" class="playlist-item-remove" aria-label="${removeLabel}">×</button>
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
