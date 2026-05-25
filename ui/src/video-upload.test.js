// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mountVideoUploader } from "./video-upload.js";

function tick() {
    return new Promise((r) => setTimeout(r, 0));
}

// jsdom has no Canvas or Video decoding; stub just enough to keep the
// module loadable and testable at the DOM-wiring level. Real decode +
// frame extraction is exercised in the browser (Playwright) only.
beforeEach(() => {
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(() => ({
        save: () => {},
        restore: () => {},
        fillRect: () => {},
        drawImage: () => {},
        set fillStyle(_v) {},
    }));
    vi.spyOn(HTMLCanvasElement.prototype, "toDataURL").mockReturnValue(
        "data:image/png;base64,THUMB",
    );
});

afterEach(() => {
    vi.restoreAllMocks();
});

describe("mountVideoUploader", () => {
    it("renders the upload form with status pill (no Save button — auto-save)", async () => {
        const container = document.createElement("div");
        mountVideoUploader(container, {
            width: 128,
            height: 96,
            onSave: vi.fn(),
        });
        await tick();
        expect(container.querySelector(".field-save")).toBeNull();
        expect(container.querySelector(".om-save-status")).not.toBeNull();
        // Pipeline dropdown is gone — the uploader always transcodes to
        // H.264 via ffmpeg.wasm now.
        expect(container.querySelector(".field-pipeline")).toBeNull();
    });

    it("loadForEdit pre-fills + auto-save sends metadata-only payload (no new file)", async () => {
        // Stub Image so drawUrlToCanvas for the stored thumbnail resolves.
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
        const onSaveExisting = vi.fn().mockResolvedValue({ id: "v-1" });
        const handle = mountVideoUploader(container, {
            width: 128,
            height: 96,
            onSave: vi.fn(),
            onSaveExisting,
        });
        await handle.loadForEdit({
            type: "video",
            id: "v-1",
            name: "Promo",
            duration_ms: 15000,
            pipeline: "h264_mp4",
        });
        await tick();

        expect(container.querySelector(".field-name").value).toBe("Promo");
        expect(container.querySelector(".field-duration").value).toBe("15");

        // Mutate the name to trigger auto-save.
        container.querySelector(".field-name").value = "Updated Promo";
        container.querySelector(".field-name").dispatchEvent(
            new Event("input", { bubbles: true }),
        );
        await handle.flushAutoSave();
        expect(onSaveExisting).toHaveBeenCalledTimes(1);
        const [id, payload] = onSaveExisting.mock.calls[0];
        expect(id).toBe("v-1");
        expect(payload.name).toBe("Updated Promo");
        expect(payload.png_base64).toBeNull();
        expect(payload.mp4_base64).toBeNull();

        window.Image = RealImage;
    });

    it("starts in blank-create mode even when fetchItems has saved videos (regression: QA explore-image-upload 2026-04-26)", async () => {
        // Same UX argument as image-upload: the uploader stays blank on
        // mount so an operator who picks a file doesn't silently overwrite
        // an auto-loaded existing video. Edit-existing still works via
        // slide-browser tile click → loadForEdit.
        const container = document.createElement("div");
        const onSave = vi.fn();
        const onSaveExisting = vi.fn();
        mountVideoUploader(container, {
            width: 128,
            height: 96,
            onSave,
            onSaveExisting,
            fetchItems: async () => [
                {
                    id: "older-vid",
                    type: "video",
                    name: "Old Loop",
                    created_at: "2026-04-20T00:00:00Z",
                },
            ],
        });
        for (let i = 0; i < 4; i++) await new Promise((r) => setTimeout(r, 0));
        // Auto-name path took over instead of loading "Old Loop".
        expect(container.querySelector(".field-name").value).toMatch(
            /Video Slide \d+/,
        );
        expect(onSaveExisting).not.toHaveBeenCalled();
        expect(onSave).not.toHaveBeenCalled();
    });

    it("file pick with no file is a no-op (no save attempted)", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn();
        const handle = mountVideoUploader(container, {
            width: 128,
            height: 96,
            onSave,
        });
        await tick();
        // Simulate a file pick with no actual file — change handler runs,
        // canSave gate keeps auto-save suppressed.
        const fileEl = container.querySelector(".field-file");
        fileEl.dispatchEvent(new Event("change", { bubbles: true }));
        await handle.flushAutoSave();
        expect(onSave).not.toHaveBeenCalled();
    });

    it("isStale guard prevents canvas-poison + stale preview when operator switches slide mid-load", async () => {
        // Round-17 regression-lock for the slide-switch race:
        // operator clicks A (slow load), then B; A's image onload
        // resolves AFTER B's loadForEdit completed and would
        // otherwise (a) ctx.drawImage(A) over B's pixels in the
        // canvas (next autoSave's canvasToBase64 would PATCH B's
        // record with A's PNG) and (b) setPreviewSrc(A) reverting
        // videoEl.src to A's MP4.
        const drawImageMock = vi.fn();
        vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(() => ({
            save: () => {},
            restore: () => {},
            fillRect: () => {},
            drawImage: drawImageMock,
            set fillStyle(_v) {},
        }));

        const RealImage = window.Image;
        // Test-controlled Image.onload firing keyed on URL substring.
        const triggers = {};
        window.Image = class {
            constructor() {
                this.width = 128;
                this.height = 96;
            }
            set crossOrigin(_) {}
            set onload(fn) { this._onload = fn; }
            get onload() { return this._onload; }
            onerror = null;
            set src(url) {
                if (url.includes("/api/content/A/asset")) {
                    triggers.A = () => this._onload?.();
                } else if (url.includes("/api/content/B/asset")) {
                    triggers.B = () => this._onload?.();
                }
            }
        };

        try {
            const container = document.createElement("div");
            const handle = mountVideoUploader(container, {
                width: 128,
                height: 96,
                onSave: vi.fn(),
                onSaveExisting: vi.fn(),
            });
            await tick();

            // Start A; don't await; A's image stays pending.
            const aPromise = handle.loadForEdit({
                type: "video",
                id: "A",
                name: "Slide A",
                duration_ms: 5000,
            });
            await tick();

            // Switch to B mid-flight. B's image will resolve immediately
            // below; A's is still pending.
            const bPromise = handle.loadForEdit({
                type: "video",
                id: "B",
                name: "Slide B",
                duration_ms: 5000,
            });
            await tick();
            triggers.B();
            await bPromise;

            const drawImageCallsAfterB = drawImageMock.mock.calls.length;
            const videoElAfterB = container.querySelector("video").src;
            expect(videoElAfterB).toContain("/api/content/B/video");

            // Now A's late onload fires. isStale === true →
            // ctx.drawImage(A) SKIPPED; after-await guard → setPreviewSrc
            // for A SKIPPED. Both observables stay at B.
            triggers.A();
            await aPromise;

            expect(drawImageMock).toHaveBeenCalledTimes(drawImageCallsAfterB);
            expect(container.querySelector("video").src).toBe(videoElAfterB);
            expect(container.querySelector("video").src).not.toContain("/api/content/A/video");
        } finally {
            window.Image = RealImage;
        }
    });
});
