// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mock the video-frame helpers at module scope so processVideo's
// real HTMLVideoElement / FileReader paths don't run in jsdom. Each
// mock is a controllable promise the test resolves/rejects to
// drive the state machine deterministically. Tests that don't need
// to exercise processVideo (e.g. the loadForEdit / no-file-pick
// branches) are unaffected since they never call these helpers.
vi.mock("./video-frame-helpers.js", () => ({
    peekVideoDims: vi.fn().mockResolvedValue({ width: 1920, height: 1080 }),
    drawFirstFrameToCanvas: vi.fn().mockResolvedValue({ durationSeconds: 10 }),
    fileToBase64: vi.fn().mockResolvedValue("MOCK_MP4_BASE64"),
}));
// Same shape for the ffmpeg pipeline -- transcodeToH264 is the
// expensive real call we'd otherwise need an actual wasm binary for.
// describeFfmpegError is left to its real impl (a pure-function
// mapping, already covered in ffmpeg-pipelines.test.js).
vi.mock("./ffmpeg-pipelines.js", async (importOriginal) => {
    const actual = await importOriginal();
    return {
        ...actual,
        transcodeToH264: vi.fn().mockResolvedValue(new Uint8Array([0x00, 0x00, 0x00, 0x20])),
    };
});

import { mountVideoUploader } from "./video-upload.js";
import {
    drawFirstFrameToCanvas,
    fileToBase64,
    peekVideoDims,
} from "./video-frame-helpers.js";
import { transcodeToH264 } from "./ffmpeg-pipelines.js";

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
    // Reset the module-scope mocks between tests so prior calls don't
    // bleed across assertions. Default behaviors are re-installed below.
    peekVideoDims.mockReset().mockResolvedValue({ width: 1920, height: 1080 });
    drawFirstFrameToCanvas.mockReset().mockResolvedValue({ durationSeconds: 10 });
    fileToBase64.mockReset().mockResolvedValue("MOCK_MP4_BASE64");
    transcodeToH264.mockReset().mockResolvedValue(new Uint8Array([0x00, 0x00, 0x00, 0x20]));
});

afterEach(() => {
    vi.restoreAllMocks();
});

// Helper: inject a fake FileList onto the file input so the change
// handler reaches processVideo (jsdom doesn't expose a way to populate
// HTMLInputElement.files via the public API without DataTransfer).
function setFile(fileEl, file) {
    Object.defineProperty(fileEl, "files", {
        value: file ? [file] : [],
        configurable: true,
    });
}

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
        // Helpers must NOT be touched on the no-file branch (they're
        // only reached past the early-return at line 177).
        expect(peekVideoDims).not.toHaveBeenCalled();
        expect(transcodeToH264).not.toHaveBeenCalled();
    });
});


// State-machine + race tests for the file-pick + processVideo flow.
// The 4 existing tests above heavily cover the no-file / metadata-
// only / blank-mount paths; this block covers the success-path
// transcode flow, the error path, the cleanup race, and the
// loadForEdit-reject + createNew paths surfaced by the 2026-05-24
// test-coverage audit.

