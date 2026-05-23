// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { mountWebSlideEditor } from "./web-slide.js";

function tick() {
    return new Promise((r) => setTimeout(r, 0));
}

function fireInput(el) {
    el.dispatchEvent(new Event("input", { bubbles: true }));
}

describe("mountWebSlideEditor", () => {
    it("renders the metadata form — name, page URL, refresh, duration, no file input", () => {
        const container = document.createElement("div");
        mountWebSlideEditor(container, { onSave: vi.fn() });

        expect(container.querySelector(".field-name")).not.toBeNull();
        expect(container.querySelector(".field-web-url")).not.toBeNull();
        expect(container.querySelector(".field-web-refresh")).not.toBeNull();
        expect(container.querySelector(".field-duration")).not.toBeNull();
        expect(container.querySelector(".om-save-status")).not.toBeNull();
        // A web slide has no asset upload — there is no file input.
        expect(container.querySelector('input[type="file"]')).toBeNull();
        // The refresh select is populated with the common cadences.
        expect(
            container.querySelectorAll(".field-web-refresh option").length,
        ).toBeGreaterThan(1);
        // Default refresh is 1 hour (3600s).
        expect(container.querySelector(".field-web-refresh").value).toBe("3600");
    });

    it("auto-save with an empty page URL does nothing (canSave gate suppresses)", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn();
        const handle = mountWebSlideEditor(container, { onSave });
        await tick();

        const nameEl = container.querySelector(".field-name");
        nameEl.value = "Renamed but no URL";
        fireInput(nameEl);
        await handle.flushAutoSave();
        expect(onSave).not.toHaveBeenCalled();
    });

    it("auto-save with a URL creates the slide with the right payload + defaults", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue({ id: "web-1" });
        const handle = mountWebSlideEditor(container, { onSave });
        await tick();

        const urlEl = container.querySelector(".field-web-url");
        urlEl.value = "https://status.example.com";
        fireInput(urlEl);
        await handle.flushAutoSave();

        expect(onSave).toHaveBeenCalledTimes(1);
        const payload = onSave.mock.calls[0][0];
        expect(payload.url).toBe("https://status.example.com");
        expect(payload.refresh_interval_s).toBe(3600);
        expect(payload.duration_ms).toBe(10_000);
        expect(payload.transition).toBe("cut");
        expect(payload.transition_ms).toBe(500);
        // Pure metadata — no screenshot is uploaded from the editor.
        expect(payload.png_base64).toBeUndefined();
    });

    it("the chosen refresh interval is carried into the payload", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue({ id: "web-1" });
        const handle = mountWebSlideEditor(container, { onSave });
        await tick();

        container.querySelector(".field-web-url").value = "https://h/x";
        const refreshEl = container.querySelector(".field-web-refresh");
        // A non-default choice (the default is 3600) so this proves the
        // chosen value — not the default — is what reaches the payload.
        refreshEl.value = "300";
        fireInput(refreshEl);
        await handle.flushAutoSave();

        expect(onSave.mock.calls[0][0].refresh_interval_s).toBe(300);
    });

    it("loadForEdit pre-fills the form and round-trips the transition on save", async () => {
        const container = document.createElement("div");
        const onSaveExisting = vi.fn().mockResolvedValue({ id: "web-7" });
        const handle = mountWebSlideEditor(container, {
            onSave: vi.fn(),
            onSaveExisting,
        });

        handle.loadForEdit({
            type: "web",
            id: "web-7",
            name: "Status board",
            url: "https://status.example.com/board",
            refresh_interval_s: 900,
            duration_ms: 20_000,
            transition: "fade",
            transition_ms: 300,
        });
        await tick();

        expect(container.querySelector(".field-name").value).toBe(
            "Status board",
        );
        expect(container.querySelector(".field-web-url").value).toBe(
            "https://status.example.com/board",
        );
        expect(container.querySelector(".field-web-refresh").value).toBe("900");
        expect(container.querySelector(".field-duration").value).toBe("20");

        // Editing only the name must NOT reset the transition the slide
        // already had (it isn't an editable field in this form).
        const nameEl = container.querySelector(".field-name");
        nameEl.value = "Status board (renamed)";
        fireInput(nameEl);
        await handle.flushAutoSave();

        expect(onSaveExisting).toHaveBeenCalledTimes(1);
        const [id, payload] = onSaveExisting.mock.calls[0];
        expect(id).toBe("web-7");
        expect(payload.name).toBe("Status board (renamed)");
        expect(payload.url).toBe("https://status.example.com/board");
        expect(payload.transition).toBe("fade");
        expect(payload.transition_ms).toBe(300);
    });

    it("loadForEdit snaps an off-list refresh interval to the nearest option", async () => {
        const container = document.createElement("div");
        const handle = mountWebSlideEditor(container, { onSave: vi.fn() });

        // 280s isn't an offered cadence — the select must still show a
        // value; 300s (5 min) is nearest.
        handle.loadForEdit({
            type: "web",
            id: "web-9",
            name: "Odd interval",
            url: "https://h/x",
            refresh_interval_s: 280,
            duration_ms: 10_000,
        });
        await tick();

        expect(container.querySelector(".field-web-refresh").value).toBe("300");
    });

    it("the placeholder preview card shows the typed page URL", () => {
        const container = document.createElement("div");
        mountWebSlideEditor(container, { onSave: vi.fn() });

        const urlEl = container.querySelector(".field-web-url");
        urlEl.value = "https://shown.example.com/x";
        fireInput(urlEl);
        expect(
            container.querySelector(".web-preview-url").textContent,
        ).toBe("https://shown.example.com/x");
    });

    it("unsaved (no editingId) keeps the placeholder visible and hides the screenshot img", () => {
        // Bug #5 (qarl 2026-05-23): the editor preview pane was a
        // placeholder card forever. For a brand-new draft (no id
        // yet), there's no asset on the server to fetch — so the
        // placeholder remains the only signal the operator gets.
        // This pins that fallback: the img stays hidden until a
        // save lands an id.
        const container = document.createElement("div");
        mountWebSlideEditor(container, { onSave: vi.fn() });

        const screenshot = container.querySelector(".web-preview-screenshot");
        const placeholder = container.querySelector(".web-preview-card");
        expect(screenshot).not.toBeNull();
        expect(screenshot.hidden).toBe(true);
        expect(screenshot.getAttribute("src")).toBeNull();
        expect(placeholder.hidden).toBe(false);
    });

    it("loadForEdit shows the saved slide's screenshot via /api/content/{id}/asset with a cache-bust", async () => {
        // Bug #5 fix: for a SAVED web slide, the editor pane previews
        // the same asset.png the slide-browser tile thumb does — the
        // backend always has SOMETHING at /asset (either the Pi's
        // rendered screenshot or the just-created placeholder PNG
        // synthesised by storage.save_web). The img src must include
        // the auth-token query param (via mediaSrc) AND a `?v=` cache-
        // bust whose value reflects the envelope's updated_at, so an
        // updated screenshot doesn't get masked by an HTTP-cached old
        // copy.
        const container = document.createElement("div");
        const handle = mountWebSlideEditor(container, { onSave: vi.fn() });

        handle.loadForEdit({
            type: "web",
            id: "web-42",
            name: "Status",
            url: "https://h/x",
            refresh_interval_s: 3600,
            duration_ms: 10_000,
            updated_at: "2026-05-23T01:23:45+00:00",
        });

        const screenshot = container.querySelector(".web-preview-screenshot");
        const placeholder = container.querySelector(".web-preview-card");
        expect(screenshot.hidden).toBe(false);
        expect(placeholder.hidden).toBe(true);
        expect(screenshot.getAttribute("src")).toContain(
            "/api/content/web-42/asset",
        );
        expect(screenshot.getAttribute("src")).toContain("v=");
        // The cache-bust value reflects updated_at, not Date.now() —
        // a stale browser cache after a peer-flock sync re-rendered
        // the asset must invalidate cleanly.
        expect(screenshot.getAttribute("src")).toContain(
            encodeURIComponent("2026-05-23T01:23:45+00:00"),
        );
    });
});
