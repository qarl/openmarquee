// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { fileToBase64 } from "./video-frame-helpers.js";

// drawFirstFrameToCanvas + peekVideoDims aren't directly tested here
// because they orchestrate HTMLVideoElement event flows that jsdom
// doesn't fire for synthetic blob URLs. Their callers (video-upload.js
// processVideo + rotation-rerender.js) are tested via vi.mock against
// this module, which gives us behavioral coverage of the integration
// without paying for a brittle HTMLVideoElement stub.

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
