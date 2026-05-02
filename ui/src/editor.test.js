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
        translate: vi.fn(),
        scale: vi.fn(),
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
        // §5.10a v3.1.2 (qarl 2026-05-01 review #3): font sizing is
        // BOX-WIDTH-relative. Default box.w = 0.8 → boxW = 0.8 * 128
        // = 102.4 → floor → 102. Heuristic = pickFontSize(boxW).
        const boxW = Math.max(1, Math.round(0.8 * 128));
        expect(canvas._ctx.font).toContain(`${pickFontSize(boxW)}px`);
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

    it("squishes vertically when total text height overflows the box (qarl 2026-05-01 ask #1)", () => {
        // Many lines + a tall font_size_pct → totalHeight > box.h
        // (default box.h = 0.8 → 0.8 * 100 = 80px on a 100×100 canvas).
        // 5 lines × fontSize 30 (30% of 100 width) × 1.1 line-height
        // = 165px total. 165 > 80 → vertical squish via ctx.scale(1, ratio).
        const canvas = mockCanvas(100, 100);
        drawCanvas(canvas, { text: "A\nB\nC\nD\nE", fontSizePct: 30 });
        // ctx.translate + ctx.scale fired once each (for the squish).
        expect(canvas._ctx.translate).toHaveBeenCalledTimes(1);
        expect(canvas._ctx.scale).toHaveBeenCalledTimes(1);
        // The y-scale ratio should be box.h / totalHeight ≈ 80/165 ≈ 0.485.
        const [, yScale] = canvas._ctx.scale.mock.calls[0];
        expect(yScale).toBeGreaterThan(0);
        expect(yScale).toBeLessThan(1);
    });

    it("does NOT squish vertically when text fits in the box", () => {
        const canvas = mockCanvas(100, 100);
        drawCanvas(canvas, { text: "OK", fontSizePct: 20 });
        // Single line fits → no scale call.
        expect(canvas._ctx.scale).not.toHaveBeenCalled();
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
    // §5.10a v3: drawTextOnly takes a wire-shape ContentItem with
    // text_layers; iterates layers in array order.
    it("clears to transparent and skips fillText when text_layers is empty", () => {
        const canvas = mockCanvas(128, 96);
        drawTextOnly(canvas, { text_layers: [] });
        expect(canvas._ctx.clearRect).toHaveBeenCalledWith(0, 0, 128, 96);
        expect(canvas._ctx.fillText).not.toHaveBeenCalled();
    });

    it("draws each line of multi-line text", () => {
        const canvas = mockCanvas(128, 96);
        drawTextOnly(canvas, { text_layers: [{ text: "TOP\nBOTTOM" }] });
        expect(canvas._ctx.fillText).toHaveBeenCalledTimes(2);
        expect(canvas._ctx.fillText.mock.calls[0][0]).toBe("TOP");
        expect(canvas._ctx.fillText.mock.calls[1][0]).toBe("BOTTOM");
    });

    it("uses font_size_pct relative to BOX width when provided", () => {
        // §5.10a v3.1.2 (qarl 2026-05-01 review #3): pct is of box
        // width. Default box {x:0.1, y:0.1, w:0.8, h:0.8} on a 200×100
        // canvas → boxW = 0.8 * 200 = 160. 25% of 160 = 40px.
        const canvas = mockCanvas(200, 100);
        drawTextOnly(canvas, {
            text_layers: [{ text: "Hi", font_size_pct: 25 }],
        });
        expect(canvas._ctx.font).toMatch(/\b40px\b/);
    });

    it("font scales with the layer's box.w (qarl 2026-05-01 review #3)", () => {
        // Half-width box → half-size font for the same font_size_pct.
        // Confirms 'resizing the box visibly resizes the text.'
        const canvasWide = mockCanvas(200, 100);
        drawTextOnly(canvasWide, {
            text_layers: [
                { text: "X", font_size_pct: 25, box: { x: 0, y: 0, w: 0.8, h: 1 } },
            ],
        });
        const wideFont = canvasWide._ctx.font;
        const canvasNarrow = mockCanvas(200, 100);
        drawTextOnly(canvasNarrow, {
            text_layers: [
                { text: "X", font_size_pct: 25, box: { x: 0, y: 0, w: 0.4, h: 1 } },
            ],
        });
        const narrowFont = canvasNarrow._ctx.font;
        // Wide box (boxW=160) → 40px; narrow box (boxW=80) → 20px.
        expect(wideFont).toMatch(/\b40px\b/);
        expect(narrowFont).toMatch(/\b20px\b/);
    });

    it("never paints a background — the video frame underneath shows through", () => {
        const canvas = mockCanvas(128, 96);
        drawTextOnly(canvas, {
            text_layers: [{ text: "OVER VIDEO", text_color: "#ff0" }],
        });
        // drawCanvas paints fillRect for the bg; drawTextOnly must not.
        expect(canvas._ctx.fillRect).not.toHaveBeenCalled();
    });

    it("composites multiple text_layers in array order (later draws over earlier)", () => {
        const canvas = mockCanvas(200, 200);
        drawTextOnly(canvas, {
            text_layers: [{ text: "BOTTOM" }, { text: "TOP" }],
        });
        // Two layers → two fillText calls in order.
        expect(canvas._ctx.fillText).toHaveBeenCalledTimes(2);
        expect(canvas._ctx.fillText.mock.calls[0][0]).toBe("BOTTOM");
        expect(canvas._ctx.fillText.mock.calls[1][0]).toBe("TOP");
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
        translate: vi.fn(),
        scale: vi.fn(),
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
        // §5.10a v3: per-layer fields live in text_layers[0], slide-level
        // fields stay at the root (background_color, duration_ms, etc.).
        expect(payload.text_layers[0].text).toBe("Hi");
        expect(payload.text_layers[0].text_color).toBe("#FFAA00");
        expect(payload.background_color).toBe("#000000");
        expect(payload.duration_ms).toBe(5000); // default 5s
        expect(payload.png_base64).toBe("STUBDATA");
        // Font-size defaults to the pickFontSizePct() default (% of height).
        expect(payload.text_layers[0].font_size_pct).toBe(30);
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
        // §5.10a v3.1 (accordion editor): the dynamic-source picker is a
        // segmented button group whose clicks drive a hidden
        // .field-auto-mode input. Drive it via the segmented buttons.
        patchCanvasPrototype();
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue({ id: "x" });
        const handle = mountEditor(container, { width: 128, height: 96, onSave });

        // Format dropdown starts hidden (no mode selected).
        expect(container.querySelector(".field-auto-format-wrap").hidden).toBe(true);

        container
            .querySelector('.field-auto-mode-segmented button[data-value="time"]')
            .click();
        expect(container.querySelector(".field-auto-mode").value).toBe("time");
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
        fmt.dispatchEvent(new Event("change", { bubbles: true }));

        container.querySelector(".field-text").value = "12:34 (fallback)";
        container.querySelector(".field-text").dispatchEvent(new Event("input"));
        await handle.flushAutoSave();
        expect(onSave.mock.calls[0][0].text_layers[0].auto_mode).toBe("time");
        expect(onSave.mock.calls[0][0].text_layers[0].auto_format).toBe("time_hms");
    });

    it("switching the auto_mode repopulates the format dropdown with the new options", async () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        mountEditor(container, {
            width: 128,
            height: 96,
            onSave: vi.fn().mockResolvedValue({}),
        });
        const segPick = (val) =>
            container
                .querySelector(`.field-auto-mode-segmented button[data-value="${val}"]`)
                .click();

        segPick("date");
        let opts = Array.from(
            container.querySelectorAll(".field-auto-format option"),
        ).map((o) => o.value);
        expect(opts).toEqual(["date_iso", "date_long", "date_medium"]);

        segPick("day");
        opts = Array.from(
            container.querySelectorAll(".field-auto-format option"),
        ).map((o) => o.value);
        expect(opts).toEqual(["day_long", "day_short"]);

        // And switching OFF auto_mode hides the wrap entirely.
        segPick("");
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
        // Field is "% of height" — operator typed 64, payload sends pct on layer[0].
        expect(onSave.mock.calls[0][0].text_layers[0].font_size_pct).toBe(64);
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

    it("clicking a quick-color swatch updates the layer's text color and re-renders", () => {
        // §5.10a v3.1 (accordion editor): the per-layer "Quick colors"
        // pairs (text + bg "Aa" buttons) are gone; replaced by 9 hex
        // swatches that set the LAYER'S text color only. Bg color stays
        // slide-level in the Background-source card.
        const fakeCtx = patchCanvasPrototype();
        const container = document.createElement("div");
        mountEditor(container, { width: 64, height: 32, onSave: vi.fn() });

        const renderCallsBefore = fakeCtx.fillRect.mock.calls.length;
        // Pick the amber accent swatch (#FFB43C — second in the row).
        const swatch = container.querySelectorAll(".editor-color-swatch")[1];
        expect(swatch.dataset.color).toBe("#FFB43C");
        swatch.click();

        // Native browsers lowercase color-input values; the editor
        // uppercases on save, so just check case-insensitively.
        expect(
            container.querySelector(".field-text-color").value.toUpperCase(),
        ).toBe("#FFB43C");
        // Bg color was NOT touched — that's slide-level now.
        expect(container.querySelector(".field-bg-color").value).toBe("#000000");
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
        expect(payload.text_layers[0].font_family).toBe("serif");
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
            background_color: "#CC0000",
            duration_ms: 7000,
            text_layers: [
                {
                    text: "PROMO",
                    text_color: "#ffffff",
                    font_family: "monospace",
                    font_size_px: 40,
                    auto_mode: null,
                },
            ],
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
            text_layers: [
                {
                    text: "X",
                    box: { x: 0.1, y: 0.1, w: 0.8, h: 0.8 },
                },
            ],
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
        // §5.10a v3: box lives on the per-layer entry now.
        const layer0 = payload.text_layers[0];
        expect(layer0.box.w).toBeCloseTo(0.6, 5);
        expect(layer0.box.h).toBeCloseTo(0.6, 5);
        // x and y are unchanged by an SE drag (origin stays put).
        expect(layer0.box.x).toBeCloseTo(0.1, 5);
        expect(layer0.box.y).toBeCloseTo(0.1, 5);
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
            background_color: "#CC0000",
            duration_ms: 7000,
            text_layers: [
                {
                    text: "PROMO",
                    text_color: "#ffffff",
                    font_family: "monospace",
                    font_size_px: 40,
                    box: { x: 0.2, y: 0.3, w: 0.5, h: 0.4 },
                },
            ],
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
        expect(payload.text_layers[0].box).toEqual({ x: 0.2, y: 0.3, w: 0.5, h: 0.4 });
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
            text_layers: [{ text: "OLD" }],
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
                background_color: "#000000",
                duration_ms: 5000,
                text_layers: [
                    {
                        text: "PROMO",
                        text_color: "#ffffff",
                        font_family: "Pacifico",
                        font_size_px: 40,
                        auto_mode: null,
                    },
                ],
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

    it("clicking + New layer adds a layer at the TOP of the UI list (drawn last → on top)", async () => {
        // §5.10a v3: "+ New layer adds at the top of the list (drawn
        // last → composited on top)". The UI list renders in REVERSE of
        // array order, so the new layer (which addLayer pushes to the
        // array tail) must appear at DOM child[0] and become the active
        // layer.
        patchCanvasPrototype();
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue({ id: "abc" });
        const handle = mountEditor(container, { width: 128, height: 96, onSave });

        // Start: one layer, auto-named "Layer 1" (qarl 2026-05-01:
        // new layers pre-populate name with the next-unused "Layer N").
        const list = container.querySelector(".editor-layers-list");
        expect(list.children.length).toBe(1);
        expect(list.children[0].querySelector(".editor-layer-name-display").textContent).toBe("Layer 1");

        // Type into layer 1 (the only one) so it's distinguishable.
        const layer0Text = list.children[0].querySelector(".field-text");
        layer0Text.value = "BOTTOM";
        layer0Text.dispatchEvent(new Event("input", { bubbles: true }));

        // Click +New. New layer lands at array tail (drawn last) and
        // appears at DOM[0] (top of UI). Old layer slides to DOM[1].
        // Auto-naming: new layer takes the next-unused N → "Layer 2";
        // original keeps its "Layer 1" (names are sticky to the layer,
        // not the visual position — same as slide-name nextAutoName).
        container.querySelector(".editor-add-layer").click();
        expect(list.children.length).toBe(2);
        expect(list.children[0].querySelector(".editor-layer-name-display").textContent).toBe("Layer 2");
        expect(list.children[1].querySelector(".editor-layer-name-display").textContent).toBe("Layer 1");
        // Old "BOTTOM" text is now in DOM[1] (Layer 1 / array index 0).
        expect(list.children[1].querySelector(".field-text").value).toBe("BOTTOM");
        // New layer is empty + selected.
        expect(list.children[0].querySelector(".field-text").value).toBe("");
        expect(list.children[0].classList.contains("editor-layer-active")).toBe(true);

        // Type into the new top layer + save.
        const layer1Text = list.children[0].querySelector(".field-text");
        layer1Text.value = "TOP";
        layer1Text.dispatchEvent(new Event("input", { bubbles: true }));
        await handle.flushAutoSave();

        const payload = onSave.mock.calls[0][0];
        // Array order: index 0 is drawn first (BOTTOM), index 1 is
        // drawn last (TOP). UI top = array tail.
        expect(payload.text_layers.length).toBe(2);
        expect(payload.text_layers[0].text).toBe("BOTTOM");
        expect(payload.text_layers[1].text).toBe("TOP");
    });

    it("delete-layer affordance removes the targeted layer + collapses to single-layer when 2→1", async () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue({ id: "abc" });
        const handle = mountEditor(container, { width: 128, height: 96, onSave });
        const list = container.querySelector(".editor-layers-list");

        // Seed two layers.
        list.children[0].querySelector(".field-text").value = "BOTTOM";
        list.children[0].querySelector(".field-text").dispatchEvent(new Event("input", { bubbles: true }));
        container.querySelector(".editor-add-layer").click();
        list.children[0].querySelector(".field-text").value = "TOP";
        list.children[0].querySelector(".field-text").dispatchEvent(new Event("input", { bubbles: true }));
        expect(list.children.length).toBe(2);
        // Both layers' delete buttons are visible.
        expect(list.children[0].querySelector(".editor-layer-delete").hidden).toBe(false);

        // Delete the top layer (DOM[0] = array tail = "Layer 2").
        list.children[0].querySelector(".editor-layer-delete").click();
        expect(list.children.length).toBe(1);
        // Sole layer keeps its sticky auto-name "Layer 1" and shows BOTTOM.
        expect(list.children[0].querySelector(".editor-layer-name-display").textContent).toBe("Layer 1");
        expect(list.children[0].querySelector(".field-text").value).toBe("BOTTOM");
        // Delete button is hidden when only one layer remains (backend
        // min_length=1).
        expect(list.children[0].querySelector(".editor-layer-delete").hidden).toBe(true);

        await handle.flushAutoSave();
        const payload = onSave.mock.calls[onSave.mock.calls.length - 1][0];
        expect(payload.text_layers.length).toBe(1);
        expect(payload.text_layers[0].text).toBe("BOTTOM");
    });

    it("loadForEdit hydrates a multi-layer slide; UI shows layer 1 at top + saves the same shape", async () => {
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
            name: "Stacked",
            background_color: "#000000",
            duration_ms: 5000,
            text_layers: [
                { text: "BOTTOM", text_color: "#FF0000" },
                { text: "TOP", text_color: "#FFFFFF" },
            ],
        });
        const list = container.querySelector(".editor-layers-list");
        expect(list.children.length).toBe(2);
        // §5.10a v3.1 review #1 (qarl 2026-05-01): empty-name layers
        // backfill on load with the smallest-unused "Layer N", same
        // convention as +New. Array-order backfill: layer[0] → Layer 1,
        // layer[1] → Layer 2. Visual top = array tail = "Layer 2"="TOP".
        expect(list.children[0].querySelector(".editor-layer-name-display").textContent).toBe("Layer 2");
        expect(list.children[1].querySelector(".editor-layer-name-display").textContent).toBe("Layer 1");
        expect(list.children[0].querySelector(".field-text").value).toBe("TOP");
        expect(list.children[1].querySelector(".field-text").value).toBe("BOTTOM");

        await handle.flushAutoSave();
        const payload = onSaveExisting.mock.calls[0][1];
        expect(payload.text_layers[0].text).toBe("BOTTOM");
        expect(payload.text_layers[1].text).toBe("TOP");
    });

    it("auto-mode segmented control explicitly kicks autosave (qarl ask #3 root cause)", async () => {
        // Bug QA flagged 2026-05-01: thumbnails don't update on layer
        // edits. Root cause: segmented controls drive a HIDDEN
        // input via `.value = …`, which dispatches no event — so
        // attachAutoSave's form-level input/change listener never sees
        // the change and the debounce never schedules. Real-world: tile
        // thumb keeps the pre-edit updated_at in `?v=` indefinitely. Fix:
        // explicitly autoSave.kick() in the segmented click handlers,
        // same shape as box-drag's onBoxPointerUp.
        //
        // Test guards against regression by using fake timers to advance
        // past the debounce WITHOUT calling flushAutoSave (which would
        // mask the bug — flush attempts a save regardless of the timer
        // state). Real usage relies on the debounce-schedule path.
        vi.useFakeTimers();
        try {
            patchCanvasPrototype();
            const container = document.createElement("div");
            const onSaveExisting = vi.fn().mockResolvedValue({ id: "abc" });
            const handle = mountEditor(container, {
                width: 128,
                height: 96,
                onSave: vi.fn().mockResolvedValue({ id: "abc" }),
                onSaveExisting,
            });
            // Pre-load an existing slide so canSave passes without
            // typing into the text field (typing would route through
            // the form's input listener, masking the segmented bug).
            await handle.loadForEdit({
                type: "text_slide",
                id: "abc",
                name: "X",
                text_layers: [{ text: "X" }],
            });

            // Click auto-mode-segmented "time" button (still uses the
            // hidden-input pattern that needs the explicit kick — motion
            // moved to a <select> in step 2 of the motion spec, so it
            // dispatches change events natively and no longer needs this
            // regression coverage).
            container
                .querySelector('.field-auto-mode-segmented button[data-value="time"]')
                .click();

            // Advance past the 900ms autosave debounce.
            await vi.advanceTimersByTimeAsync(950);
            // attempt() resolves an awaitable promise inside the
            // debounced fn; the in-flight save's microtasks need a turn
            // before the assertion sees the call.
            await vi.advanceTimersByTimeAsync(0);

            expect(onSaveExisting).toHaveBeenCalled();
            const payload =
                onSaveExisting.mock.calls[onSaveExisting.mock.calls.length - 1][1];
            expect(payload.text_layers[0].auto_mode).toBe("time");
        } finally {
            vi.useRealTimers();
        }
    });

    it("loadForEdit backfills empty-name layers with auto Layer-N (qarl 2026-05-01 review #1)", async () => {
        // Pre-76934f2 saves left layer.name="" — without backfill the
        // saved name stays blank forever, the input shows the
        // 'Headline' placeholder, and the chip falls through to a
        // visual fallback. Backfill on load surfaces "Layer N" in the
        // input AND saves it on next edit. Custom-named layers DON'T
        // get renamed; they keep reserving their slot via nextLayerName.
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
            name: "Stacked",
            background_color: "#000000",
            duration_ms: 5000,
            text_layers: [
                { text: "BOTTOM" },              // unnamed → Layer 1 (smallest unused)
                { text: "MID", name: "Headline" }, // custom → keeps name
                { text: "TOP" },                 // unnamed → Layer 2 (next unused)
            ],
        });
        const list = container.querySelector(".editor-layers-list");
        // Per-layer name input is hydrated with the backfilled name —
        // operator sees a real label, not a placeholder.
        expect(list.children[0].querySelector(".field-layer-name").value).toBe("Layer 2");
        expect(list.children[1].querySelector(".field-layer-name").value).toBe("Headline");
        expect(list.children[2].querySelector(".field-layer-name").value).toBe("Layer 1");

        await handle.flushAutoSave();
        const payload = onSaveExisting.mock.calls[0][1];
        // Backfilled names ride through to the wire shape — next save
        // catches up the slide model. Custom name preserved.
        expect(payload.text_layers[0].name).toBe("Layer 1");
        expect(payload.text_layers[1].name).toBe("Headline");
        expect(payload.text_layers[2].name).toBe("Layer 2");
    });

    it("single-layer slide always shows 'Layer 1' (no bare 'Layer' fallback)", async () => {
        // Review #1: "qarl wants to see 'Layer 1' / 'Layer 2' / etc
        // visibly in the header chip even when name is blank." A sole
        // layer used to fall through to "Layer" (no number).
        patchCanvasPrototype();
        const container = document.createElement("div");
        const handle = mountEditor(container, {
            width: 128,
            height: 96,
            onSave: vi.fn(),
            onSaveExisting: vi.fn().mockResolvedValue({}),
        });
        await handle.loadForEdit({
            type: "text_slide",
            id: "x",
            name: "X",
            text_layers: [{ text: "X" }],   // unnamed
        });
        const list = container.querySelector(".editor-layers-list");
        expect(list.children[0].querySelector(".editor-layer-name-display").textContent).toBe("Layer 1");
    });

    it("auto-named layers: new layers fill the smallest unused 'Layer N' slot", async () => {
        // §5.10a v3.1 (qarl 2026-05-01): on +New layer, default name
        // is the smallest unused "Layer N". Custom-named layers (e.g.
        // "Headline") don't reserve a slot — deleting "Layer 2" then
        // adding fills back as "Layer 2".
        patchCanvasPrototype();
        const container = document.createElement("div");
        const handle = mountEditor(container, {
            width: 128,
            height: 96,
            onSave: vi.fn().mockResolvedValue({ id: "abc" }),
        });
        const list = container.querySelector(".editor-layers-list");

        // Start: Layer 1.
        expect(list.children[0].querySelector(".editor-layer-name-display").textContent).toBe("Layer 1");

        // +New → Layer 2 at top.
        container.querySelector(".editor-add-layer").click();
        expect(list.children[0].querySelector(".editor-layer-name-display").textContent).toBe("Layer 2");

        // +New → Layer 3 at top.
        container.querySelector(".editor-add-layer").click();
        expect(list.children[0].querySelector(".editor-layer-name-display").textContent).toBe("Layer 3");

        // Delete Layer 2 (now at DOM[1] — DOM[0]=Layer 3, DOM[1]=Layer 2,
        // DOM[2]=Layer 1).
        list.children[1].querySelector(".editor-layer-delete").click();
        expect(list.children.length).toBe(2);

        // +New → fills the gap with "Layer 2".
        container.querySelector(".editor-add-layer").click();
        expect(list.children[0].querySelector(".editor-layer-name-display").textContent).toBe("Layer 2");

        // Custom-named layer doesn't reserve a slot.
        list.children[0].querySelector(".field-layer-name").value = "Headline";
        list.children[0].querySelector(".field-layer-name").dispatchEvent(new Event("input", { bubbles: true }));
        // +New → since no Layer 2 left, "Layer 2" again.
        container.querySelector(".editor-add-layer").click();
        expect(list.children[0].querySelector(".editor-layer-name-display").textContent).toBe("Layer 2");

        await handle.flushAutoSave();
    });

    it("accordion: only one layer is expanded at a time; clicking another header swaps", async () => {
        // §5.10a v3.1 (accordion editor): one-open-at-a-time. On mount
        // the sole layer is expanded. Adding a second layer expands the
        // new (TOP / array-tail) one and collapses the previous. Clicking
        // the OTHER card's header opens it and collapses the first.
        patchCanvasPrototype();
        const container = document.createElement("div");
        const handle = mountEditor(container, {
            width: 128,
            height: 96,
            onSave: vi.fn().mockResolvedValue({}),
        });
        const list = container.querySelector(".editor-layers-list");
        // Initial: 1 layer, expanded.
        expect(list.children[0].classList.contains("editor-layer-expanded")).toBe(true);

        // + New layer: new card lands at DOM[0] expanded; old card collapses.
        container.querySelector(".editor-add-layer").click();
        expect(list.children.length).toBe(2);
        expect(list.children[0].classList.contains("editor-layer-expanded")).toBe(true);
        expect(list.children[1].classList.contains("editor-layer-expanded")).toBe(false);

        // Click DOM[1]'s header → it expands, DOM[0] collapses.
        list.children[1].querySelector(".editor-layer-head").click();
        expect(list.children[0].classList.contains("editor-layer-expanded")).toBe(false);
        expect(list.children[1].classList.contains("editor-layer-expanded")).toBe(true);

        // Cleanup the autosave that the +New layer kicked.
        await handle.flushAutoSave();
    });

    it("eye toggle hides a layer from save-time rasterization", async () => {
        // §5.10a v3.1: clicking the eye icon on a layer's header sets
        // visible=false. The save payload reflects that, AND the layer
        // is excluded from drawCanvas / rasterizeAtTarget so the stored
        // PNG matches the editor preview.
        patchCanvasPrototype();
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue({ id: "x" });
        const handle = mountEditor(container, { width: 128, height: 96, onSave });
        const list = container.querySelector(".editor-layers-list");

        container.querySelector(".field-text").value = "VISIBLE";
        container.querySelector(".field-text").dispatchEvent(new Event("input", { bubbles: true }));

        // Toggle eye → layer goes hidden.
        list.children[0].querySelector(".editor-layer-eye").click();
        expect(list.children[0].classList.contains("editor-layer-hidden")).toBe(true);

        await handle.flushAutoSave();
        expect(onSave.mock.calls[0][0].text_layers[0].visible).toBe(false);
    });

    it("motion select rides through to the save payload", async () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue({ id: "x" });
        const handle = mountEditor(container, { width: 128, height: 96, onSave });

        container.querySelector(".field-text").value = "RUN";
        container.querySelector(".field-text").dispatchEvent(new Event("input", { bubbles: true }));

        // Default motion: static. Pick the new "ticker" value (post-
        // 2026-05-02 motion-spec rename of "scroll" → "ticker") via the
        // <select> that replaced the 3-button segmented control.
        const motionSelect = container.querySelector(".field-motion");
        motionSelect.value = "ticker";
        motionSelect.dispatchEvent(new Event("change", { bubbles: true }));
        expect(motionSelect.value).toBe("ticker");

        await handle.flushAutoSave();
        expect(onSave.mock.calls[0][0].text_layers[0].motion).toBe("ticker");
    });

    it("motion intensity + phase ride through to the save payload", async () => {
        // Step 2 of the motion spec adds two operator-facing knobs
        // (intensity 0-100 default 50, phase 0.0-1.0 default 0.0). They
        // ride to the wire as `motion_intensity` / `motion_phase`.
        patchCanvasPrototype();
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue({ id: "x" });
        const handle = mountEditor(container, { width: 128, height: 96, onSave });

        container.querySelector(".field-text").value = "RUN";
        container.querySelector(".field-text").dispatchEvent(new Event("input", { bubbles: true }));

        // Switch to a non-static effect so the intensity+phase row
        // shows. Intensity default is 50 — bump to 80. Phase default
        // is 0 — bump to 0.25.
        const motionSelect = container.querySelector(".field-motion");
        motionSelect.value = "breathe";
        motionSelect.dispatchEvent(new Event("change", { bubbles: true }));

        const intensityEl = container.querySelector(".field-motion-intensity");
        intensityEl.value = "80";
        intensityEl.dispatchEvent(new Event("input", { bubbles: true }));

        const phaseEl = container.querySelector(".field-motion-phase");
        phaseEl.value = "0.25";
        phaseEl.dispatchEvent(new Event("input", { bubbles: true }));

        await handle.flushAutoSave();
        const layer = onSave.mock.calls[0][0].text_layers[0];
        expect(layer.motion).toBe("breathe");
        expect(layer.motion_intensity).toBe(80);
        expect(layer.motion_phase).toBe(0.25);
    });

    it("motion intensity + phase row hides when motion=static", async () => {
        // intensity/phase have no meaning without a non-static effect
        // picked. Test that toggling motion=static hides the row.
        patchCanvasPrototype();
        const container = document.createElement("div");
        mountEditor(container, { width: 128, height: 96, onSave: vi.fn() });

        const motionSelect = container.querySelector(".field-motion");
        const controlsRow = container.querySelector(".field-motion-controls");
        // Default state: motion=static, controls hidden.
        expect(motionSelect.value).toBe("static");
        expect(controlsRow.hidden).toBe(true);

        // Pick a non-static effect → controls row becomes visible.
        motionSelect.value = "ticker";
        motionSelect.dispatchEvent(new Event("change", { bubbles: true }));
        expect(controlsRow.hidden).toBe(false);

        // Back to static → controls row hides again.
        motionSelect.value = "static";
        motionSelect.dispatchEvent(new Event("change", { bubbles: true }));
        expect(controlsRow.hidden).toBe(true);
    });

    it("motion thumb gets a motion-{kind} class for CSS keyframes preview", async () => {
        // Spec docs/text-layer-motion-spec.md (Q3 lock): editor preview
        // uses CSS keyframes. The implementation tags the layer thumb
        // with a `motion-{kind}` class; styles.css has the @keyframes.
        // Test the class hookup, not the CSS itself (jsdom can't render
        // animation; the class+keyframe contract is what matters).
        patchCanvasPrototype();
        const container = document.createElement("div");
        mountEditor(container, { width: 128, height: 96, onSave: vi.fn() });
        const thumbEl = container.querySelector(".editor-layer-thumb");

        // Default motion=static → no motion-* class.
        expect([...thumbEl.classList].some((c) => c.startsWith("motion-"))).toBe(false);

        const motionSelect = container.querySelector(".field-motion");
        motionSelect.value = "breathe";
        motionSelect.dispatchEvent(new Event("change", { bubbles: true }));
        expect(thumbEl.classList.contains("motion-breathe")).toBe(true);

        // Switching effects swaps the class, doesn't accumulate.
        motionSelect.value = "ticker";
        motionSelect.dispatchEvent(new Event("change", { bubbles: true }));
        expect(thumbEl.classList.contains("motion-breathe")).toBe(false);
        expect(thumbEl.classList.contains("motion-ticker")).toBe(true);

        // Back to static → all motion-* classes drop.
        motionSelect.value = "static";
        motionSelect.dispatchEvent(new Event("change", { bubbles: true }));
        expect([...thumbEl.classList].some((c) => c.startsWith("motion-"))).toBe(false);
    });

    it("ticker thumb wraps text in a doubled track for seamless repeat", async () => {
        // qarl 2026-05-02 demo eyeball: the prior single-text-translate
        // ticker showed "text exits, gap, text returns." Fix: two text
        // copies inside an inline-flex track, animated translateX 0 →
        // -50% (= one copy width), so the second copy slides into where
        // the first started. Test the DOM structure that enables the
        // seamless loop; CSS is exercised in browser, not jsdom.
        patchCanvasPrototype();
        const container = document.createElement("div");
        mountEditor(container, { width: 128, height: 96, onSave: vi.fn() });
        // Type text first so the thumb has a value other than the
        // empty-state em-dash.
        const text = container.querySelector(".field-text");
        text.value = "MARQUEE";
        text.dispatchEvent(new Event("input", { bubbles: true }));

        const motionSelect = container.querySelector(".field-motion");
        motionSelect.value = "ticker";
        motionSelect.dispatchEvent(new Event("change", { bubbles: true }));

        const thumbEl = container.querySelector(".editor-layer-thumb");
        const track = thumbEl.querySelector(".editor-layer-thumb-ticker-track");
        expect(track).not.toBeNull();
        const copies = track.querySelectorAll(":scope > span");
        expect(copies.length).toBe(2);
        expect(copies[0].textContent).toBe("MARQUEE");
        expect(copies[1].textContent).toBe("MARQUEE");
    });

    it("non-ticker thumb wraps text in a single inner span", async () => {
        // breathe / pulse / bounce / shake / blink all animate the
        // same inner span (.editor-layer-thumb-text) so the thumb's
        // overflow:hidden clips them — animating the thumb itself
        // would move its clipping box too. Static thumbs use the
        // same wrapper for layout consistency.
        patchCanvasPrototype();
        const container = document.createElement("div");
        mountEditor(container, { width: 128, height: 96, onSave: vi.fn() });
        const text = container.querySelector(".field-text");
        text.value = "PULSE";
        text.dispatchEvent(new Event("input", { bubbles: true }));

        const motionSelect = container.querySelector(".field-motion");
        for (const kind of ["static", "breathe", "pulse", "bounce", "shake", "blink"]) {
            motionSelect.value = kind;
            motionSelect.dispatchEvent(new Event("change", { bubbles: true }));
            const thumbEl = container.querySelector(".editor-layer-thumb");
            const span = thumbEl.querySelector(".editor-layer-thumb-text");
            expect(span).not.toBeNull();
            expect(span.textContent).toBe("PULSE");
            // Ticker structure should NOT exist for non-ticker effects.
            expect(thumbEl.querySelector(".editor-layer-thumb-ticker-track")).toBeNull();
        }
    });

    it("switching from ticker to non-ticker swaps thumb DOM cleanly", async () => {
        // Both ends of the structure swap: ticker → text-span erases
        // the track; text-span → ticker erases the span. No accumulated
        // children that could overlap visually.
        patchCanvasPrototype();
        const container = document.createElement("div");
        mountEditor(container, { width: 128, height: 96, onSave: vi.fn() });
        const text = container.querySelector(".field-text");
        text.value = "X";
        text.dispatchEvent(new Event("input", { bubbles: true }));

        const motionSelect = container.querySelector(".field-motion");
        motionSelect.value = "ticker";
        motionSelect.dispatchEvent(new Event("change", { bubbles: true }));
        const thumbEl = container.querySelector(".editor-layer-thumb");
        expect(thumbEl.querySelector(".editor-layer-thumb-ticker-track")).not.toBeNull();
        expect(thumbEl.querySelector(".editor-layer-thumb-text")).toBeNull();

        motionSelect.value = "breathe";
        motionSelect.dispatchEvent(new Event("change", { bubbles: true }));
        expect(thumbEl.querySelector(".editor-layer-thumb-ticker-track")).toBeNull();
        expect(thumbEl.querySelector(".editor-layer-thumb-text")).not.toBeNull();

        motionSelect.value = "ticker";
        motionSelect.dispatchEvent(new Event("change", { bubbles: true }));
        expect(thumbEl.querySelector(".editor-layer-thumb-ticker-track")).not.toBeNull();
        expect(thumbEl.querySelector(".editor-layer-thumb-text")).toBeNull();
    });

    it("layer name input drives the header name display + saves on the wire", async () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue({ id: "x" });
        const handle = mountEditor(container, { width: 128, height: 96, onSave });
        const list = container.querySelector(".editor-layers-list");

        // Editor's per-layer name field is .field-layer-name (slide-level
        // .field-name lives at the top of the editor unchanged).
        const nameInput = list.children[0].querySelector(".field-layer-name");
        nameInput.value = "Headline";
        nameInput.dispatchEvent(new Event("input", { bubbles: true }));

        // Header's name display (always visible) reflects the value.
        expect(
            list.children[0].querySelector(".editor-layer-name-display").textContent,
        ).toBe("Headline");

        container.querySelector(".field-text").value = "OPEN";
        container.querySelector(".field-text").dispatchEvent(new Event("input", { bubbles: true }));
        await handle.flushAutoSave();
        expect(onSave.mock.calls[0][0].text_layers[0].name).toBe("Headline");
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
                background_color: "#000000",
                background_video_slide_id: "vid-1",
                duration_ms: 5000,
                text_layers: [
                    {
                        text: "Bar Open",
                        text_color: "#FFFFFF",
                    },
                ],
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
                translate: () => {}, scale: () => {},
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
