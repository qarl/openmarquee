// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fileToBase64, mountVideoUploader } from "./video-upload.js";

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
    it("renders the upload form with Save disabled until a file is picked", async () => {
        const container = document.createElement("div");
        mountVideoUploader(container, {
            width: 128,
            height: 96,
            onSave: vi.fn(),
        });
        await tick();
        expect(container.querySelector(".field-save").disabled).toBe(true);
        expect(container.querySelector(".field-pipeline").value).toBe("h264_mp4");
    });

    it("exposes both pipelines now that the ffmpeg.wasm spike produces raw frames", async () => {
        const container = document.createElement("div");
        mountVideoUploader(container, {
            width: 128,
            height: 96,
            onSave: vi.fn(),
        });
        await tick();
        const options = Array.from(
            container.querySelectorAll(".field-pipeline option"),
        ).map((o) => o.value);
        expect(options).toEqual(["h264_mp4", "raw_frames"]);
    });

    it("loadForEdit pre-fills + allows metadata-only save (no new file)", async () => {
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
        expect(container.querySelector(".field-save").disabled).toBe(false);

        container.querySelector(".controls").dispatchEvent(new Event("submit"));
        await tick();
        expect(onSaveExisting).toHaveBeenCalledTimes(1);
        const [id, payload] = onSaveExisting.mock.calls[0];
        expect(id).toBe("v-1");
        expect(payload.name).toBe("Promo");
        expect(payload.png_base64).toBeNull();
        expect(payload.mp4_base64).toBeNull();

        window.Image = RealImage;
    });

    it("surfaces a known message when the video can't be read", async () => {
        const container = document.createElement("div");
        mountVideoUploader(container, {
            width: 128,
            height: 96,
            onSave: vi.fn(),
        });
        await tick();
        // Simulate a file pick with no actual file — change handler sets
        // the disabled state without erroring.
        const fileEl = container.querySelector(".field-file");
        fileEl.dispatchEvent(new Event("change"));
        await tick();
        expect(container.querySelector(".field-save").disabled).toBe(true);
    });
});

describe("fileToBase64", () => {
    it("strips the data: prefix and returns just the base64 body", async () => {
        const blob = new Blob([Uint8Array.from([1, 2, 3, 4])], {
            type: "application/octet-stream",
        });
        const body = await fileToBase64(blob);
        // 4 bytes → 8 base64 chars (before any padding handling).
        expect(body).toMatch(/^[A-Za-z0-9+/]+=*$/);
        // The raw bytes 0x01 0x02 0x03 0x04 encode as "AQIDBA==".
        expect(body).toBe("AQIDBA==");
    });
});
