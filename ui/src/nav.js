// Sidebar nav + hash-based section routing.
//
// The UI is one page with four panels (slides, playlists, schedule,
// settings). Each panel mounts once at boot and stays in the DOM; the
// nav just toggles a `hidden` attribute so the selected panel is the
// only visible one. This keeps each panel's internal state (scroll
// position, in-progress edits, polling loops) alive across nav clicks
// — no re-mounting, no re-fetching.
//
// Routing is hash-based (`#/slides`, `#/playlists`, `#/schedule`,
// `#/settings`). Unknown or missing hashes fall back to the default
// section. No History API, no router library — hashchange is all we
// need for a captive-portal single-page app.

const DEFAULT_SECTION = "slides";

/**
 * Wire up the sidebar + hash routing.
 *
 * @param {object} options
 * @param {HTMLElement} options.main — <main id="app"> host
 * @param {HTMLElement} options.sidebar — <nav> containing `.nav-link`s
 * @param {string[]} options.sections — section ids in display order
 *   (must correspond to `data-section` values in the sidebar AND to
 *   `<section data-section=...>` elements inside `main`)
 * @returns {{ show: (name: string) => void, current: () => string }}
 */
export function mountNav({ main, sidebar, sections }) {
    const validSections = new Set(sections);
    const links = Array.from(sidebar.querySelectorAll(".nav-link"));
    const sectionEls = new Map(
        Array.from(main.querySelectorAll("[data-section]")).map((el) => [
            el.dataset.section,
            el,
        ]),
    );

    function parseHash() {
        const raw = (window.location.hash || "").replace(/^#\/?/, "");
        return validSections.has(raw) ? raw : DEFAULT_SECTION;
    }

    function show(name) {
        const target = validSections.has(name) ? name : DEFAULT_SECTION;
        for (const [id, el] of sectionEls) {
            el.hidden = id !== target;
        }
        for (const link of links) {
            const active = link.dataset.section === target;
            link.classList.toggle("active", active);
            if (active) {
                link.setAttribute("aria-current", "page");
            } else {
                link.removeAttribute("aria-current");
            }
        }
    }

    window.addEventListener("hashchange", () => show(parseHash()));
    show(parseHash());
    // If we landed on a URL without a hash, canonicalize to the default so
    // reloads come back to the same section.
    if (!window.location.hash) {
        history.replaceState(null, "", `#/${DEFAULT_SECTION}`);
    }

    return {
        show,
        current: () => parseHash(),
    };
}
