// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mountNav } from "./nav.js";

const SECTIONS = ["slides", "playlists", "schedule", "settings"];
const HIERARCHICAL = [
    "slides/text",
    "slides/image",
    "playlists",
    "settings",
];

function buildShell() {
    document.body.innerHTML = `
        <nav class="sidebar">
            <ul class="nav-list">
                <li><a class="nav-link" href="#/slides" data-section="slides">Slides</a></li>
                <li><a class="nav-link" href="#/playlists" data-section="playlists">Playlists</a></li>
                <li><a class="nav-link" href="#/schedule" data-section="schedule">Schedule</a></li>
                <li><a class="nav-link" href="#/settings" data-section="settings">Settings</a></li>
            </ul>
        </nav>
        <main id="app">
            <section data-section="slides">slides panel</section>
            <section data-section="playlists">playlists panel</section>
            <section data-section="schedule">schedule panel</section>
            <section data-section="settings">settings panel</section>
        </main>
    `;
    return {
        main: document.getElementById("app"),
        sidebar: document.querySelector(".sidebar"),
    };
}

function buildHierShell() {
    document.body.innerHTML = `
        <nav class="sidebar">
            <ul class="nav-list">
                <li><a class="nav-link nav-child" href="#/slides/text" data-section="slides/text">Text</a></li>
                <li><a class="nav-link nav-child" href="#/slides/image" data-section="slides/image">Image</a></li>
                <li><a class="nav-link" href="#/playlists" data-section="playlists">Playlists</a></li>
                <li><a class="nav-link" href="#/settings" data-section="settings">Settings</a></li>
            </ul>
        </nav>
        <main id="app">
            <section data-section="slides/text">text</section>
            <section data-section="slides/image">image</section>
            <section data-section="playlists">p</section>
            <section data-section="settings">s</section>
        </main>
    `;
    return {
        main: document.getElementById("app"),
        sidebar: document.querySelector(".sidebar"),
    };
}

beforeEach(() => {
    history.replaceState(null, "", "/");
});

afterEach(() => {
    document.body.innerHTML = "";
});

describe("mountNav", () => {
    it("defaults to the first section when the hash is empty", () => {
        const shell = buildShell();
        mountNav({ ...shell, sections: SECTIONS });
        // Scope to #app so we don't pick up the sidebar's nav-links (they
        // also carry `data-section`).
        const main = document.getElementById("app");
        const slides = main.querySelector('[data-section="slides"]');
        expect(slides.hidden).toBe(false);
        const others = main.querySelectorAll(
            '[data-section]:not([data-section="slides"])',
        );
        others.forEach((el) => expect(el.hidden).toBe(true));
    });

    it("canonicalizes a bare URL to the default hash", () => {
        const shell = buildShell();
        mountNav({ ...shell, sections: SECTIONS });
        expect(window.location.hash).toBe("#/slides");
    });

    it("honors an initial hash on mount", () => {
        history.replaceState(null, "", "#/schedule");
        const shell = buildShell();
        mountNav({ ...shell, sections: SECTIONS });
        const main = document.getElementById("app");
        expect(main.querySelector('[data-section="schedule"]').hidden).toBe(false);
    });

    it("swaps sections on hashchange", () => {
        const shell = buildShell();
        mountNav({ ...shell, sections: SECTIONS });
        window.location.hash = "#/playlists";
        window.dispatchEvent(new HashChangeEvent("hashchange"));
        const main = document.getElementById("app");
        expect(main.querySelector('[data-section="playlists"]').hidden).toBe(false);
        expect(main.querySelector('[data-section="slides"]').hidden).toBe(true);
    });

    it("falls back to default on an unknown section", () => {
        history.replaceState(null, "", "#/nope");
        const shell = buildShell();
        mountNav({ ...shell, sections: SECTIONS });
        const main = document.getElementById("app");
        expect(main.querySelector('[data-section="slides"]').hidden).toBe(false);
    });

    it("marks the active link with aria-current and .active class", () => {
        history.replaceState(null, "", "#/schedule");
        const shell = buildShell();
        mountNav({ ...shell, sections: SECTIONS });
        const scheduleLink = document.querySelector(
            '.nav-link[data-section="schedule"]',
        );
        expect(scheduleLink.classList.contains("active")).toBe(true);
        expect(scheduleLink.getAttribute("aria-current")).toBe("page");
        const slidesLink = document.querySelector(
            '.nav-link[data-section="slides"]',
        );
        expect(slidesLink.classList.contains("active")).toBe(false);
        expect(slidesLink.getAttribute("aria-current")).toBeNull();
    });

    // --- hierarchical section names (slides/text etc.) ---

    it("routes hierarchical sections via the full slash-separated name", () => {
        history.replaceState(null, "", "#/slides/image");
        const shell = buildHierShell();
        mountNav({
            ...shell,
            sections: HIERARCHICAL,
            defaultSection: "slides/text",
        });
        const main = document.getElementById("app");
        expect(main.querySelector('[data-section="slides/image"]').hidden).toBe(false);
        expect(main.querySelector('[data-section="slides/text"]').hidden).toBe(true);
    });

    it("canonicalizes a bare URL to the provided defaultSection", () => {
        const shell = buildHierShell();
        mountNav({
            ...shell,
            sections: HIERARCHICAL,
            defaultSection: "slides/text",
        });
        expect(window.location.hash).toBe("#/slides/text");
    });
});
