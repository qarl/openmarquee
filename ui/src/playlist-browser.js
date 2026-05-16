// Multi-playlist browser — horizontal tile strip at the top of the
// Playlists subpage. Renders one tile per existing playlist; selecting
// a tile switches both the track editor + the inline preview to that
// playlist via the parent's onSelect callback.
//
// Identity: tiles are keyed by stable UUID (data-id), with the display
// name shown as the label. Renames don't shift identity, so highlight
// + selection don't break across an edit.

import { mediaSrc } from "./api.js";
import { DEFAULT_PLAYLIST_ID } from "./constants.js";

const TEMPLATE = `
    <div class="playlist-browser" role="toolbar" aria-label="playlists">
        <ul class="playlist-browser-list" role="list"></ul>
    </div>
`;

/**
 * Mount the multi-playlist browser.
 *
 * @param {HTMLElement} container — parent (emptied + replaced).
 * @param {() => Promise<{playlists: Array}>} options.fetchPlaylists
 *   — yields the v4 collection: `playlists` is a list of {id, name, items}.
 * @param {() => Promise<Array>} [options.fetchItems] — when provided,
 *   each tile shows a thumbnail of the playlist's first slide.
 * @param {(playlistId: string) => void} options.onSelect — tile click.
 * @param {(playlistId: string) => void} options.onCreate — page-head
 *   "+ New" hook (no in-browser tile uses this anymore).
 * @param {(playlistId: string, displayName: string) => void} [options.onDelete]
 *   — × button click. Receives both id (the action key) and display
 *   name (for the confirm prompt).
 * @returns {{
 *   refresh: () => Promise<void>,
 *   highlight: (playlistId: string | null) => void,
 * }}
 */
export function mountPlaylistBrowser(container, options) {
    const { fetchPlaylists, fetchItems, onSelect, onCreate, onDelete } = options;
    container.innerHTML = TEMPLATE;
    const listEl = container.querySelector(".playlist-browser-list");

    let highlightedId = null;

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
            collection = { playlists: [] };
        }
        const itemById = new Map(items.map((it) => [String(it.id), it]));
        // Sort: default first (by id, not name -- rename-safe), then
        // preserve insertion order so newly-created playlists appear at
        // the end of the list (B19, 2026-05-05). The collection array
        // is in creation order on the backend (PlaylistCollection
        // appends new playlists), so a stable sort that only pulls
        // the default to the front leaves the rest in place.
        const playlists = [...(collection.playlists || [])].sort((a, b) => {
            if (String(a.id) === DEFAULT_PLAYLIST_ID) return -1;
            if (String(b.id) === DEFAULT_PLAYLIST_ID) return 1;
            return 0;
        });
        listEl.innerHTML = "";
        for (const playlist of playlists) {
            const playlistItems = playlist.items || [];
            const firstId = playlistItems[0]?.item_id || null;
            const firstItem = firstId ? itemById.get(String(firstId)) : null;
            listEl.appendChild(
                renderTile(
                    String(playlist.id),
                    playlist.name || "",
                    playlistItems.length,
                    firstItem,
                ),
            );
        }
    }

    function highlight(playlistId) {
        highlightedId = playlistId ? String(playlistId) : null;
        for (const tile of listEl.querySelectorAll(".playlist-browser-tile")) {
            const match = tile.dataset.id && tile.dataset.id === highlightedId;
            tile.classList.toggle(
                "playlist-browser-tile--selected",
                Boolean(match),
            );
        }
    }

    function renderTile(playlistId, displayName, itemCount, firstItem) {
        const li = document.createElement("li");
        li.className = "playlist-browser-tile";
        li.dataset.id = playlistId;
        if (highlightedId && playlistId === highlightedId) {
            li.classList.add("playlist-browser-tile--selected");
        }
        const safeName = escapeHtml(displayName || "(unnamed)");
        const itemsLabel = itemCount === 1 ? "1 slide" : `${itemCount} slides`;
        const thumb = firstItem
            ? `<img class="playlist-browser-tile-thumb" alt="" src="${mediaSrc(`/api/content/${firstItem.id}/asset?v=${encodeURIComponent(firstItem.updated_at || firstItem.created_at || firstItem.id)}`)}">`
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
                highlight(playlistId);
                onSelect(playlistId);
            },
        );
        li.querySelector(".playlist-browser-tile-delete").addEventListener(
            "click",
            (event) => {
                // Prevent the tile-action click from firing (which would
                // open the playlist we're about to delete).
                event.stopPropagation();
                if (onDelete) onDelete(playlistId, displayName);
            },
        );
        return li;
    }

    refresh();
    return { refresh, highlight };
}

/**
 * Pick the next default display name "playlist-N" that fills gaps in
 * the existing series. Display names are free-form; this helper just
 * produces a regex-friendly default. Tolerates legacy "Playlist N"
 * (caps + space) names from before the UUID refactor so the series
 * stays continuous.
 */
export function nextPlaylistName(existingNames) {
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
