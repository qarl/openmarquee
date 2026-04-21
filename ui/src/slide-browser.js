// Horizontal slide browser mounted at the top of each slide subpage
// (Text / Image / Video). Filters content by type so operators can see
// all their text slides in one place, click to edit, or hit "+ New"
// to start a fresh one. Parallel to the Playlists-page pallet but
// type-scoped — both affordances coexist (pallet drag-to-playlist,
// subpage browser click-to-edit) since each is natural in its
// location.

const TEMPLATE = `
    <div class="slide-browser" role="toolbar" aria-label="slide list">
        <ul class="slide-browser-list" role="list"></ul>
    </div>
`;

/**
 * Mount the slide browser.
 *
 * @param {HTMLElement} container — parent (emptied + replaced).
 * @param {object} options
 * @param {"text_slide" | "image" | "video"} options.type — which slides to list.
 * @param {() => Promise<Array>} options.fetchItems — all content items.
 * @param {(item: object) => void} options.onSelect — tile-click handler.
 * @param {() => void} options.onCreate — "+ New" tile-click handler.
 * @returns {{
 *   refresh: () => Promise<void>,
 *   highlight: (id: string | null) => void,
 * }}
 */
export function mountSlideBrowser(container, options) {
    const { type, fetchItems, onSelect, onCreate } = options;
    container.innerHTML = TEMPLATE;
    const listEl = container.querySelector(".slide-browser-list");

    let highlightedId = null;

    async function refresh() {
        let items = [];
        try {
            items = await fetchItems();
        } catch (err) {
            console.error("[slide-browser] fetchItems failed:", err);
            items = [];
        }
        const filtered = items
            .filter((it) => it && it.type === type)
            // Most-recent first so newly-created slides land in view.
            .sort((a, b) =>
                String(b.created_at || "").localeCompare(String(a.created_at || "")),
            );

        listEl.innerHTML = "";
        listEl.appendChild(renderNewTile());
        for (const item of filtered) {
            listEl.appendChild(renderTile(item));
        }
    }

    function highlight(id) {
        highlightedId = id ? String(id) : null;
        for (const tile of listEl.querySelectorAll(".slide-browser-tile")) {
            const match = tile.dataset.id && tile.dataset.id === highlightedId;
            tile.classList.toggle("slide-browser-tile--selected", Boolean(match));
        }
    }

    function renderNewTile() {
        const li = document.createElement("li");
        li.className = "slide-browser-tile slide-browser-tile--new";
        li.innerHTML = `
            <button type="button" class="slide-browser-tile-action" aria-label="Create a new slide">
                <span class="slide-browser-tile-plus" aria-hidden="true">+</span>
                <span class="slide-browser-tile-new-label">New</span>
            </button>
        `;
        li.querySelector(".slide-browser-tile-action").addEventListener(
            "click",
            () => {
                highlight(null);
                onCreate();
            },
        );
        return li;
    }

    function renderTile(item) {
        const li = document.createElement("li");
        li.className = "slide-browser-tile";
        li.dataset.id = String(item.id);
        if (highlightedId && String(item.id) === highlightedId) {
            li.classList.add("slide-browser-tile--selected");
        }
        const safeName = escapeHtml(item.name || "Untitled");
        const cacheKey = encodeURIComponent(item.created_at || String(item.id));
        li.innerHTML = `
            <button type="button" class="slide-browser-tile-action" title="${safeName}">
                <img class="slide-browser-tile-thumb" alt=""
                     src="/api/content/${item.id}/asset?v=${cacheKey}">
                <span class="slide-browser-tile-name">${safeName}</span>
            </button>
        `;
        li.querySelector(".slide-browser-tile-action").addEventListener(
            "click",
            () => {
                highlight(item.id);
                onSelect(item);
            },
        );
        return li;
    }

    refresh();
    return { refresh, highlight };
}

/**
 * Pick the next default name for a new slide of type `typeLabel`.
 * Scans existing names for the "{typeLabel} N" pattern and returns
 * the smallest positive integer N not already used — so deleting
 * #2 and creating a new one recycles the "2" slot rather than
 * jumping to "4".
 *
 * @param {Array<{name: string}>} existingItems
 * @param {string} typeLabel — e.g. "Text Slide".
 * @returns {string} — e.g. "Text Slide 2".
 */
export function nextAutoName(existingItems, typeLabel) {
    const pattern = new RegExp(
        `^${escapeRegex(typeLabel)} (\\d+)$`,
    );
    const used = new Set();
    for (const item of existingItems || []) {
        const m = (item && item.name && item.name.match(pattern)) || null;
        if (m) used.add(Number(m[1]));
    }
    let n = 1;
    while (used.has(n)) n += 1;
    return `${typeLabel} ${n}`;
}

function escapeHtml(value) {
    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

function escapeRegex(value) {
    return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