describe("mountVideoUploader — file-pick + processVideo flow", () => {
    function mockFile(name = "clip.mov", bytes = [0xff, 0xd8]) {
        return new File([new Uint8Array(bytes)], name, { type: "video/quicktime" });
    }

    it("file pick triggers transcode + disables fileEl during, re-enables after", async () => {
        // Locks the shipped #9 concurrent-upload defense: while
        // transcodeToH264 is awaiting, the picker must be disabled so
        // a fast operator can't queue a second job that would double-
        // write mp4Base64. Re-enable must fire in the finally regardless
        // of success/error path.
        let resolveTranscode;
        transcodeToH264.mockImplementation(
            () => new Promise((resolve) => { resolveTranscode = resolve; }),
        );

        const container = document.createElement("div");
        mountVideoUploader(container, {
            width: 128, height: 96,
            onSave: vi.fn().mockResolvedValue({ id: "v-new" }),
        });
        await tick();
        const fileEl = container.querySelector(".field-file");
        // Pre-conditions: input enabled.
        expect(fileEl.disabled).toBe(false);

        setFile(fileEl, mockFile());
        fileEl.dispatchEvent(new Event("change", { bubbles: true }));
        // Drain the synchronous + first-microtask cycles so the change
        // handler runs through `fileEl.disabled = true` + reaches the
        // awaited transcode.
        await tick();
        await tick();
        expect(fileEl.disabled).toBe(true);

        // Resolve the parked transcode; finally{} re-enables.
        resolveTranscode(new Uint8Array([0x00, 0x00, 0x00, 0x20]));
        await tick();
        await tick();
        expect(fileEl.disabled).toBe(false);
    });

    it("transcode error: status text shows describeFfmpegError + state stays clean", async () => {
        // processVideo's catch (video-upload.js:210) must surface a
        // friendly status message + clear state so a retry with a
        // different file starts from blank. fileEl.disabled must also
        // return to false (the same cleanup-on-error contract test 1
        // covers from the success side).
        transcodeToH264.mockRejectedValue(new Error("invalid h264 stream"));

        const container = document.createElement("div");
        const onSave = vi.fn();
        const handle = mountVideoUploader(container, {
            width: 128, height: 96, onSave,
        });
        await tick();
        const fileEl = container.querySelector(".field-file");
        const statusEl = container.querySelector(".video-upload-status");

        setFile(fileEl, mockFile());
        fileEl.dispatchEvent(new Event("change", { bubbles: true }));
        await tick();
        await tick();
        await tick();

        expect(statusEl.textContent).toMatch(/Could not process video/);
        expect(statusEl.textContent).toMatch(/invalid h264 stream/);
        expect(statusEl.dataset.state).toBe("error");
        // Picker is re-enabled so the operator can try a different file.
        expect(fileEl.disabled).toBe(false);
        // No save was attempted (canSave gate suppresses on clean state).
        await handle.flushAutoSave();
        expect(onSave).not.toHaveBeenCalled();
    });

    it("file pick on a NEW slide calls onSave with both png_base64 and mp4_base64", async () => {
        // Locks the new-create path: processVideo succeeds → autoSave.kick
        // → performSave reads canvasToBase64(canvas) + state.mp4Base64
        // and calls onSave with both populated (because !editingId means
        // includeAssets=true at video-upload.js:259).
        const onSave = vi.fn().mockResolvedValue({ id: "v-new" });
        const container = document.createElement("div");
        const handle = mountVideoUploader(container, {
            width: 128, height: 96, onSave,
        });
        await tick();
        const fileEl = container.querySelector(".field-file");

        setFile(fileEl, mockFile("intro.mov"));
        fileEl.dispatchEvent(new Event("change", { bubbles: true }));
        // Drain transcode + the autoSave debounce → flush.
        await tick();
        await tick();
        await handle.flushAutoSave();

        expect(onSave).toHaveBeenCalledTimes(1);
        const payload = onSave.mock.calls[0][0];
        expect(payload.name).toBe("intro"); // filename minus extension
        expect(payload.png_base64).toBe("THUMB"); // from toDataURL mock
        expect(payload.mp4_base64).toBe("MOCK_MP4_BASE64");
    });

    it("file pick during edit-mode calls onSaveExisting with new assets (distinct from metadata-only branch)", async () => {
        // Distinct from the existing test 2 (which covers the metadata-
        // only branch — no re-pick, png/mp4 null). Here the operator
        // is editing v-1 AND re-picked the file, so the cascade must
        // include the new assets in onSaveExisting's payload.
        const RealImage = window.Image;
        window.Image = class {
            constructor() { setTimeout(() => this.onload && this.onload(), 0); }
            set src(_) {} set crossOrigin(_) {}
            set onload(fn) { this._onload = fn; }
            get onload() { return this._onload; }
            onerror = null;
            width = 128; height = 96;
        };

        const onSaveExisting = vi.fn().mockResolvedValue({ id: "v-1" });
        const container = document.createElement("div");
        const handle = mountVideoUploader(container, {
            width: 128, height: 96,
            onSave: vi.fn(),
            onSaveExisting,
        });
        await handle.loadForEdit({
            type: "video", id: "v-1", name: "Old Promo",
            duration_ms: 12000, pipeline: "h264_mp4",
        });
        await tick();
        // Re-pick: operator wants to swap the underlying video. The
        // change handler runs processVideo → state.mp4Base64 set →
        // autoSave.kick. canSave returns true (editingId set), and
        // includeAssets is true since thumbnailCanvasReady just flipped.
        const fileEl = container.querySelector(".field-file");
        setFile(fileEl, mockFile("new-promo.mov"));
        fileEl.dispatchEvent(new Event("change", { bubbles: true }));
        await tick();
        await tick();
        await handle.flushAutoSave();

        // Find the call WITH non-null assets (the loadForEdit shouldn't
        // have triggered an auto-save; the re-pick should be the first
        // onSaveExisting call regardless).
        expect(onSaveExisting).toHaveBeenCalled();
        const reuploadCall = onSaveExisting.mock.calls.find(
            ([, payload]) => payload.mp4_base64 !== null,
        );
        expect(reuploadCall).toBeDefined();
        const [id, payload] = reuploadCall;
        expect(id).toBe("v-1"); // same editingId
        expect(payload.png_base64).toBe("THUMB");
        expect(payload.mp4_base64).toBe("MOCK_MP4_BASE64");

        window.Image = RealImage;
    });

    it("loadForEdit on a non-video slide writes a friendly status (mirrors image-upload's parallel test)", async () => {
        const container = document.createElement("div");
        const handle = mountVideoUploader(container, {
            width: 128, height: 96,
            onSave: vi.fn(),
            onSaveExisting: vi.fn(),
        });
        await tick();
        await handle.loadForEdit({ type: "text_slide", id: "t-1" });
        const statusEl = container.querySelector(".video-upload-status");
        expect(statusEl.textContent).toMatch(/Only video slides are editable/);
    });

    it("createNew submits placeholder bytes + sets editingId so subsequent saves go through onSaveExisting", async () => {
        // The +New flow uses a pre-bundled PLACEHOLDER_MP4_B64 so the
        // slide appears in the pallet immediately (no waiting on a file
        // pick). createNew must:
        //   1) call onSave with the placeholder bytes
        //   2) capture the returned id into editingId so subsequent
        //      metadata mutations route through onSaveExisting, NOT
        //      a second onSave (which would create a duplicate slide).
        const onSave = vi.fn().mockResolvedValue({ id: "v-fresh" });
        const onSaveExisting = vi.fn().mockResolvedValue({ id: "v-fresh" });
        const container = document.createElement("div");
        const handle = mountVideoUploader(container, {
            width: 128, height: 96, onSave, onSaveExisting,
        });
        await tick();

        await handle.createNew();
        expect(onSave).toHaveBeenCalledTimes(1);
        const createPayload = onSave.mock.calls[0][0];
        // Placeholder bytes get sent up — non-null + non-empty.
        expect(createPayload.mp4_base64).toBeTruthy();
        expect(createPayload.mp4_base64.length).toBeGreaterThan(100);
        expect(createPayload.png_base64).toBe("THUMB");

        // Now mutate a metadata field → auto-save should route through
        // onSaveExisting (proves editingId was captured), NOT onSave.
        container.querySelector(".field-name").value = "Renamed";
        container.querySelector(".field-name").dispatchEvent(
            new Event("input", { bubbles: true }),
        );
        await handle.flushAutoSave();
        expect(onSave).toHaveBeenCalledTimes(1); // unchanged
        expect(onSaveExisting).toHaveBeenCalled();
        expect(onSaveExisting.mock.calls[0][0]).toBe("v-fresh");
    });
});
