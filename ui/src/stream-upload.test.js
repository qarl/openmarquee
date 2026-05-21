// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { mountStreamUploader } from "./stream-upload.js";

function tick() {
    return new Promise((r) => setTimeout(r, 0));
}

function fireInput(el) {
    el.dispatchEvent(new Event("input", { bubbles: true }));
}

describe("mountStreamUploader", () => {
    it("renders the metadata form — name, stream URL, duration, fallback radios, no file input", () => {
        const container = document.createElement("div");
        mountStreamUploader(container, { onSave: vi.fn() });

        expect(container.querySelector(".field-name")).not.toBeNull();
        expect(container.querySelector(".field-stream-url")).not.toBeNull();
        expect(container.querySelector(".field-duration")).not.toBeNull();
        expect(
            container.querySelectorAll('input[name="stream-on-unreachable"]').length,
        ).toBe(3);
        expect(container.querySelector(".om-save-status")).not.toBeNull();
        // A stream slide has no asset upload — there is no file input.
        expect(container.querySelector('input[type="file"]')).toBeNull();
    });

    it("auto-save with an empty stream URL does nothing (canSave gate suppresses)", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn();
        const handle = mountStreamUploader(container, { onSave });
        await tick();

        const nameEl = container.querySelector(".field-name");
        nameEl.value = "Renamed but no URL";
        fireInput(nameEl);
        await handle.flushAutoSave();
        expect(onSave).not.toHaveBeenCalled();
    });

    it("auto-save with a URL creates the slide with the right payload + defaults", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue({ id: "stream-1" });
        const handle = mountStreamUploader(container, { onSave });
        await tick();

        const urlEl = container.querySelector(".field-stream-url");
        urlEl.value = "rtsp://laptop:8554/live";
        fireInput(urlEl);
        await handle.flushAutoSave();

        expect(onSave).toHaveBeenCalledTimes(1);
        const payload = onSave.mock.calls[0][0];
        expect(payload.stream_url).toBe("rtsp://laptop:8554/live");
        expect(payload.duration_ms).toBe(10_000);
        expect(payload.on_unreachable).toBe("hold_last_frame");
        expect(payload.transition).toBe("cut");
        expect(payload.transition_ms).toBe(500);
    });

    it("the on_unreachable radio selection is carried into the payload", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue({ id: "stream-1" });
        const handle = mountStreamUploader(container, { onSave });
        await tick();

        container.querySelector(".field-stream-url").value = "rtsp://h/x";
        const blackRadio = container.querySelector(
            'input[name="stream-on-unreachable"][value="black"]',
        );
        blackRadio.checked = true;
        fireInput(blackRadio);
        await handle.flushAutoSave();

        expect(onSave.mock.calls[0][0].on_unreachable).toBe("black");
    });

    it("loadForEdit pre-fills the form and round-trips the transition on save", async () => {
        const container = document.createElement("div");
        const onSaveExisting = vi.fn().mockResolvedValue({ id: "stream-7" });
        const handle = mountStreamUploader(container, {
            onSave: vi.fn(),
            onSaveExisting,
        });

        handle.loadForEdit({
            type: "stream",
            id: "stream-7",
            name: "Q3 Live",
            stream_url: "rtsp://host:8554/q3",
            duration_ms: 20_000,
            on_unreachable: "skip",
            transition: "fade",
            transition_ms: 300,
        });
        await tick();

        expect(container.querySelector(".field-name").value).toBe("Q3 Live");
        expect(container.querySelector(".field-stream-url").value).toBe(
            "rtsp://host:8554/q3",
        );
        expect(container.querySelector(".field-duration").value).toBe("20");
        expect(
            container.querySelector(
                'input[name="stream-on-unreachable"]:checked',
            ).value,
        ).toBe("skip");

        // Editing only the name must NOT reset the transition the slide
        // already had (it isn't an editable field in this form).
        const nameEl = container.querySelector(".field-name");
        nameEl.value = "Q3 Live (renamed)";
        fireInput(nameEl);
        await handle.flushAutoSave();

        expect(onSaveExisting).toHaveBeenCalledTimes(1);
        const [id, payload] = onSaveExisting.mock.calls[0];
        expect(id).toBe("stream-7");
        expect(payload.name).toBe("Q3 Live (renamed)");
        expect(payload.transition).toBe("fade");
        expect(payload.transition_ms).toBe(300);
    });

    it("the placeholder preview card shows the typed stream URL", () => {
        const container = document.createElement("div");
        mountStreamUploader(container, { onSave: vi.fn() });

        const urlEl = container.querySelector(".field-stream-url");
        urlEl.value = "rtsp://shown:8554/x";
        fireInput(urlEl);
        expect(
            container.querySelector(".stream-preview-url").textContent,
        ).toBe("rtsp://shown:8554/x");
    });
});
