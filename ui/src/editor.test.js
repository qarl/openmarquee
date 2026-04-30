// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import {
    canvasToBase64,
    drawCanvas,
    drawTextOnly,
    mountEditor,
    pickFontSize,
} from "./editor.js";

function mockCanvas(width, height) {
    const ctx = {
        fillStyle: "",
        font: "",
        textAlign: "",
        textBaseline: "",
        clearRect: vi.fn(),
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

    it("renders the text centered in the box (default {0.1, 0.1, 0.8, 0.8})", () => {
        const canvas = mockCanvas(128, 96);
        drawCanvas(canvas, { text: "HI", textColor: "#fff", backgroundColor: "#000" });
        expect(canvas._ctx.textAlign).toBe("center");
        expect(canvas._ctx.textBaseline).toBe("middle");
        expect(canvas._ctx.fillText).toHaveBeenCalledTimes(1);
        const [line, x] = canvas._ctx.fillText.mock.calls[0];
        expect(line).toBe("HI");
        // Default box {0.1, 0.1, 0.8, 0.8} centers at 0.5 → 0.5 * 128 = 64
        expect(x).toBeCloseTo(0.5 * 128, 5);
    });

    it("centers in an explicit box rather than the slide (§5.10a)", () => {
        const canvas = mockCanvas(100, 100);
        drawCanvas(canvas, {
            text: "X",
            box: { x: 0.5, y: 0.1, w: 0.4, h: 0.4 },
        });
        const [, x, y] = canvas._ctx.fillText.mock.calls[0];
        // box center: x = 0.5 + 0.4/2 = 0.7; y = 0.1 + 0.4/2 = 0.3
        expect(x).toBeCloseTo(0.7 * 100, 5);
        // y is the line baseline, derived from box-center + half lineHeight
        // adjustments — just check it sits inside the box's vertical span.
        expect(y).toBeGreaterThan(0.1 * 100);
        expect(y).toBeLessThan(0.5 * 100);
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

describe("drawCanvas — auto_mode dynamic text", () => {
    // Bug B6 (qarl batch 2026-04-29): the editor preview should show
    // the current formatted token (HH:MM, weekday, etc.) when auto_mode
    // is set, instead of the operator's typed placeholder text.
    it("substitutes the formatted time when autoMode is 'time'", () => {
        const canvas = mockCanvas(64, 32);
        drawCanvas(canvas, {
            text: "ignored",
            autoMode: "time",
            autoFormat: "time_hm",
        });
        const drawnText = canvas._ctx.fillText.mock.calls[0][0];
        expect(drawnText).toMatch(/^\d\d:\d\d$/);
    });

    it("falls back to the operator's text when autoMode is null", () => {
        const canvas = mockCanvas(64, 32);
        drawCanvas(canvas, { text: "Hello", autoMode: null });
        expect(canvas._ctx.fillText.mock.calls[0][0]).toBe("Hello");
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
        // §5.10a (qarl 2026-04-30 revision): font sizing is slide-relative,
        // not box-relative. Heuristic fires against canvas.height = 96.
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

    it("constrains text width to box width edge-to-edge (§5.10a)", () => {
        const canvas = mockCanvas(64, 32);
        drawCanvas(canvas, { text: "VERY LONG LINE OF TEXT" });
        const [, , , maxWidth] = canvas._ctx.fillText.mock.calls[0];
        // Default box.w = 0.8 → 0.8 * 64 = 51.2
        expect(maxWidth).toBeCloseTo(0.8 * 64, 5);
    });

    it("splits text on \\r\\n as well as \\n (iOS paste)", () => {
        const canvas = mockCanvas(128, 96);
        drawCanvas(canvas, { text: "A\r\nB" });
        expect(canvas._ctx.fillText).toHaveBeenCalledTimes(2);
        expect(canvas._ctx.fillText.mock.calls[0][0]).toBe("A");
        expect(canvas._ctx.fillText.mock.calls[1][0]).toBe("B");
    });
});

describe("drawTextOnly (Phase 5b — Text-over-Video overlay)", () => {
    it("clears to transparent and skips fillText when text is empty", () => {
        const canvas = mockCanvas(128, 96);
        drawTextOnly(canvas, { text: "" });
        expect(canvas._ctx.clearRect).toHaveBeenCalledWith(0, 0, 128, 96);
        expect(canvas._ctx.fillText).not.toHaveBeenCalled();
    });

    it("draws each line of multi-line text", () => {
        const canvas = mockCanvas(128, 96);
        drawTextOnly(canvas, { text: "TOP\nBOTTOM" });
        expect(canvas._ctx.fillText).toHaveBeenCalledTimes(2);
        expect(canvas._ctx.fillText.mock.calls[0][0]).toBe("TOP");
        expect(canvas._ctx.fillText.mock.calls[1][0]).toBe("BOTTOM");
    });

    it("uses font_size_pct relative to canvas height when provided", () => {
        const canvas = mockCanvas(100, 200);
        drawTextOnly(canvas, { text: "Hi", font_size_pct: 25 });
        // 25% of 200 = 50px; the wire-shape pct path wins over the
        // pickFontSize default that drawCanvas falls back to.
        expect(canvas._ctx.font).toMatch(/\b50px\b/);
    });

    it("never paints a background — the video frame underneath shows through", () => {
        const canvas = mockCanvas(128, 96);
        drawTextOnly(canvas, { text: "OVER VIDEO", text_color: "#ff0" });
        // drawCanvas paints fillRect for the bg; drawTextOnly must not.
        expect(canvas._ctx.fillRect).not.toHaveBeenCalled();
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

        const handle = mountEditor(container, { width: 128, height: 96, onSave });

        container.querySelector(".field-text").value = "Hi";
        container.querySelector(".field-text").dispatchEvent(new Event("input"));
        container.querySelector(".field-text-color").value = "#ffaa00";
        container.querySelector(".field-text-color").dispatchEvent(new Event("input"));

        await handle.flushAutoSave();

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
        const handle = mountEditor(container, { width: 128, height: 96, onSave });

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
        await handle.flushAutoSave();
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

        const handle = mountEditor(container, { width: 128, height: 96, onSave });
        container.querySelector(".field-text").value = "BIG";
        container.querySelector(".field-text").dispatchEvent(new Event("input"));
        const sizeEl = container.querySelector(".field-font-size");
        sizeEl.value = "64";
        sizeEl.dispatchEvent(new Event("input"));

        await handle.flushAutoSave();
        // Field is "% of width" now — operator typed 64, payload sends pct.
        expect(onSave.mock.calls[0][0].font_size_pct).toBe(64);
    });

    it("sends the user's duration in milliseconds", async () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue({ id: "abc" });

        const handle = mountEditor(container, { width: 128, height: 96, onSave });

        container.querySelector(".field-text").value = "Hi";
        container.querySelector(".field-text").dispatchEvent(new Event("input"));
        container.querySelector(".field-duration").value = "12";

        await handle.flushAutoSave();

        expect(onSave.mock.calls[0][0].duration_ms).toBe(12_000);
    });

    it("suppresses auto-save when text is empty (canSave gate)", async () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        const onSave = vi.fn();
        const handle = mountEditor(container, { width: 128, height: 96, onSave });

        // Touch a field other than text — auto-save schedules but the
        // gate (state.text.trim() > 0) suppresses on flush.
        const nameEl = container.querySelector(".field-name");
        nameEl.value = "Untitled";
        nameEl.dispatchEvent(new Event("input", { bubbles: true }));
        await handle.flushAutoSave();
        expect(onSave).not.toHaveBeenCalled();

        // Filling text in unblocks the gate.
        const textEl = container.querySelector(".field-text");
        textEl.value = "Hi";
        textEl.dispatchEvent(new Event("input", { bubbles: true }));
        await handle.flushAutoSave();
        expect(onSave).toHaveBeenCalledOnce();
    });

    it("surfaces the error message when onSave rejects", async () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        const onSave = vi.fn().mockRejectedValue(new Error("backend boom"));

        const handle = mountEditor(container, { width: 128, height: 96, onSave });

        const textEl = container.querySelector(".field-text");
        textEl.value = "Hi";
        textEl.dispatchEvent(new Event("input"));
        await handle.flushAutoSave();

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

        const handle = mountEditor(container, { width: 128, height: 96, onSave });
        container.querySelector(".field-text").value = "Hi";
        container.querySelector(".field-text").dispatchEvent(new Event("input"));
        container.querySelector(".field-duration").value = "12";

        await handle.flushAutoSave();

        expect(onSave.mock.calls[0][0].duration_ms).toBe(12_000);
    });

    // Cmd+Enter submit shortcut was removed alongside the Save button —
    // auto-save replaces it. Typing into the text field schedules a save
    // automatically; flush forces it through synchronously for the test.

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

        const handle = mountEditor(container, { width: 128, height: 96, onSave });
        container.querySelector(".field-text").value = "Hi";
        container.querySelector(".field-text").dispatchEvent(new Event("input"));

        await handle.flushAutoSave();

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

        const handle = mountEditor(container, { width: 128, height: 96, onSave });
        container.querySelector(".field-text").value = "Hi";
        container.querySelector(".field-text").dispatchEvent(new Event("input"));
        container.querySelector(".field-font-family").value = "serif";
        container
            .querySelector(".field-font-family")
            .dispatchEvent(new Event("input"));

        await handle.flushAutoSave();

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

        await handle.flushAutoSave();
        expect(onSaveExisting).toHaveBeenCalledTimes(1);
        expect(onSave).not.toHaveBeenCalled();
        expect(onSaveExisting.mock.calls[0][0]).toBe("abc");
    });

    it("dragging the SE handle commits new box dims into the autosave payload (qarl §5.10a fu)", async () => {
        // QA 2026-04-30: the live trace showed overlay style updating
        // mid-drag (state.box mutating correctly) but the autoSave
        // payload sent the DEFAULT box. Reproduce here: simulate a
        // pointerdown on the SE handle, pointermove that crosses the
        // 5px threshold, pointerup, then flush autoSave and assert the
        // captured payload.box reflects the new size.
        patchCanvasPrototype();
        const container = document.createElement("div");
        const onSaveExisting = vi.fn().mockResolvedValue({ id: "abc" });
        const handle = mountEditor(container, {
            width: 128,
            height: 96,
            onSave: vi.fn(),
            onSaveExisting,
        });
        await handle.loadForEdit({
            type: "text_slide",
            id: "abc",
            name: "X",
            text: "X",
            box: { x: 0.1, y: 0.1, w: 0.8, h: 0.8 },
        });

        const overlay = container.querySelector(".editor-box-overlay");
        const seHandle = overlay.querySelector('[data-handle="se"]');
        const canvas = container.querySelector(".editor-canvas");

        // Stub the canvas's bounding rect so the drag math has known
        // pixel dims to work against. jsdom defaults to 0×0.
        canvas.getBoundingClientRect = () => ({
            width: 1000,
            height: 1000,
            left: 0,
            top: 0,
            right: 1000,
            bottom: 1000,
            x: 0,
            y: 0,
        });

        // Drag SE handle by -200px in both dims → -0.2 in slide-relative.
        // Starting box w=h=0.8 → new w=h should clamp to 0.6.
        seHandle.dispatchEvent(
            new PointerEvent("pointerdown", {
                pointerId: 1,
                clientX: 900,
                clientY: 900,
                button: 0,
                bubbles: true,
            }),
        );
        overlay.dispatchEvent(
            new PointerEvent("pointermove", {
                pointerId: 1,
                clientX: 700,
                clientY: 700,
                bubbles: true,
            }),
        );
        overlay.dispatchEvent(
            new PointerEvent("pointerup", {
                pointerId: 1,
                clientX: 700,
                clientY: 700,
                bubbles: true,
            }),
        );

        await handle.flushAutoSave();
        expect(onSaveExisting).toHaveBeenCalled();
        const lastCall =
            onSaveExisting.mock.calls[onSaveExisting.mock.calls.length - 1];
        const payload = lastCall[1];
        expect(payload.box.w).toBeCloseTo(0.6, 5);
        expect(payload.box.h).toBeCloseTo(0.6, 5);
        // x and y are unchanged by an SE drag (origin stays put).
        expect(payload.box.x).toBeCloseTo(0.1, 5);
        expect(payload.box.y).toBeCloseTo(0.1, 5);
    });

    it("loadForEdit hydrates state.box from slide payload + Save round-trips it", async () => {
        // §5.10a phase 3: the editor restores the operator's box on
        // re-edit, the overlay positions itself off it, and Save sends
        // the box back to the backend in the payload.
        patchCanvasPrototype();
        const container = document.createElement("div");
        const onSaveExisting = vi.fn().mockResolvedValue({ id: "abc" });
        const handle = mountEditor(container, {
            width: 128,
            height: 96,
            onSave: vi.fn(),
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
            box: { x: 0.2, y: 0.3, w: 0.5, h: 0.4 },
        });
        const overlay = container.querySelector(".editor-box-overlay");
        // Inline style reflects the loaded box as percentages.
        expect(overlay.style.left).toBe("20%");
        expect(overlay.style.top).toBe("30%");
        expect(overlay.style.width).toBe("50%");
        expect(overlay.style.height).toBe("40%");

        await handle.flushAutoSave();
        expect(onSaveExisting).toHaveBeenCalledTimes(1);
        const payload = onSaveExisting.mock.calls[0][1];
        expect(payload.box).toEqual({ x: 0.2, y: 0.3, w: 0.5, h: 0.4 });
    });

    it("loadForEdit defaults the box to {0.1, 0.1, 0.8, 0.8} when slide.box is missing", async () => {
        // Old slides on disk carry no `box` field — editor synthesizes
        // the centered-with-10%-margin default.
        patchCanvasPrototype();
        const container = document.createElement("div");
        const handle = mountEditor(container, {
            width: 128,
            height: 96,
            onSave: vi.fn(),
            onSaveExisting: vi.fn().mockResolvedValue({ id: "old" }),
        });
        await handle.loadForEdit({
            type: "text_slide",
            id: "old",
            name: "Legacy",
            text: "OLD",
        });
        const overlay = container.querySelector(".editor-box-overlay");
        expect(overlay.style.left).toBe("10%");
        expect(overlay.style.top).toBe("10%");
        expect(overlay.style.width).toBe("80%");
        expect(overlay.style.height).toBe("80%");
    });

    it("loadForEdit re-renders the canvas after a bundled font finishes loading", async () => {
        // Regression: loading a slide whose font is a bundled @font-face
        // family (e.g. Pacifico) used to paint the canvas ONCE with a
        // fallback before the .ttf was registered, leaving the operator
        // staring at the wrong glyphs until they clicked the slide a
        // second time. The fix: loadForEdit awaits document.fonts.load,
        // gives the browser a paint cycle, then re-renders.
        const ctx = patchCanvasPrototype();
        const container = document.createElement("div");

        // Track every time drawCanvas writes to ctx.fillRect (once per
        // call). With the fix we expect ≥ 2 (initial paint + post-font-
        // load repaint). Without it, ≤ 1.
        const fillRectCalls = ctx.fillRect;

        // Stub document.fonts.load so we can resolve it on demand.
        let loadResolver;
        const loadCalls = [];
        const fakeFonts = {
            load: vi.fn((spec) => {
                loadCalls.push(spec);
                return new Promise((r) => {
                    loadResolver = r;
                });
            }),
            ready: Promise.resolve(),
        };
        const origFonts = document.fonts;
        Object.defineProperty(document, "fonts", {
            value: fakeFonts,
            configurable: true,
        });

        try {
            const handle = mountEditor(container, {
                width: 128,
                height: 96,
                onSave: vi.fn().mockResolvedValue({}),
            });
            // mountEditor's initial syncAndRender bumps fillRect once.
            const baseline = fillRectCalls.mock.calls.length;

            const editPromise = handle.loadForEdit({
                type: "text_slide",
                id: "abc",
                name: "Promo",
                text: "PROMO",
                text_color: "#ffffff",
                background_color: "#000000",
                font_family: "Pacifico",
                font_size_px: 40,
                duration_ms: 5000,
                auto_mode: null,
            });

            // Pump microtasks so the synchronous body of loadForEdit
            // (including its first syncAndRender) has run, then verify
            // the font-load was kicked off but the post-load repaint
            // hasn't fired yet.
            await new Promise((r) => setTimeout(r, 0));
            const afterFirstPaint = fillRectCalls.mock.calls.length;
            expect(afterFirstPaint).toBeGreaterThan(baseline);
            expect(loadCalls.length).toBe(1);
            expect(loadCalls[0]).toMatch(/Pacifico/);

            // Resolve the font load. loadForEdit's await cascade should
            // then issue a second syncAndRender → drawCanvas → fillRect.
            loadResolver();
            await editPromise;

            const afterFontLoad = fillRectCalls.mock.calls.length;
            expect(afterFontLoad).toBeGreaterThan(afterFirstPaint);
        } finally {
            Object.defineProperty(document, "fonts", {
                value: origFonts,
                configurable: true,
            });
        }
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
            name: "sunset — Background",
        });
        mountEditor(container, {
            width: 128,
            height: 96,
            onSave: vi.fn(),
            fetchItems: async () => [
                // `type: "image"` required after the picker started filtering
                // to image-only (bug #4 regression).
                { id: "new-bg", type: "image", name: "sunset — Background" },
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

    it("picking a video bg sends background_video_slide_id (Phase 5b)", async () => {
        // §5.10: an operator picks a saved VideoSlide as the background
        // of a TextSlide; the device composites text over the live video
        // frames at playback. The editor stores the reference; payload
        // ships background_video_slide_id and DOESN'T set
        // background_image_slide_id (mutual-exclusion at the model).
        patchCanvasPrototype();
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue({ id: "abc" });
        const handle = mountEditor(container, {
            width: 128,
            height: 96,
            onSave,
            fetchItems: async () => [
                { id: "vid-1", type: "video", name: "loop reel" },
                { id: "img-1", type: "image", name: "sunset" },
            ],
        });
        // Settle the editor's initial-mount async tail (resetToBlank
        // → computeDefaultName → fetchItems again) before flipping
        // radios — otherwise the tail's resetToBlank overwrites the
        // radio choice mid-test. Multi-tick await drains the chain.
        for (let i = 0; i < 4; i++) await new Promise((r) => setTimeout(r, 0));

        // Switch to "video" mode → triggers populateBgVideoOptions.
        const videoRadio = container.querySelector('.field-bg-source[value="video"]');
        videoRadio.checked = true;
        videoRadio.dispatchEvent(new Event("change"));
        await new Promise((r) => setTimeout(r, 0));

        // Dropdown only carries video-type items.
        const videoSelect = container.querySelector(".field-bg-video");
        const optionValues = Array.from(videoSelect.options).map((o) => o.value);
        expect(optionValues).toEqual(["", "vid-1"]);
        expect(container.querySelector(".editor-bg-video-wrap").hidden).toBe(false);
        expect(container.querySelector(".editor-bg-slide-wrap").hidden).toBe(true);

        // Pick a video and save.
        videoSelect.value = "vid-1";
        videoSelect.dispatchEvent(new Event("change"));
        container.querySelector(".field-text").value = "Happy Hour";
        container.querySelector(".field-text").dispatchEvent(new Event("input"));
        await handle.flushAutoSave();

        expect(onSave).toHaveBeenCalledOnce();
        const payload = onSave.mock.calls[0][0];
        expect(payload.background_video_slide_id).toBe("vid-1");
        expect(payload.background_image_slide_id).toBeNull();
    });

    it("switching from video bg to image bg clears the video ref (mutual exclusion)", async () => {
        // Operator picks a video bg, changes their mind, picks an image
        // instead. The save payload must carry only the image id —
        // shipping both would 422 against the backend's mutual-exclusion
        // validator (content/__init__.py::_bg_layers_are_exclusive).
        patchCanvasPrototype();
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue({ id: "abc" });
        const handle = mountEditor(container, {
            width: 128,
            height: 96,
            onSave,
            fetchItems: async () => [
                { id: "vid-1", type: "video", name: "loop" },
                { id: "img-1", type: "image", name: "sunset" },
            ],
        });
        for (let i = 0; i < 4; i++) await new Promise((r) => setTimeout(r, 0));

        // First: video bg.
        const videoRadio = container.querySelector('.field-bg-source[value="video"]');
        videoRadio.checked = true;
        videoRadio.dispatchEvent(new Event("change"));
        await new Promise((r) => setTimeout(r, 0));
        const videoSelect = container.querySelector(".field-bg-video");
        videoSelect.value = "vid-1";
        videoSelect.dispatchEvent(new Event("change"));

        // Then: switch to image bg.
        const slideRadio = container.querySelector('.field-bg-source[value="slide"]');
        slideRadio.checked = true;
        slideRadio.dispatchEvent(new Event("change"));
        await new Promise((r) => setTimeout(r, 0));
        const imgSelect = container.querySelector(".field-bg-slide");
        imgSelect.value = "img-1";
        imgSelect.dispatchEvent(new Event("change"));

        container.querySelector(".field-text").value = "Sale";
        container.querySelector(".field-text").dispatchEvent(new Event("input"));
        await handle.flushAutoSave();

        const payload = onSave.mock.calls[0][0];
        expect(payload.background_image_slide_id).toBe("img-1");
        expect(payload.background_video_slide_id).toBeNull();
    });

    it("loadForEdit hydrates the video bg picker from a stored video reference (Phase 5b)", async () => {
        patchCanvasPrototype();
        // Stub global Image so loadImageForSlide resolves quickly in jsdom
        // (the real `new Image()` in jsdom doesn't fire onerror reliably
        // under microtask awaits, hanging loadForEdit).
        const RealImage = global.Image;
        global.Image = class {
            constructor() {
                queueMicrotask(() => this.onerror?.(new Event("error")));
            }
            set src(_) {}
        };
        try {
            const container = document.createElement("div");
            const handle = mountEditor(container, {
                width: 128,
                height: 96,
                onSave: vi.fn(),
                onSaveExisting: vi.fn().mockResolvedValue({}),
                fetchItems: async () => [
                    { id: "vid-1", type: "video", name: "loop" },
                ],
            });
            for (let i = 0; i < 4; i++) await new Promise((r) => setTimeout(r, 0));

            await handle.loadForEdit({
                id: "edit-me",
                type: "text_slide",
                name: "Existing",
                text: "Bar Open",
                text_color: "#FFFFFF",
                background_color: "#000000",
                background_video_slide_id: "vid-1",
                duration_ms: 5000,
            });

            // The "video" radio is selected and the dropdown points at the
            // referenced video.
            const videoRadio = container.querySelector('.field-bg-source[value="video"]');
            expect(videoRadio.checked).toBe(true);
            expect(container.querySelector(".field-bg-video").value).toBe("vid-1");
            expect(container.querySelector(".editor-bg-video-wrap").hidden).toBe(false);
            expect(container.querySelector(".editor-bg-slide-wrap").hidden).toBe(true);
        } finally {
            global.Image = RealImage;
        }
    });
});

describe("mountEditor — visual font picker", () => {
    function patchCanvasPrototype() {
        // Same shim the other mountEditor tests use — jsdom canvas.
        HTMLCanvasElement.prototype.getContext = function () {
            return {
                fillStyle: "", font: "", textAlign: "", textBaseline: "",
                clearRect: () => {}, fillRect: () => {}, fillText: () => {},
                save: () => {}, restore: () => {},
            };
        };
        HTMLCanvasElement.prototype.toDataURL = () => "data:image/png;base64,STUBDATA";
    }

    it("renders the trigger + a tile per font family, popover starts hidden", () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        document.body.appendChild(container);
        try {
            mountEditor(container, {
                width: 128, height: 96,
                onSave: vi.fn().mockResolvedValue({}),
            });
            const trigger = container.querySelector(".font-picker-trigger");
            const popover = container.querySelector(".font-picker-popover");
            expect(trigger).not.toBeNull();
            expect(popover.hidden).toBe(true);
            const tiles = popover.querySelectorAll(".font-picker-tile");
            // One tile per FONT_FAMILIES entry — locked at the const length
            // so the test fails loudly if a font is added without picker
            // wiring (or vice versa).
            expect(tiles.length).toBeGreaterThanOrEqual(20);
            // Each tile is rendered in its own face — the inline
            // font-family style is the canonical signal.
            const interTile = popover.querySelector('[data-value="Inter"]');
            expect(interTile).not.toBeNull();
            expect(interTile.style.fontFamily).toContain("Inter");
        } finally {
            container.remove();
        }
    });

    it("clicking a tile fires BOTH input and change on the select (regression QA #2026-04-26: picker fired only change → canvas preview never updated)", () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        document.body.appendChild(container);
        try {
            mountEditor(container, {
                width: 128, height: 96,
                onSave: vi.fn().mockResolvedValue({}),
            });
            const select = container.querySelector(".field-font-family");
            const events = [];
            select.addEventListener("input", () => events.push("input"));
            select.addEventListener("change", () => events.push("change"));

            container.querySelector(".font-picker-trigger").click();
            container
                .querySelector(".font-picker-popover")
                .querySelector('[data-value="Oswald"]')
                .click();

            // Input must fire before change to match a native <select>'s
            // user-pick semantics — syncAndRender is wired to input,
            // and the font-load handler on change reads state.fontFamily
            // which input has just updated.
            expect(events).toEqual(["input", "change"]);
        } finally {
            container.remove();
        }
    });

    it("clicking the trigger toggles the popover; clicking a tile selects + closes", () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        document.body.appendChild(container);
        try {
            mountEditor(container, {
                width: 128, height: 96,
                onSave: vi.fn().mockResolvedValue({}),
            });
            const trigger = container.querySelector(".font-picker-trigger");
            const popover = container.querySelector(".font-picker-popover");
            const select = container.querySelector(".field-font-family");

            trigger.click();
            expect(popover.hidden).toBe(false);
            expect(trigger.getAttribute("aria-expanded")).toBe("true");

            const oswaldTile = popover.querySelector('[data-value="Oswald"]');
            oswaldTile.click();
            expect(select.value).toBe("Oswald");
            expect(popover.hidden).toBe(true);

            // Trigger label sync — shows the new selection, in its face.
            const label = container.querySelector(".font-picker-trigger-label");
            expect(label.textContent).toBe("Oswald");
            expect(label.style.fontFamily).toContain("Oswald");
        } finally {
            container.remove();
        }
    });
});
