// Stream panel e2e (SYSTEM_SPEC §5.11). Phase 12.2 dry-land coverage.
//
// Real WebRTC + a real backend SDP handshake gets a live-fire pass in
// Phase 12.3 (hardware bring-up). Here we stub navigator.mediaDevices
// and RTCPeerConnection in the page so the test exercises the panel's
// state machine + button wiring against a mocked backend, without
// needing a real camera or a real ICE round trip.

import { expect, test } from "@playwright/test";
import { resetServerState } from "./_helpers.js";

test.beforeEach(() => {
    resetServerState();
});

const STUB_INIT_SCRIPT = `
    // Real Chromium's <video>.srcObject setter validates that the value
    // is a MediaStream or null and rejects everything else. Our fake
    // stream isn't a real MediaStream, so the panel's
    // \`previewEl.srcObject = stream\` assignment would throw and shove
    // the panel into the error phase. Make the setter a no-op for the
    // duration of the test — preview pixels don't matter for the
    // state-machine flow we're verifying.
    Object.defineProperty(HTMLVideoElement.prototype, "srcObject", {
        configurable: true,
        set() { /* swallow */ },
        get() { return null; },
    });

    // Fake getUserMedia: returns a minimal MediaStream-shaped object
    // with one stoppable video track. The panel only needs getTracks
    // / getVideoTracks / track.stop() — everything else stays untouched.
    const makeFakeStream = () => {
        const track = {
            kind: "video",
            stopped: false,
            stop() { this.stopped = true; },
        };
        return {
            getTracks: () => [track],
            getVideoTracks: () => [track],
        };
    };
    navigator.mediaDevices = navigator.mediaDevices || {};
    navigator.mediaDevices.getUserMedia = async () => makeFakeStream();

    // Fake RTCPeerConnection: synchronous-enough surface for the panel's
    // negotiate() flow. iceGatheringState is "complete" up front so the
    // panel's waitForIceGathering() returns immediately.
    window.RTCPeerConnection = class {
        constructor() {
            this.iceGatheringState = "complete";
            this.localDescription = { sdp: "v=0\\r\\nfake-offer\\r\\n", type: "offer" };
            this._senders = [];
            this._handlers = {};
        }
        addTrack(track) {
            const sender = { track, replaceTrack: async (t) => { sender.track = t; } };
            this._senders.push(sender);
            return sender;
        }
        getSenders() { return this._senders; }
        async createOffer() { return { sdp: "v=0\\r\\nfake-offer\\r\\n", type: "offer" }; }
        async setLocalDescription(o) { this.localDescription = o; }
        async setRemoteDescription() {}
        addEventListener(e, fn) { this._handlers[e] = fn; }
        removeEventListener() {}
        close() {}
    };
`;

test("Stream nav route shows the panel with Go Live button", async ({ page }) => {
    await page.goto("/");

    await page.locator('.nav-link[data-section="stream"]').click();
    await expect(page.locator('.panel[data-section="stream"]')).toBeVisible();
    await expect(page.locator('.nav-link[data-section="stream"]')).toHaveClass(
        /active/,
    );

    await expect(page.locator(".stream-header-title")).toHaveText("Stream");
    await expect(page.locator(".stream-go-live")).toBeVisible();
    await expect(page.locator(".stream-stop")).toBeHidden();
    // 2026-04-29 redesign: tailscale-foreground warning + camera-flip
    // button removed from the panel template. LIVE pill + metrics grid
    // only surface in the live phase. Idle-only paused-playlist row is
    // visible.
    await expect(page.locator(".stream-warning")).toHaveCount(0);
    await expect(page.locator(".stream-flip-camera")).toHaveCount(0);
    await expect(page.locator(".stream-live-pill")).toBeHidden();
    await expect(page.locator(".stream-metrics-grid")).toBeHidden();
    await expect(page.locator(".stream-paused-row")).toBeVisible();
});

test("Go Live → Stop cycle flips through live and back to idle", async ({
    page,
}) => {
    // Stub WebRTC + getUserMedia BEFORE the page boots, otherwise the
    // panel mounts against the real (camera-less) globals first and the
    // first click produces a permission-denied error.
    await page.addInitScript(STUB_INIT_SCRIPT);
    // Mock the backend Stream endpoints. /status reports idle, /start
    // returns a fake answer + session id, /stop returns 204. Lets us
    // verify the panel flow without depending on real aiortc.
    await page.route("/api/stream/status", (route) => {
        route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({
                state: "idle",
                session_id: null,
                tier: { name: "basic", max_width: 854, max_height: 480, max_fps: 30 },
            }),
        });
    });
    await page.route("/api/stream/start", (route) => {
        route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({
                session_id: "11111111-1111-1111-1111-111111111111",
                sdp_answer: "v=0\r\nfake-answer\r\n",
            }),
        });
    });
    await page.route("/api/stream/stop", (route) => {
        route.fulfill({ status: 204 });
    });

    await page.goto("/#/stream");
    await expect(page.locator('.panel[data-section="stream"]')).toBeVisible();

    await page.locator(".stream-go-live").click();
    // Live state surfaces: Stop button visible, Go live hidden,
    // LIVE pill on the viewfinder visible, 4-cell metrics grid below
    // the viewfinder visible, idle-only paused-playlist row hidden.
    await expect(page.locator(".stream-stop")).toBeVisible();
    await expect(page.locator(".stream-go-live")).toBeHidden();
    await expect(page.locator(".stream-live-pill")).toBeVisible();
    await expect(page.locator(".stream-metrics-grid")).toBeVisible();
    await expect(page.locator(".stream-paused-row")).toBeHidden();
    await expect(page.locator(".stream-status")).toContainText("Live");

    await page.locator(".stream-stop").click();
    await expect(page.locator(".stream-go-live")).toBeVisible();
    await expect(page.locator(".stream-stop")).toBeHidden();
    await expect(page.locator(".stream-live-pill")).toBeHidden();
    await expect(page.locator(".stream-metrics-grid")).toBeHidden();
    await expect(page.locator(".stream-paused-row")).toBeVisible();
});

test("active session at /status surfaces Take over UI without opening camera", async ({
    page,
}) => {
    await page.addInitScript(STUB_INIT_SCRIPT);
    // Track whether getUserMedia was called by smuggling a flag through
    // window — the take-over branch should NOT request camera access
    // until the user explicitly taps Take over.
    await page.addInitScript(`
        window.__cameraOpened = false;
        const real = navigator.mediaDevices.getUserMedia;
        navigator.mediaDevices.getUserMedia = async (...args) => {
            window.__cameraOpened = true;
            return real(...args);
        };
    `);
    await page.route("/api/stream/status", (route) => {
        route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({
                state: "active",
                session_id: "22222222-2222-2222-2222-222222222222",
                tier: { name: "basic", max_width: 854, max_height: 480, max_fps: 30 },
            }),
        });
    });

    await page.goto("/#/stream");
    await page.locator(".stream-go-live").click();

    await expect(page.locator(".stream-take-over")).toBeVisible();
    await expect(page.locator(".stream-cancel-takeover")).toBeVisible();
    await expect(page.locator(".stream-go-live")).toBeHidden();
    await expect(page.locator(".stream-status")).toContainText("Someone else");

    // Camera permission was NOT requested — saves the operator a dialog
    // they'd just dismiss after seeing the "take over" prompt.
    expect(await page.evaluate(() => window.__cameraOpened)).toBe(false);
});
