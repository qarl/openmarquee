// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { canvasToBase64, drawCanvas, mountEditor, pickFontSize } from "./editor.js";

function mockCanvas(width, height) {
    const ctx = {
        fillStyle: "",
        font: "",
        textAlign: "",
        textBaseline: "",
        fillRect: vi.fn(),
        fillText: vi.fn(),
        save: vi.fn(),
        restore: vi.fn(),
    };
    return {
        width,
        height,
        getContext: () => ctx,
        toDataURL: () => "data:image/png;base64,FAKEDATA",
        _ctx: ctx,
    };
}

describe("drawCanvas", () => {
    it("always paints the background across the full canvas", () => {
        const canvas = mockCanvas(128, 96);
        drawCanvas(canvas, { text: "", backgroundColor: "#123456" });
        expect(canvas._ctx.fillRect).toHaveBeenCalledWith(0, 0, 128, 96);
    });

    it("skips text rendering when the text is empty", () => {
        const canvas = mockCanvas(128, 96);
        drawCanvas(canvas, { text: "" });
        expect(canvas._ctx.fillText).not.toHaveBeenCalled();
    });

    it("renders the text centered when there is text", () => {
        const canvas = mockCanvas(128, 96);
        drawCanvas(canvas, { text: "HI", textColor: "#fff", backgroundColor: "#000" });
        expect(canvas._ctx.textAlign).toBe("center");
        expect(canvas._ctx.textBaseline).toBe("middle");
        expect(canvas._ctx.fillText).toHaveBeenCalledTimes(1);
        const [line, x] = canvas._ctx.fillText.mock.calls[0];
        expect(line).toBe("HI");
        expect(x).toBe(64); // canvas.width / 2
    });

    it("renders multiline text as separate fillText calls", () => {
        const canvas = mockCanvas(128, 96);
        drawCanvas(canvas, { text: "LINE ONE\nLINE TWO" });
        expect(canvas._ctx.fillText).toHaveBeenCalledTimes(2);
        expect(canvas._ctx.fillText.mock.calls[0][0]).toBe("LINE ONE");
        expect(canvas._ctx.fillText.mock.calls[1][0]).toBe("LINE TWO");
    });

    it("applies the caller's colors", () => {
        const canvas = mockCanvas(64, 32);
        drawCanvas(canvas, { text: "x", textColor: "#FFAA00", backgroundColor: "#001122" });
        // fillStyle is set twice (bg then text). We just check the final value.
        expect(canvas._ctx.fillStyle).toBe("#FFAA00");
    });
});

describe("pickFontSize", () => {
    it("scales with panel height", () => {
        expect(pickFontSize(96)).toBeGreaterThan(pickFontSize(32));
    });

    it("has a floor so tiny panels still get readable text", () => {
        expect(pickFontSize(10)).toBeGreaterThanOrEqual(12);
    });
});

describe("drawCanvas — explicit fontSize override", () => {
    it("honors the caller's fontSize over the panel-height heuristic", () => {
        const canvas = mockCanvas(128, 96);
        drawCanvas(canvas, { text: "X", fontSize: 17 });
        expect(canvas._ctx.font).toContain("17px");
    });

    it("falls back to the heuristic when fontSize is not a positive number", () => {
        const canvas = mockCanvas(128, 96);
        drawCanvas(canvas, { text: "X", fontSize: 0 });
        expect(canvas._ctx.font).toContain(`${pickFontSize(96)}px`);
    });
});

