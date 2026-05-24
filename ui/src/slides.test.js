// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mountSlidesShell } from "./slides.js";

function tick() {
    return new Promise((r) => setTimeout(r, 0));
}

function buildTabPanesHost() {
    // mountSlidesShell expects a host whose direct children are
    // `.tab-pane[data-tab=X]` elements -- one per tab key. setActive()
    // toggles `.hidden` on each. Build the same 5 panes the real app
    // ships in slides.html so the show/hide assertions have something
    // to read.
    const host = document.createElement("div");
    for (const key of ["text", "image", "video", "stream", "web"]) {
        const pane = document.createElement("div");
        pane.className = "tab-pane";
        pane.dataset.tab = key;
        host.appendChild(pane);
    }
    return host;
}

function fireHashChange() {
    // jsdom's programmatic `location.hash = "..."` fires hashchange
    // synchronously in recent versions, but to keep tests independent
    // of jsdom-version quirks, dispatch the event ourselves after each
    // hash mutation.
    window.dispatchEvent(new Event("hashchange"));
}

afterEach(() => {
    // Clean slate between tests so a prior `#/slides/video` hash
    // doesn't bleed into the next mount's deep-link resolution.
    window.location.hash = "";
    vi.restoreAllMocks();
});

describe("mountSlidesShell — tab switching", () => {
    it("clicking a tab updates the URL hash and marks the tab + pane active", async () => {
        const container = document.createElement("div");
        const tabPanesHost = buildTabPanesHost();
        mountSlidesShell(container, {
            fetchItems: async () => [],
            onAddNew: vi.fn(),
            tabPanesHost,
        });
        await tick();

        // Default mount lands on "text" -- preconditions to confirm
        // the post-click delta isn't a leftover from a stale state.
        expect(
            container.querySelector('button[data-tab="text"]').classList.contains("active"),
        ).toBe(true);
        expect(tabPanesHost.querySelector('.tab-pane[data-tab="text"]').hidden).toBe(false);

        const imageTabBtn = container.querySelector('button[data-tab="image"]');
        imageTabBtn.click();
        // The click sets `window.location.hash`; the hashchange listener
        // is what mutates the DOM. Fire it explicitly.
        fireHashChange();

        expect(window.location.hash).toBe("#/slides/image");
        expect(imageTabBtn.classList.contains("active")).toBe(true);
        expect(imageTabBtn.getAttribute("aria-selected")).toBe("true");
        // The previously-active "text" tab should have been deactivated.
        const textTabBtn = container.querySelector('button[data-tab="text"]');
        expect(textTabBtn.classList.contains("active")).toBe(false);
        expect(textTabBtn.hasAttribute("aria-selected")).toBe(false);

        // Pane visibility flips with the active tab.
        expect(tabPanesHost.querySelector('.tab-pane[data-tab="image"]').hidden).toBe(false);
        expect(tabPanesHost.querySelector('.tab-pane[data-tab="text"]').hidden).toBe(true);
    });

    it("updates the +New button label to reflect the active tab's noun", async () => {
        const container = document.createElement("div");
        mountSlidesShell(container, {
            fetchItems: async () => [],
            onAddNew: vi.fn(),
            tabPanesHost: buildTabPanesHost(),
        });
        await tick();

        const newBtn = container.querySelector(".slides-shell-new");
        // Default tab is "text" → "+ New text".
        expect(newBtn.textContent).toBe("+ New text");

        container.querySelector('button[data-tab="image"]').click();
        fireHashChange();
        expect(newBtn.textContent).toBe("+ New image");

        // "stream" tab's noun is capital-S "Stream" by design (per the
        // TABS table). Verify the label preserves that casing rather
        // than lowercasing -- a label refactor that lowercased nouns
        // would silently regress without this check.
        container.querySelector('button[data-tab="stream"]').click();
        fireHashChange();
        expect(newBtn.textContent).toBe("+ New Stream");
    });

    it("clicking +New invokes onAddNew with the currently-active tab key", async () => {
        const onAddNew = vi.fn();
        const container = document.createElement("div");
        mountSlidesShell(container, {
            fetchItems: async () => [],
            onAddNew,
            tabPanesHost: buildTabPanesHost(),
        });
        await tick();

        container.querySelector(".slides-shell-new").click();
        expect(onAddNew).toHaveBeenCalledTimes(1);
        expect(onAddNew).toHaveBeenLastCalledWith("text");

        // Switch tabs + click again; the active-key should travel.
        container.querySelector('button[data-tab="video"]').click();
        fireHashChange();
        container.querySelector(".slides-shell-new").click();
        expect(onAddNew).toHaveBeenCalledTimes(2);
        expect(onAddNew).toHaveBeenLastCalledWith("video");
    });
});

