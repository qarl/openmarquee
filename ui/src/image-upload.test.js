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
    it("renders file input, name input, status pill, and preview canvas", () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        mountImageUploader(container, { width: 128, height: 96, onSave: vi.fn() });

        expect(container.querySelector(".field-file")).not.toBeNull();
        expect(container.querySelector(".field-name")).not.toBeNull();
        // Save button is gone — replaced by debounced auto-save.
        expect(container.querySelector(".field-save")).toBeNull();
        expect(container.querySelector(".om-save-status")).not.toBeNull();
        expect(container.querySelector(".image-upload-canvas")).not.toBeNull();
    });

    it("paints a black background on the preview canvas at mount", () => {
        const ctx = patchCanvasPrototype();
        const container = document.createElement("div");
        mountImageUploader(container, { width: 64, height: 32, onSave: vi.fn() });
        // Initial clear + fillRect.
        expect(ctx.fillRect).toHaveBeenCalledWith(0, 0, 64, 32);
    });

    it("auto-save without a file does nothing (canSave gate suppresses)", async () => {
        patchCanvasPrototype();
        const container = document.createElement("div");
        const onSave = vi.fn();
        const handle = mountImageUploader(container, { width: 64, height: 32, onSave });

        // Mutate name to schedule a save; then flush — gate should suppress.
        container.querySelector(".field-name").value = "Untitled";
        container.querySelector(".field-name").dispatchEvent(
            new Event("input", { bubbles: true }),
        );
        await handle.flushAutoSave();
        expect(onSave).not.toHaveBeenCalled();
    });

    it("loadForEdit pre-fills name + duration; auto-save sends metadata-only payload", async () => {
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

        // Mutate the name to trigger an auto-save attempt.
        container.querySelector(".field-name").value = "Updated Logo";
        container.querySelector(".field-name").dispatchEvent(
            new Event("input", { bubbles: true }),
        );
        await handle.flushAutoSave();
        expect(onSaveExisting).toHaveBeenCalledTimes(1);
        const [id, payload] = onSaveExisting.mock.calls[0];
        expect(id).toBe("img-1");
        expect(payload.name).toBe("Updated Logo");
        expect(payload.duration_ms).toBe(8000);
        // image_base64 is null — operator didn't re-pick a file.
        expect(payload.image_base64).toBeNull();

        window.Image = RealImage;
    });

    it("starts in blank-create mode even when fetchItems has saved images (regression: QA explore-image-upload 2026-04-26)", async () => {
        // The uploader used to auto-load the most-recent saved image
        // for edit on mount. An operator who landed here to upload a
        // NEW file then picked one — the file-pick handler PUTs the
        // bytes against `editingId` (the auto-loaded slide) and silently
        // overwrote it. Now the uploader stays blank on mount; explicit
        // edit-existing happens via clicking a slide-browser tile.
        const RealImage = window.Image;
        window.Image = class {
            constructor() {
                setTimeout(() => this.onload && this.onload(), 0);
            }
            set src(_) {}
            set crossOrigin(_) {}
            set onload(fn) { this._onload = fn; }
            get onload() { return this._onload; }
            onerror = null;
            width = 128;
            height = 96;
        };
        try {
            patchCanvasPrototype();
            const container = document.createElement("div");
            const onSave = vi.fn();
            const onSaveExisting = vi.fn();
            mountImageUploader(container, {
                width: 128,
                height: 96,
                onSave,
                onSaveExisting,
                fetchItems: async () => [
                    {
                        id: "older-img",
                        type: "image",
                        name: "Parchment - Background",
                        created_at: "2026-04-20T00:00:00Z",
                    },
                ],
            });
            // Drain a few ticks so any IIFE that WOULD have fired
            // loadForEdit has had its chance.
            for (let i = 0; i < 4; i++) await new Promise((r) => setTimeout(r, 0));

            // Name field should be the auto-allocated next-name, not the
            // existing slide's name.
            expect(container.querySelector(".field-name").value).toMatch(
                /Image Slide \d+/,
            );
            // No PUT/POST should have fired for the auto-loaded slide.
            expect(onSaveExisting).not.toHaveBeenCalled();
            expect(onSave).not.toHaveBeenCalled();
        } finally {
            window.Image = RealImage;
        }
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

    it("isStale guard prevents canvas-poison when operator switches slide mid-load", async () => {
        // Round-17 regression-lock for the image-upload slide-switch
        // race: operator clicks A (slow load), then B; A's image
        // onload resolves AFTER B's loadForEdit completed and would
        // otherwise ctx.drawImage(A) over B's pixels — a subsequent
        // autoSave's canvasToBase64 would PATCH B's record with A's
        // PNG (silent thumbnail corruption).
        const ctx = patchCanvasPrototype();

        const RealImage = window.Image;
        const triggers = {};
        window.Image = class {
            constructor() {
                this.width = 64;
                this.height = 32;
            }
            set crossOrigin(_) {}
            set onload(fn) { this._onload = fn; }
            get onload() { return this._onload; }
            onerror = null;
            set src(url) {
                if (url.includes("/api/content/img-A/asset")) {
                    triggers.A = () => this._onload?.();
                } else if (url.includes("/api/content/img-B/asset")) {
                    triggers.B = () => this._onload?.();
                }
            }
        };

        try {
            const container = document.createElement("div");
            const handle = mountImageUploader(container, {
                width: 64,
                height: 32,
                onSave: vi.fn(),
                onSaveExisting: vi.fn(),
            });
            await tick();

            // Start A; don't await; A's image stays pending.
            const aPromise = handle.loadForEdit({
                type: "image",
                id: "img-A",
                name: "Image A",
                duration_ms: 5000,
            });
            await tick();

            // Switch to B mid-flight. Resolve B's image immediately.
            const bPromise = handle.loadForEdit({
                type: "image",
                id: "img-B",
                name: "Image B",
                duration_ms: 5000,
            });
            await tick();
            triggers.B();
            await bPromise;

            const drawImageCallsAfterB = ctx.drawImage.mock.calls.length;

            // Now A's late onload fires. isStale === true →
            // ctx.drawImage(A) SKIPPED. drawImage count unchanged.
            triggers.A();
            await aPromise;

            expect(ctx.drawImage).toHaveBeenCalledTimes(drawImageCallsAfterB);
        } finally {
            window.Image = RealImage;
        }
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
