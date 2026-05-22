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
});