describe("mountSlidesShell — hash deep-linking", () => {
    it("respects #/slides/<tab> on initial mount", async () => {
        window.location.hash = "#/slides/video";
        const container = document.createElement("div");
        const tabPanesHost = buildTabPanesHost();
        mountSlidesShell(container, {
            fetchItems: async () => [],
            onAddNew: vi.fn(),
            tabPanesHost,
        });
        await tick();

        expect(
            container.querySelector('button[data-tab="video"]').classList.contains("active"),
        ).toBe(true);
        expect(tabPanesHost.querySelector('.tab-pane[data-tab="video"]').hidden).toBe(false);
        // Default "text" pane is NOT visible -- ensures we didn't fall
        // back to the default after the deep-link resolution.
        expect(tabPanesHost.querySelector('.tab-pane[data-tab="text"]').hidden).toBe(true);
    });

    it("accepts the leading-slash-less #slides/<tab> form too", async () => {
        // activeTabFromHash() strips `^#\/?` so both `#/slides/x` and
        // `#slides/x` resolve. Lock this in -- a future regex tightening
        // that broke the loose form would silently kill older bookmarks.
        window.location.hash = "#slides/stream";
        const container = document.createElement("div");
        const tabPanesHost = buildTabPanesHost();
        mountSlidesShell(container, {
            fetchItems: async () => [],
            onAddNew: vi.fn(),
            tabPanesHost,
        });
        await tick();

        expect(
            container.querySelector('button[data-tab="stream"]').classList.contains("active"),
        ).toBe(true);
        expect(tabPanesHost.querySelector('.tab-pane[data-tab="stream"]').hidden).toBe(false);
    });

    it("falls back to the default 'text' tab on an unrecognized hash", async () => {
        window.location.hash = "#/somewhere-else";
        const container = document.createElement("div");
        const tabPanesHost = buildTabPanesHost();
        mountSlidesShell(container, {
            fetchItems: async () => [],
            onAddNew: vi.fn(),
            tabPanesHost,
        });
        await tick();

        expect(
            container.querySelector('button[data-tab="text"]').classList.contains("active"),
        ).toBe(true);
        expect(tabPanesHost.querySelector('.tab-pane[data-tab="text"]').hidden).toBe(false);
    });

    it("hashchange after mount routes to the new tab (back/forward navigation)", async () => {
        const container = document.createElement("div");
        const tabPanesHost = buildTabPanesHost();
        mountSlidesShell(container, {
            fetchItems: async () => [],
            onAddNew: vi.fn(),
            tabPanesHost,
        });
        await tick();
        // Mount lands on "text" by default.
        expect(
            container.querySelector('button[data-tab="text"]').classList.contains("active"),
        ).toBe(true);

        // Simulate the browser's back/forward causing a hashchange --
        // operator-visible because it's how url-driven tab navigation
        // works when the operator hits the back button.
        window.location.hash = "#/slides/web";
        fireHashChange();

        expect(
            container.querySelector('button[data-tab="web"]').classList.contains("active"),
        ).toBe(true);
        expect(tabPanesHost.querySelector('.tab-pane[data-tab="web"]').hidden).toBe(false);
    });
});

