// Saved-slides list: thumbnails + per-item Play and Delete. Reads from the
// /api/content endpoint on mount and on demand via the returned `refresh`
// function so the editor can ping us after a save.

const LIST_TEMPLATE = `
    <section class="list">
        <h2 class="list-heading">Saved slides</h2>
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
 * @returns {{ refresh: () => Promise<void> }} — caller can trigger a reload.
 */
export function mountList(container, { fetchItems, onPlay, onDelete }) {
    container.innerHTML = LIST_TEMPLATE;
    const listEl = container.querySelector(".slides");
    const statusEl = container.querySelector(".list-status");

    async function refresh() {
        statusEl.textContent = "";
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
                statusEl.textContent = EMPTY_COPY;
            }
        } catch (err) {
            statusEl.textContent = `Could not load slides: ${err.message}`;
        }
    }

    refresh();
    return { refresh };
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
