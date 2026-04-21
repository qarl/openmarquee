// Named-playlists manager.
//
// The saved-slides list handles the default playlist (upload-auto-append +
// drag-reorder). This module handles everything else: creating new named
// playlists, picking which content items belong in each one, deleting them.
// Schedule rules can then point at these names and the playback engine
// picks the right one at the right time.

const SECTION_TEMPLATE = `
    <section class="playlists">
        <h2 class="playlists-heading">Named playlists</h2>
        <p class="playlists-hint">
            Schedule rules select from these at play time. The
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

    async function refresh() {
        statusEl.textContent = "";
        try {
            const [items, collection] = await Promise.all([
                fetchItems(),
                fetchPlaylists(),
            ]);
            const allPlaylists = collection.playlists || {};
            const names = Object.keys(allPlaylists)
                .filter((n) => n !== DEFAULT_PLAYLIST_NAME)
                .sort();
            renderList(listEl, names, allPlaylists, items, {
                onSave: wrap(onSave, statusEl, "Save"),
                onDelete: async (name) => {
                    await wrap(onDelete, statusEl, "Delete")(name);
                    await refresh();
                },
            });
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

function wrap(fn, statusEl, label) {
    return async (...args) => {
        statusEl.textContent = `${label}…`;
        try {
            await fn(...args);
            statusEl.textContent = "";
        } catch (err) {
            statusEl.textContent = `${label} failed: ${err.message}`;
            throw err;
        }
    };
}

function renderList(listEl, names, allPlaylists, items, { onSave, onDelete }) {
    listEl.innerHTML = "";
    for (const name of names) {
        const playlist = allPlaylists[name] || { item_ids: [] };
        listEl.appendChild(renderPlaylist(name, playlist, items, { onSave, onDelete }));
    }
}

function renderPlaylist(name, playlist, items, { onSave, onDelete }) {
    const li = document.createElement("li");
    li.className = "playlist-card";
    li.dataset.name = name;
    const currentIds = new Set((playlist.item_ids || []).map(String));
    const safeName = escapeHtml(name);

    li.innerHTML = `
        <div class="playlist-header">
            <h3 class="playlist-name">${safeName}</h3>
            <span class="playlist-count">${currentIds.size} item${currentIds.size === 1 ? "" : "s"}</span>
            <button type="button" class="danger playlist-delete" aria-label="Delete playlist ${safeName}">Delete</button>
        </div>
        <ul class="playlist-items" role="list">
            ${items
                .map((item) => {
                    const itemId = String(item.id);
                    const checked = currentIds.has(itemId) ? "checked" : "";
                    const label = escapeHtml(item.name || "Untitled");
                    return `
                        <li class="playlist-item">
                            <label>
                                <input type="checkbox" value="${itemId}" ${checked}>
                                <span>${label}</span>
                            </label>
                        </li>`;
                })
                .join("")}
        </ul>
        <button type="button" class="primary playlist-save">Save ${safeName}</button>
    `;

    li.querySelector(".playlist-delete").addEventListener("click", async () => {
        if (
            !window.confirm(
                `Delete playlist "${name}"? Schedule rules pointing at it will play empty.`,
            )
        ) {
            return;
        }
        await onDelete(name);
    });

    li.querySelector(".playlist-save").addEventListener("click", async () => {
        // Collect item ids in the order they appear on screen (which matches
        // the Saved slides / default-playlist order — predictable for the user).
        const checked = Array.from(
            li.querySelectorAll(".playlist-item input[type='checkbox']:checked"),
        ).map((cb) => cb.value);
        await onSave(name, checked);
        li.querySelector(".playlist-count").textContent = `${checked.length} item${
            checked.length === 1 ? "" : "s"
        }`;
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