describe("mountSlidesShell — refreshCounts", () => {
    // Scope clarification: slides.js does NOT itself listen for
    // `openmarquee:delete-slide`. The post-delete refresh is wired
    // EXTERNALLY in main.js:1026 (`slidesShell?.refreshCounts?.()` in
    // the central delete event handler). These tests cover the
    // contract slides.js exposes -- `handle.refreshCounts()` re-reads
    // fetchItems + updates the count slot per tab -- which is what
    // the external caller drives after delete.

    it("populates per-tab counts on initial mount from fetchItems", async () => {
        const items = [
            { id: "a", type: "text_slide" },
            { id: "b", type: "text_slide" },
            { id: "c", type: "image" },
            { id: "d", type: "video" },
            { id: "e", type: "video" },
            { id: "f", type: "video" },
            { id: "g", type: "stream" },
            // No web items -- count should be "0", not "·" (the
            // pre-refresh placeholder), so a 0-count tab is visibly
            // distinct from a never-fetched one.
        ];
        const container = document.createElement("div");
        mountSlidesShell(container, {
            fetchItems: async () => items,
            onAddNew: vi.fn(),
            tabPanesHost: buildTabPanesHost(),
        });
        // Initial mount kicks an async refreshCounts(); drain it.
        await tick();
        await tick();

        const countOf = (key) =>
            container.querySelector(`[data-count="${key}"]`).textContent;
        expect(countOf("text")).toBe("2");
        expect(countOf("image")).toBe("1");
        expect(countOf("video")).toBe("3");
        expect(countOf("stream")).toBe("1");
        expect(countOf("web")).toBe("0");
    });

    it("handle.refreshCounts() re-reads fetchItems and updates the slots", async () => {
        let items = [
            { id: "a", type: "text_slide" },
            { id: "b", type: "image" },
        ];
        const fetchItems = vi.fn(async () => items);
        const container = document.createElement("div");
        const handle = mountSlidesShell(container, {
            fetchItems,
            onAddNew: vi.fn(),
            tabPanesHost: buildTabPanesHost(),
        });
        await tick();
        await tick();

        expect(container.querySelector('[data-count="text"]').textContent).toBe("1");
        expect(container.querySelector('[data-count="image"]').textContent).toBe("1");
        expect(fetchItems).toHaveBeenCalledTimes(1);

        // Simulate the post-delete-event flow: caller mutates the
        // underlying data + invokes refreshCounts(). slides.js should
        // re-fetch + repaint without re-mount.
        items = [{ id: "a", type: "text_slide" }];
        await handle.refreshCounts();

        expect(fetchItems).toHaveBeenCalledTimes(2);
        expect(container.querySelector('[data-count="text"]').textContent).toBe("1");
        expect(container.querySelector('[data-count="image"]').textContent).toBe("0");
    });

    it("leaves last-known counts intact when fetchItems throws", async () => {
        // The slides.js doc-comment says "Counts are decorative; on
        // failure just leave the last-known value." Lock that behavior.
        let shouldFail = false;
        const fetchItems = vi.fn(async () => {
            if (shouldFail) throw new Error("network blip");
            return [
                { id: "a", type: "text_slide" },
                { id: "b", type: "text_slide" },
            ];
        });
        const container = document.createElement("div");
        const handle = mountSlidesShell(container, {
            fetchItems,
            onAddNew: vi.fn(),
            tabPanesHost: buildTabPanesHost(),
        });
        await tick();
        await tick();
        expect(container.querySelector('[data-count="text"]').textContent).toBe("2");

        shouldFail = true;
        await handle.refreshCounts();

        // Failed refresh: the existing "2" persists; no throw escapes.
        expect(container.querySelector('[data-count="text"]').textContent).toBe("2");
    });
});

describe("mountSlidesShell — setActive (programmatic)", () => {
    it("exposes setActive(key) so callers can drive the tab without a hash mutation", async () => {
        const container = document.createElement("div");
        const tabPanesHost = buildTabPanesHost();
        const handle = mountSlidesShell(container, {
            fetchItems: async () => [],
            onAddNew: vi.fn(),
            tabPanesHost,
        });
        await tick();

        handle.setActive("video");
        expect(
            container.querySelector('button[data-tab="video"]').classList.contains("active"),
        ).toBe(true);
        expect(tabPanesHost.querySelector('.tab-pane[data-tab="video"]').hidden).toBe(false);
        // NB: setActive doesn't touch window.location.hash by design
        // (only the click handler does). That's correct for callers
        // that want to align UI to a model change without polluting
        // the URL.
        expect(window.location.hash).toBe("");
    });
});
