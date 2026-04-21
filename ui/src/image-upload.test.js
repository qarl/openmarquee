// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { canvasToBase64, mountImageUploader } from "./image-upload.js";

function patchCanvasPrototype() {
    const fakeCtx = {
        fillStyle: "",
        fillRect: vi.fn(),
        drawImage: vi.fn(),
        save: vi.fn(),
        restore: vi.fn(),
    };
    HTMLCanvasElement.prototype.getContext = vi.fn(() => fakeCtx);
    HTMLCanvasElement.prototype.toDataURL = vi.fn(() => "data:image/png;base64,IMAGEDATA");
    return fakeCtx;
}

function tick() {
    return new Promise((r) => setTimeout(r, 0));
}

describe("mountImageUploader", () => {
    it("renders file input, name input, save button (disabled), and preview canvas", () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        mountImageUploader(container, { width: 128, height: 96, onSave: vi.fn() });

        expect(container.querySelector(".field-file")).not.toBeNull();
        expect(container.querySelector(".field-name")).not.toBeNull();
        expect(container.querySelector(".field-save")).not.toBeNull();
        expect(container.querySelector(".field-save").disabled).toBe(true);
        expect(container.querySelector(".image-upload-canvas")).not.toBeNull();
    });

    it("paints a black background on the preview canvas at mount", () => {
        const ctx = patchCanvasPrototype();
        const container = document.createElement("div");
        mountImageUploader(container, { width: 64, height: 32, onSave: vi.fn() });
        // Initial clear + fillRect.
        expect(ctx.fillRect).toHaveBeenCalledWith(0, 0, 64, 32);
    });

    it("submit without a file does nothing", async () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        const onSave = vi.fn();
        mountImageUploader(container, { width: 64, height: 32, onSave });

        container.querySelector(".controls").dispatchEvent(new Event("submit"));
        await tick();
        expect(onSave).not.toHaveBeenCalled();
    });
});

describe("canvasToBase64", () => {
    it("strips the data URL prefix", () => {
        patchCanvasPrototype();
        const canvas = document.createElement("canvas");
        canvas.width = 1;
        canvas.height = 1;
        expect(canvasToBase64(canvas)).toBe("IMAGEDATA");
    });
});
