// Horizontal slide browser mounted at the top of each slide subpage
// (Text / Image / Video). Filters content by type so operators can see
// all their text slides in one place, click to edit, or hit "+ New"
// to start a fresh one. Parallel to the Playlists-page pallet but
// type-scoped — both affordances coexist (pallet drag-to-playlist,
// subpage browser click-to-edit) since each is natural in its
// location.

import { contentTileThumbnailAttrs } from "./api.js";
import { attachAutoTextOverlay } from "./auto-text-overlay.js";

const TEMPLATE = `
    <div class="slide-browser" role="toolbar" aria-label="slide list">
        <ul class="slide-browser-list" role="list"></ul>
        <div class="slide-browser-status" role="status" aria-live="polite" hidden></div>
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
    const statusEl = container.querySelector(".slide-browser-status");

    let highlightedId = null;
    // Bumped on every refresh() so thumbnail URLs force the browser to
    // refetch. Without this, editing a slide in place leaves the tile
    // thumb stale — created_at never changes on an update, so the old
    // `?v=${created_at}` would keep hitting the HTTP cache.
    let refreshVersion = 0;

    async function refresh() {
        let items;
        try {
            items = await fetchItems();
        } catch (err) {
            // Empty-list and fetch-failure used to render identically
            // (silent empty UL). Operator couldn't tell "you have no
            // slides" from "we couldn't reach the server." Now the empty
            // case stays as an empty list, and the failure case surfaces
            // an inline message.
            console.error("[slide-browser] fetchItems failed:", err);
            listEl.innerHTML = "";
            statusEl.hidden = false;
            statusEl.textContent = "Couldn't load slides — check connection.";
            return;
        }
        statusEl.hidden = true;
        statusEl.textContent = "";
        refreshVersion += 1;
        const filtered = items
            .filter((it) => it && it.type === type)
            // Chronological: oldest at top, newest appended at the end
            // (B19, 2026-05-05). Operators read top-to-bottom; new items
            // landing at the bottom matches "I just made this, it's the
            // most recent thing."
            .sort((a, b) =>
                String(a.created_at || "").localeCompare(String(b.created_at || "")),
            );

        listEl.innerHTML = "";
        // The +New affordance is now in the slides shell page-head ("+ New
        // text/image/video"), so we don't render an in-browser tile.
        // `onCreate` stays in the public API for backward compat in case
        // the slides shell wants to invoke it programmatically.
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

    function renderTile(item) {
        const li = document.createElement("li");
        li.className = "slide-browser-tile";
        li.dataset.id = String(item.id);
        if (highlightedId && String(item.id) === highlightedId) {
            li.classList.add("slide-browser-tile--selected");
        }
        const safeName = escapeHtml(item.name || "Untitled");
        // Type badge retired: with the slides shell tab subnav (Text /
        // Image / Video) filtering by type, the per-tile corner badge
        // was redundant chrome.
        // 2026-07-02 (handover-blocker fix): use the small /thumbnail
        // JPEG endpoint via `contentTileThumbnailAttrs` — the raw
        // /asset PNG is 1-3 MB each and ~15 concurrent GETs OOM'd the
        // memory-tight Pi Zero 2 W (qarl). Helper also emits
        // loading="lazy" so off-screen tiles don't fetch, and an
        // onerror that swaps to a static placeholder data-URI
        // (never to /video).
        const thumbAttrs = contentTileThumbnailAttrs(
            item.id,
            item.updated_at || item.created_at || refreshVersion,
        );
        li.innerHTML = `
            <button type="button" class="slide-browser-tile-action" title="${safeName}">
                <span class="slide-browser-tile-thumb-wrap">
                    <img class="slide-browser-tile-thumb" alt="" draggable="false"
                         ${thumbAttrs}>
                </span>
                <span class="slide-browser-tile-name">${safeName}</span>
            </button>
            <button type="button" class="slide-browser-tile-delete"
                    aria-label="Delete ${safeName}" title="Delete ${safeName}">×</button>
        `;
        // Auto-mode text slides get a live-ticking overlay on top of the
        // static asset.png — same pattern as inline-preview's auto-text
        // div. Shared ticker prunes the overlay automatically when this
        // tile is replaced on the next refresh().
        attachAutoTextOverlay(
            li.querySelector(".slide-browser-tile-thumb-wrap"),
            item,
        );
        li.querySelector(".slide-browser-tile-action").addEventListener(
            "click",
            () => {
                highlight(item.id);
                onSelect(item);
            },
        );
        li.querySelector(".slide-browser-tile-delete").addEventListener(
            "click",
            (event) => {
                // Don't also fire the tile-action click (which would open the
                // slide we're about to delete).
                event.stopPropagation();
                document.dispatchEvent(
                    new CustomEvent("openmarquee:delete-slide", {
                        detail: { id: String(item.id), type: item.type, name: item.name },
                    }),
                );
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