describe("drawCanvas — context isolation", () => {
    it("wraps in save/restore so leaked state doesn't escape", () => {
        const canvas = mockCanvas(64, 32);
        drawCanvas(canvas, { text: "hi", textColor: "#fff", backgroundColor: "#000" });
        expect(canvas._ctx.save).toHaveBeenCalledTimes(1);
        expect(canvas._ctx.restore).toHaveBeenCalledTimes(1);
    });

    it("constrains text width via maxWidth (avoids horizontal overflow)", () => {
        const canvas = mockCanvas(64, 32);
        drawCanvas(canvas, { text: "VERY LONG LINE OF TEXT" });
        const [, , , maxWidth] = canvas._ctx.fillText.mock.calls[0];
        expect(maxWidth).toBe(60); // canvas.width - 4
    });

    it("splits text on \\r\\n as well as \\n (iOS paste)", () => {
        const canvas = mockCanvas(128, 96);
        drawCanvas(canvas, { text: "A\r\nB" });
        expect(canvas._ctx.fillText).toHaveBeenCalledTimes(2);
        expect(canvas._ctx.fillText.mock.calls[0][0]).toBe("A");
        expect(canvas._ctx.fillText.mock.calls[1][0]).toBe("B");
    });
});

describe("canvasToBase64", () => {
    it("strips the data URL prefix, returning just the base64 body", () => {
        const canvas = mockCanvas(2, 2);
        expect(canvasToBase64(canvas)).toBe("FAKEDATA");
    });
});

// --- mountEditor ---
//
// jsdom's <canvas> doesn't implement getContext or toDataURL, so we patch
// the prototype before each mount-related test and restore it after.

function patchCanvasPrototype() {
    const fakeCtx = {
        fillStyle: "",
        font: "",
        textAlign: "",
        textBaseline: "",
        fillRect: vi.fn(),
        fillText: vi.fn(),
        save: vi.fn(),
        restore: vi.fn(),
    };
    const proto = HTMLCanvasElement.prototype;
    proto.getContext = vi.fn(() => fakeCtx);
    proto.toDataURL = vi.fn(() => "data:image/png;base64,STUBDATA");
    return fakeCtx;
}

