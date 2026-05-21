// Slides shell — the unified Library page.
//
// Hosts four child panels (text editor, image upload, video upload,
// stream) under one sidebar nav entry. Renders the page head + a 4-tab
// subnav strip; tab clicks (and deep-link URLs `#/slides/text`,
// `#/slides/image`, `#/slides/video`, `#/slides/stream`) toggle which
// child pane is visible.
//
// Counts in the subnav strip are populated from /api/content via
// `fetchItems`; refresh by calling `handle.refreshCounts()` after a
// save / delete elsewhere in the app.

const TABS = [
    { key: "text",  label: "Text",  type: "text_slide", noun: "text"  },
    { key: "image", label: "Image", type: "image",      noun: "image" },
    { key: "video", label: "Video", type: "video",      noun: "video" },
    { key: "stream", label: "Stream", type: "stream", noun: "Stream" },
];

const TEMPLATE = `
    <section class="slides-shell">
        <div class="om-page-head">
            <div>
                <span class="om-eyebrow">Library · 4 sources</span>
                <h1>Slides</h1>
                <p>Build text, drop in images, loop a video, or pull a live stream. Each slide previews at your sign's resolution.</p>
            </div>
            <div class="slides-shell-actions">
                <button type="button" class="om-btn primary slides-shell-new">+ New text</button>
            </div>
        </div>
        <div class="om-subnav slides-shell-subnav" role="tablist"></div>
    </section>
`;

/**
 * Mount the slides shell into `container`. Caller is responsible for
 * mounting the three child panels separately into the .tab-pane[data-tab=X]
 * slots that this shell expects to find as siblings.
 *
 * Single-mount only — attaches a `hashchange` listener to `window` that
 * is never removed. Re-mounting would leak listeners pointing at the
 * old subnav DOM.
 *
 * @param {HTMLElement} container — host for the shell chrome.
 * @param {object} options
 * @param {() => Promise<Array>} options.fetchItems — yields all content
 *   items so the shell can compute per-tab counts.
 * @param {(tabKey: string) => void} options.onAddNew — invoked when the
 *   +New button is clicked. Tab key is the active tab.
 * @param {HTMLElement} options.tabPanesHost — element whose direct children
 *   `.tab-pane[data-tab=X]` get hidden / unhidden as the active tab changes.
 * @returns {{ refreshCounts: () => Promise<void>, setActive: (key) => void }}
 */
export function mountSlidesShell(container, { fetchItems, onAddNew, tabPanesHost }) {
    container.innerHTML = TEMPLATE;
    const subnav = container.querySelector(".slides-shell-subnav");
    const newBtn = container.querySelector(".slides-shell-new");

    for (const tab of TABS) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.role = "tab";
        btn.dataset.tab = tab.key;
        btn.innerHTML = `${tab.label} <span class="om-count" data-count="${tab.key}">·</span>`;
        btn.addEventListener("click", () => {
            // Mutate the URL — the hashchange listener does the rest, so
            // back/forward navigation between tabs works.
            window.location.hash = `#/slides/${tab.key}`;
        });
        subnav.appendChild(btn);
    }

    function activeTabFromHash() {
        const raw = (window.location.hash || "").replace(/^#\/?/, "");
        if (raw.startsWith("slides/")) {
            const key = raw.slice("slides/".length);
            if (TABS.some((t) => t.key === key)) return key;
        }
        return "text";
    }

    function setActive(key) {
        for (const btn of subnav.querySelectorAll("button[data-tab]")) {
            const isActive = btn.dataset.tab === key;
            btn.classList.toggle("active", isActive);
            if (isActive) btn.setAttribute("aria-selected", "true");
            else btn.removeAttribute("aria-selected");
        }
        // :scope > so a child editor that ever drops its own .tab-pane
        // descendant doesn't get accidentally hidden by us.
        for (const pane of tabPanesHost.querySelectorAll(":scope > .tab-pane[data-tab]")) {
            pane.hidden = pane.dataset.tab !== key;
        }
        const tab = TABS.find((t) => t.key === key);
        if (tab) newBtn.textContent = `+ New ${tab.noun}`;
    }

    async function refreshCounts() {
        if (!fetchItems) return;
        try {
            const items = await fetchItems();
            for (const tab of TABS) {
                const count = items.filter((i) => i.type === tab.type).length;
                const slot = subnav.querySelector(`[data-count="${tab.key}"]`);
                if (slot) slot.textContent = String(count);
            }
        } catch {
            // Counts are decorative; on failure just leave the last-known value.
        }
    }

    newBtn.addEventListener("click", () => {
        const key = activeTabFromHash();
        if (onAddNew) onAddNew(key);
    });

    window.addEventListener("hashchange", () => setActive(activeTabFromHash()));
    setActive(activeTabFromHash());
    refreshCounts();

    return { refreshCounts, setActive };
}
