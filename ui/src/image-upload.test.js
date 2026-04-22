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

    it("loadForEdit pre-fills name + duration and enables Save without re-pick", async () => {
        patchCanvasPrototype();
        // Stub Image so drawUrlToCanvas resolves fast in jsdom.
        const RealImage = window.Image;
        window.Image = class {
            constructor() {
                setTimeout(() => this.onload && this.onload(), 0);
            }
            set src(_) {}
            set crossOrigin(_) {}
            set onload(fn) {
                this._onload = fn;
            }
            get onload() {
                return this._onload;
            }
            onerror = null;
            width = 128;
            height = 96;
        };

        const container = document.createElement("div");
        const onSaveExisting = vi.fn().mockResolvedValue({ id: "img-1" });
        const handle = mountImageUploader(container, {
            width: 128,
            height: 96,
            onSave: vi.fn(),
            onSaveExisting,
        });

        await handle.loadForEdit({
            type: "image",
            id: "img-1",
            name: "Logo",
            duration_ms: 8000,
        });
        // Wait out Image.onload microtask.
        await tick();

        expect(container.querySelector(".field-name").value).toBe("Logo");
        expect(container.querySelector(".field-duration").value).toBe("8");
        // Save is enabled even without a new file pick — metadata-only
        // updates are allowed in edit mode.
        expect(container.querySelector(".field-save").disabled).toBe(false);

        container.querySelector(".controls").dispatchEvent(new Event("submit"));
        await tick();
        expect(onSaveExisting).toHaveBeenCalledTimes(1);
        const [id, payload] = onSaveExisting.mock.calls[0];
        expect(id).toBe("img-1");
        expect(payload.name).toBe("Logo");
        expect(payload.duration_ms).toBe(8000);
        // image_base64 is null — operator didn't re-pick a file.
        expect(payload.image_base64).toBeNull();

        window.Image = RealImage;
    });

    it("rejects loadForEdit on a non-image slide with a friendly status", async () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        const handle = mountImageUploader(container, {
            width: 64,
            height: 32,
            onSave: vi.fn(),
            onSaveExisting: vi.fn(),
        });
        await handle.loadForEdit({ type: "text_slide", id: "x" });
        expect(
            container.querySelector(".image-upload-status").textContent,
        ).toMatch(/Only image slides/);
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