describe("mountEditor — submit flow", () => {
    it("calls onSave with the wire-format payload (uppercased colors)", async () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue({ id: "abc" });

        mountEditor(container, { width: 128, height: 96, onSave });

        container.querySelector(".field-text").value = "Hi";
        container.querySelector(".field-text").dispatchEvent(new Event("input"));
        container.querySelector(".field-text-color").value = "#ffaa00";
        container.querySelector(".field-text-color").dispatchEvent(new Event("input"));

        container.querySelector(".controls").dispatchEvent(new Event("submit"));
        await new Promise((r) => setTimeout(r, 0));

        expect(onSave).toHaveBeenCalledOnce();
        const payload = onSave.mock.calls[0][0];
        expect(payload.text).toBe("Hi");
        expect(payload.text_color).toBe("#FFAA00");
        expect(payload.background_color).toBe("#000000");
        expect(payload.duration_ms).toBe(5000); // default 5s
        expect(payload.png_base64).toBe("STUBDATA");
        // Font-size defaults to the pickFontSizePct() default (% of width)
        // so the slide reads the same after a panel-resolution change.
        expect(payload.font_size_pct).toBe(30);
    });

    it("auto_mode defaults to null + the dynamic-content hint is hidden", async () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        mountEditor(container, {
            width: 128,
            height: 96,
            onSave: vi.fn().mockResolvedValue({}),
        });
        expect(container.querySelector(".field-auto-mode").value).toBe("");
        expect(container.querySelector(".field-auto-mode-hint").hidden).toBe(true);
    });

    it("picking an auto_mode reveals the hint + format dropdown + rides through save", async () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue({ id: "x" });
        mountEditor(container, { width: 128, height: 96, onSave });

        // Format dropdown starts hidden (no mode selected).
        expect(container.querySelector(".field-auto-format-wrap").hidden).toBe(true);

        const auto = container.querySelector(".field-auto-mode");
        auto.value = "time";
        auto.dispatchEvent(new Event("change"));
        expect(container.querySelector(".field-auto-mode-hint").hidden).toBe(false);

        // Format dropdown revealed with the two time options.
        expect(container.querySelector(".field-auto-format-wrap").hidden).toBe(false);
        const formatOptions = Array.from(
            container.querySelectorAll(".field-auto-format option"),
        ).map((o) => o.value);
        expect(formatOptions).toEqual(["time_hm", "time_hms"]);

        // Pick the HH:MM:SS option and save.
        const fmt = container.querySelector(".field-auto-format");
        fmt.value = "time_hms";

        container.querySelector(".field-text").value = "12:34 (fallback)";
        container.querySelector(".field-text").dispatchEvent(new Event("input"));
        container.querySelector(".controls").dispatchEvent(new Event("submit"));
        await new Promise((r) => setTimeout(r, 0));
        expect(onSave.mock.calls[0][0].auto_mode).toBe("time");
        expect(onSave.mock.calls[0][0].auto_format).toBe("time_hms");
    });

    it("switching the auto_mode repopulates the format dropdown with the new options", async () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        mountEditor(container, {
            width: 128,
            height: 96,
            onSave: vi.fn().mockResolvedValue({}),
        });
        const auto = container.querySelector(".field-auto-mode");

        auto.value = "date";
        auto.dispatchEvent(new Event("change"));
        let opts = Array.from(
            container.querySelectorAll(".field-auto-format option"),
        ).map((o) => o.value);
        expect(opts).toEqual(["date_iso", "date_long", "date_medium"]);

        auto.value = "day";
        auto.dispatchEvent(new Event("change"));
        opts = Array.from(
            container.querySelectorAll(".field-auto-format option"),
        ).map((o) => o.value);
        expect(opts).toEqual(["day_long", "day_short"]);

        // And switching OFF auto_mode hides the wrap entirely.
        auto.value = "";
        auto.dispatchEvent(new Event("change"));
        expect(container.querySelector(".field-auto-format-wrap").hidden).toBe(true);
    });

    it("sends the operator's font-size override when they edit it", async () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue({ id: "abc" });

        mountEditor(container, { width: 128, height: 96, onSave });
        container.querySelector(".field-text").value = "BIG";
        container.querySelector(".field-text").dispatchEvent(new Event("input"));
        const sizeEl = container.querySelector(".field-font-size");
        sizeEl.value = "64";
        sizeEl.dispatchEvent(new Event("input"));

        container.querySelector(".controls").dispatchEvent(new Event("submit"));
        await new Promise((r) => setTimeout(r, 0));
        // Field is "% of width" now — operator typed 64, payload sends pct.
        expect(onSave.mock.calls[0][0].font_size_pct).toBe(64);
    });

    it("sends the user's duration in milliseconds", async () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue({ id: "abc" });

        mountEditor(container, { width: 128, height: 96, onSave });

        container.querySelector(".field-text").value = "Hi";
        container.querySelector(".field-text").dispatchEvent(new Event("input"));
        container.querySelector(".field-duration").value = "12";

        container.querySelector(".controls").dispatchEvent(new Event("submit"));
        await new Promise((r) => setTimeout(r, 0));

        expect(onSave.mock.calls[0][0].duration_ms).toBe(12_000);
    });

    it("disables Save when text is empty", () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        mountEditor(container, { width: 128, height: 96, onSave: vi.fn() });
        const saveBtn = container.querySelector(".field-save");
        expect(saveBtn.disabled).toBe(true);

        const textEl = container.querySelector(".field-text");
        textEl.value = "Hi";
        textEl.dispatchEvent(new Event("input"));
        expect(saveBtn.disabled).toBe(false);
    });

    it("surfaces the error message when onSave rejects", async () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        const onSave = vi.fn().mockRejectedValue(new Error("backend boom"));

        mountEditor(container, { width: 128, height: 96, onSave });

        const textEl = container.querySelector(".field-text");
        textEl.value = "Hi";
        textEl.dispatchEvent(new Event("input"));
        container.querySelector(".controls").dispatchEvent(new Event("submit"));
        await new Promise((r) => setTimeout(r, 0));

        const status = container.querySelector(".editor-status").textContent;
        expect(status).toContain("backend boom");
    });

    it("clicking a preset updates the color inputs and re-renders", () => {
        const fakeCtx = patchCanvasPrototype();
        const container = document.createElement("div");
        mountEditor(container, { width: 64, height: 32, onSave: vi.fn() });

        const renderCallsBefore = fakeCtx.fillRect.mock.calls.length;
        // Pick the second preset (white on red).
        const preset = container.querySelectorAll(".preset")[1];
        preset.click();

        expect(container.querySelector(".field-text-color").value).toBe("#ffffff");
        expect(container.querySelector(".field-bg-color").value).toBe("#cc0000");
        // Re-render happened.
        expect(fakeCtx.fillRect.mock.calls.length).toBeGreaterThan(renderCallsBefore);
    });

    it("sends the user's duration in milliseconds", async () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue({ id: "abc" });

        mountEditor(container, { width: 128, height: 96, onSave });
        container.querySelector(".field-text").value = "Hi";
        container.querySelector(".field-text").dispatchEvent(new Event("input"));
        container.querySelector(".field-duration").value = "12";

        container.querySelector(".controls").dispatchEvent(new Event("submit"));
        await new Promise((r) => setTimeout(r, 0));

        expect(onSave.mock.calls[0][0].duration_ms).toBe(12_000);
    });

    it("Cmd+Enter submits the form when text is non-empty", async () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue({ id: "abc" });

        mountEditor(container, { width: 128, height: 96, onSave });
        const textEl = container.querySelector(".field-text");
        textEl.value = "Hi";
        textEl.dispatchEvent(new Event("input"));

        container.querySelector(".controls").dispatchEvent(
            new KeyboardEvent("keydown", { key: "Enter", metaKey: true, bubbles: true }),
        );
        await new Promise((r) => setTimeout(r, 0));

        expect(onSave).toHaveBeenCalledOnce();
    });

    it("Cmd+Enter does nothing when save is disabled (empty text)", async () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        const onSave = vi.fn();

        mountEditor(container, { width: 128, height: 96, onSave });
        container.querySelector(".controls").dispatchEvent(
            new KeyboardEvent("keydown", { key: "Enter", metaKey: true, bubbles: true }),
        );
        await new Promise((r) => setTimeout(r, 0));

        expect(onSave).not.toHaveBeenCalled();
    });

    it("Escape in the text field clears the text", () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        mountEditor(container, { width: 128, height: 96, onSave: vi.fn() });

        const textEl = container.querySelector(".field-text");
        textEl.value = "Hello";
        textEl.dispatchEvent(new Event("input"));
        textEl.focus();

        textEl.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
        expect(textEl.value).toBe("");
    });

    it("defaults transition to 'cut' with a 500ms transition_ms", async () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue({ id: "abc" });

        mountEditor(container, { width: 128, height: 96, onSave });
        container.querySelector(".field-text").value = "Hi";
        container.querySelector(".field-text").dispatchEvent(new Event("input"));

        container.querySelector(".controls").dispatchEvent(new Event("submit"));
        await new Promise((r) => setTimeout(r, 0));

        const payload = onSave.mock.calls[0][0];
        // Transition fields no longer live on text slides — the playlist
        // carries them as of v3. The editor payload mustn't send them.
        expect(payload.transition).toBeUndefined();
        expect(payload.transition_ms).toBeUndefined();
    });

    it("sends font_family + background_image_slide_id in the payload", async () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue({ id: "abc" });

        mountEditor(container, { width: 128, height: 96, onSave });
        container.querySelector(".field-text").value = "Hi";
        container.querySelector(".field-text").dispatchEvent(new Event("input"));
        container.querySelector(".field-font-family").value = "serif";
        container
            .querySelector(".field-font-family")
            .dispatchEvent(new Event("input"));

        container.querySelector(".controls").dispatchEvent(new Event("submit"));
        await new Promise((r) => setTimeout(r, 0));

        const payload = onSave.mock.calls[0][0];
        expect(payload.font_family).toBe("serif");
        expect(payload.background_image_slide_id).toBeNull();
    });

    it("loadForEdit pre-fills the form + Save dispatches to onSaveExisting", async () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue({ id: "new" });
        const onSaveExisting = vi.fn().mockResolvedValue({ id: "abc" });

        const handle = mountEditor(container, {
            width: 128,
            height: 96,
            onSave,
            onSaveExisting,
        });
        await handle.loadForEdit({
            type: "text_slide",
            id: "abc",
            name: "Promo",
            text: "PROMO",
            text_color: "#ffffff",
            background_color: "#CC0000",
            font_family: "monospace",
            font_size_px: 40,
            duration_ms: 7000,
            auto_mode: null,
        });
        expect(container.querySelector(".field-text").value).toBe("PROMO");
        expect(container.querySelector(".field-name").value).toBe("Promo");
        expect(container.querySelector(".field-font-family").value).toBe(
            "monospace",
        );
        expect(container.querySelector(".field-duration").value).toBe("7");

        container.querySelector(".controls").dispatchEvent(new Event("submit"));
        await new Promise((r) => setTimeout(r, 0));
        expect(onSaveExisting).toHaveBeenCalledTimes(1);
        expect(onSave).not.toHaveBeenCalled();
        expect(onSaveExisting.mock.calls[0][0]).toBe("abc");
    });

    it("Generate button is hidden until background source = 'slide' + wired", async () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        mountEditor(container, {
            width: 128,
            height: 96,
            onSave: vi.fn(),
            fetchItems: async () => [],
            onGenerateBackground: vi.fn(),
        });
        // Solid-color default: generator hidden.
        expect(container.querySelector(".editor-bg-generate").hidden).toBe(true);
        // Switch bg source to "slide" → generator surfaces.
        const slideRadio = container.querySelector('.field-bg-source[value="slide"]');
        slideRadio.checked = true;
        slideRadio.dispatchEvent(new Event("change"));
        await new Promise((r) => setTimeout(r, 0));
        expect(container.querySelector(".editor-bg-generate").hidden).toBe(false);
    });

    it("Generate button calls onGenerateBackground and selects the returned slide", async () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        const onGenerateBackground = vi.fn().mockResolvedValue({
            id: "new-bg",
            name: "Background — sunset",
        });
        mountEditor(container, {
            width: 128,
            height: 96,
            onSave: vi.fn(),
            fetchItems: async () => [
                { id: "new-bg", name: "Background — sunset" },
            ],
            onGenerateBackground,
        });
        // Switch to slide mode.
        const slideRadio = container.querySelector('.field-bg-source[value="slide"]');
        slideRadio.checked = true;
        slideRadio.dispatchEvent(new Event("change"));
        await new Promise((r) => setTimeout(r, 0));

        container.querySelector(".field-bg-generate-prompt").value = "sunset";
        container.querySelector(".bg-generate-btn").click();
        await new Promise((r) => setTimeout(r, 0));
        await new Promise((r) => setTimeout(r, 0));

        expect(onGenerateBackground).toHaveBeenCalledWith({ prompt: "sunset" });
        expect(container.querySelector(".field-bg-slide").value).toBe("new-bg");
    });

    it("Generate button with empty prompt surfaces a status line without calling the hook", async () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        const onGenerateBackground = vi.fn();
        mountEditor(container, {
            width: 128,
            height: 96,
            onSave: vi.fn(),
            fetchItems: async () => [],
            onGenerateBackground,
        });
        const slideRadio = container.querySelector('.field-bg-source[value="slide"]');
        slideRadio.checked = true;
        slideRadio.dispatchEvent(new Event("change"));
        await new Promise((r) => setTimeout(r, 0));

        container.querySelector(".bg-generate-btn").click();
        await new Promise((r) => setTimeout(r, 0));
        expect(onGenerateBackground).not.toHaveBeenCalled();
        expect(container.querySelector(".bg-generate-status").textContent).toMatch(
            /prompt first/i,
        );
    });

    // New-slide button + Editing-mode label removed — the slide browser
    // at the top of each subpage has the "+" tile for that flow.
});
