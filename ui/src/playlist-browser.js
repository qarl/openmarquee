// Multi-playlist browser — horizontal tile strip at the top of the
// Playlists subpage. Mirrors the slide-browser pattern but for
// PlaylistCollection entries: leading "+ New" tile creates a new
// named playlist, then one tile per existing playlist.
//
// Selecting a tile switches both the track editor + the inline
// preview to that playlist via the parent's onSelect callback.

const TEMPLATE = `
    <div class="playlist-browser" role="toolbar" aria-label="playlists">
        <ul class="playlist-browser-list" role="list"></ul>
    </div>
`;

/**
 * Mount the multi-playlist browser.
 *
 * @param {HTMLElement} container — parent (emptied + replaced).
 * @param {object} options
 * @param {() => Promise<{playlists: object}>} options.fetchPlaylists
 * @param {() => Promise<Array>} [options.fetchItems] — when provided,
 *     each tile shows a thumbnail of the playlist's first slide.
 * @param {(name: string) => void} options.onSelect — tile click.
 * @param {() => void} options.onCreate — "+ New" tile click.
 * @returns {{
 *   refresh: () => Promise<void>,
 *   highlight: (name: string | null) => void,
 * }}
 */
export function mountPlaylistBrowser(container, options) {
    const { fetchPlaylists, fetchItems, onSelect, onCreate, onDelete } = options;
    container.innerHTML = TEMPLATE;
    const listEl = container.querySelector(".playlist-browser-list");

    let highlightedName = null;

    async function refresh() {
        let collection;
        let items = [];
        try {
            const [c, i] = await Promise.all([
                fetchPlaylists(),
                fetchItems ? fetchItems() : Promise.resolve([]),
            ]);
            collection = c;
            items = i || [];
        } catch (err) {
            console.error("[playlist-browser] fetch failed:", err);
            collection = { playlists: {} };
        }
        const itemById = new Map(items.map((it) => [String(it.id), it]));
        const names = Object.keys(collection.playlists || {}).sort((a, b) => {
            // "default" first, others alphabetical — matches operator
            // expectation that the always-there playlist is the anchor.
            if (a === "default") return -1;
            if (b === "default") return 1;
            return a.localeCompare(b);
        });
        listEl.innerHTML = "";
        // The +New affordance is now in the playlist page-head ("+ New
        // playlist"); the in-browser tile is gone. `onCreate` stays in
        // the public API for the page-head button to invoke.
        for (const name of names) {
            const playlist = collection.playlists[name];
            const playlistItems = playlist?.items || [];
            const firstId = playlistItems[0]?.item_id || null;
            const firstItem = firstId ? itemById.get(String(firstId)) : null;
            listEl.appendChild(
                renderTile(name, playlistItems.length, firstItem),
            );
        }
    }

    function highlight(name) {
        highlightedName = name || null;
        for (const tile of listEl.querySelectorAll(".playlist-browser-tile")) {
            const match =
                tile.dataset.name && tile.dataset.name === highlightedName;
            tile.classList.toggle(
                "playlist-browser-tile--selected",
                Boolean(match),
            );
        }
    }

    function renderTile(name, itemCount, firstItem) {
        const li = document.createElement("li");
        li.className = "playlist-browser-tile";
        li.dataset.name = name;
        if (highlightedName && name === highlightedName) {
            li.classList.add("playlist-browser-tile--selected");
        }
        const safeName = escapeHtml(name);
        const itemsLabel = itemCount === 1 ? "1 slide" : `${itemCount} slides`;
        const thumb = firstItem
            ? `<img class="playlist-browser-tile-thumb" alt="" src="/api/content/${firstItem.id}/asset?v=${encodeURIComponent(firstItem.created_at || firstItem.id)}">`
            : `<div class="playlist-browser-tile-thumb playlist-browser-tile-thumb--empty"></div>`;
        li.innerHTML = `
            <button type="button" class="playlist-browser-tile-action"
                    title="${safeName}">
                ${thumb}
                <span class="playlist-browser-tile-name">${safeName}</span>
                <span class="playlist-browser-tile-meta">${itemsLabel}</span>
            </button>
            <button type="button" class="slide-browser-tile-delete playlist-browser-tile-delete"
                    aria-label="Delete ${safeName}" title="Delete ${safeName}">×</button>
        `;
        li.querySelector(".playlist-browser-tile-action").addEventListener(
            "click",
            () => {
                highlight(name);
                onSelect(name);
            },
        );
        li.querySelector(".playlist-browser-tile-delete").addEventListener(
            "click",
            (event) => {
                // Prevent the tile-action click from firing (which would
                // open the playlist we're about to delete).
                event.stopPropagation();
                if (onDelete) onDelete(name);
            },
        );
        return li;
    }

    refresh();
    return { refresh, highlight };
}

/**
 * Pick the next default name "Playlist N" that fills gaps in the
 * existing series — matches the slide-browser nextAutoName behavior
 * so deleting Playlist 2 + creating a new one recycles "2".
 */
export function nextPlaylistName(existingNames) {
    // Names MUST satisfy `^[a-z0-9_-]{1,64}$` because the schedule
    // model enforces that pattern on `playlist_name` references — a
    // name with caps or a space (e.g. "Playlist 1") will save into the
    // playlist collection but will reject when referenced from a rule.
    // Match the legacy "Playlist N" form too so existing series get
    // continued without gaps.
    const compliant = /^playlist-(\d+)$/;
    const legacy = /^Playlist (\d+)$/;
    const used = new Set();
    for (const name of existingNames || []) {
        const m = name.match(compliant) || name.match(legacy);
        if (m) used.add(Number(m[1]));
    }
    let n = 1;
    while (used.has(n)) n += 1;
    return `playlist-${n}`;
}

function escapeHtml(value) {
    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}
