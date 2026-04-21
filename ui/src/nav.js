// Sidebar nav + hash-based section routing.
//
// The UI is one page with N panels (text / image / video / auto /
// playlists / schedule / settings). Each panel mounts once at boot and
// stays in the DOM; the nav just toggles a `hidden` attribute so the
// selected panel is the only visible one. This keeps each panel's
// internal state (scroll position, in-progress edits, polling loops)
// alive across nav clicks — no re-mounting, no re-fetching.
//
// Routing is hash-based. Section names can contain a `/` to express
// hierarchy (`slides/text`, `slides/image`, …) — the URL is the
// straightforward `#/slides/text` form and the sidebar renders the
// prefix as a group header + indented children. Unknown or missing
// hashes fall back to the default section. No History API, no router
// library — hashchange is all we need for a captive-portal single-page
// app.

/**
 * Wire up the sidebar + hash routing.
 *
 * @param {object} options
 * @param {HTMLElement} options.main — <main id="app"> host
 * @param {HTMLElement} options.sidebar — <nav> containing `.nav-link`s
 * @param {string[]} options.sections — section ids in display order
 *   (must correspond to `data-section` values in the sidebar AND to
 *   `<section data-section=...>` elements inside `main`). A `/` in
 *   the name is treated as a hierarchy separator for URLs only.
 * @param {string} [options.defaultSection] — section to show when the
 *   hash is missing or invalid. Defaults to the first entry in `sections`.
 * @returns {{ show: (name: string) => void, current: () => string }}
 */
export function mountNav({ main, sidebar, sections, defaultSection }) {
    const fallback = defaultSection || sections[0];
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
        return validSections.has(raw) ? raw : fallback;
    }

    function show(name) {
        const target = validSections.has(name) ? name : fallback;
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
        history.replaceState(null, "", `#/${fallback}`);
    }

    return {
        show,
        current: () => parseHash(),
    };
}
