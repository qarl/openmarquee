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
});
