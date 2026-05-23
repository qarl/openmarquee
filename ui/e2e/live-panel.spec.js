// Live panel e2e (SYSTEM_SPEC §5.11). Phase 12.2 dry-land coverage.
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
        // Phase B.1: getStats() returns a minimal RTCStats report so
        // the panel's pollStats() populates the metrics cells with
        // non-mock values during the live phase. RTT 0.042s -> 42ms.
        async getStats() {
            return new Map([
                ["outbound", {
                    type: "outbound-rtp", kind: "video",
                    bytesSent: 100000, timestamp: 1000000,
                }],
                ["inbound", {
                    type: "remote-inbound-rtp", kind: "video",
                    roundTripTime: 0.042, packetsLost: 0,
                }],
            ]);
        }
    };
`;

test("Live nav route shows the panel with Go Live button", async ({ page }) => {
    await page.goto("/");

    await page.locator('.nav-link[data-section="live"]').click();
    await expect(page.locator('.panel[data-section="live"]')).toBeVisible();
    await expect(page.locator('.nav-link[data-section="live"]')).toHaveClass(
        /active/,
    );

    await expect(page.locator(".live-header-title")).toHaveText("Live");
    await expect(page.locator(".live-go-live")).toBeVisible();
    await expect(page.locator(".live-stop")).toBeHidden();
    // 2026-04-29 redesign dropped the tailscale-foreground warning.
    // The camera-flip button was dropped then restored 2026-05-01 —
    // it exists but is hidden in idle (no open camera). LIVE pill +
    // metrics grid only surface in the live phase. Idle-only paused-
    // playlist row is visible.
    await expect(page.locator(".live-warning")).toHaveCount(0);
    await expect(page.locator(".live-flip-camera")).toBeHidden();
    await expect(page.locator(".live-live-pill")).toBeHidden();
    await expect(page.locator(".live-metrics-grid")).toBeHidden();
    await expect(page.locator(".live-paused-row")).toBeVisible();
});

test("Go Live → Stop cycle flips through live and back to idle", async ({
    page,
}) => {
    // Stub WebRTC + getUserMedia BEFORE the page boots, otherwise the
    // panel mounts against the real (camera-less) globals first and the
    // first click produces a permission-denied error.
    await page.addInitScript(STUB_INIT_SCRIPT);
    // Mock the backend Live endpoints. /status reports idle, /start
    // returns a fake answer + session id, /stop returns 204. Lets us
    // verify the panel flow without depending on real aiortc.
    await page.route("/api/live/status", (route) => {
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
    await page.route("/api/live/start", (route) => {
        route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({
                session_id: "11111111-1111-1111-1111-111111111111",
                sdp_answer: "v=0\r\nfake-answer\r\n",
                // Phase A.2: backend stamps wall-clock UTC start time.
                // 5s ago so the Elapsed assertion below has something
                // to observe before the panel's 1Hz tick fires.
                started_at: new Date(Date.now() - 5000).toISOString(),
            }),
        });
    });
    await page.route("/api/live/stop", (route) => {
        route.fulfill({ status: 204 });
    });

    await page.goto("/#/live");
    await expect(page.locator('.panel[data-section="live"]')).toBeVisible();

    await page.locator(".live-go-live").click();
    // Live state surfaces: Stop button visible, Go live hidden,
    // LIVE pill on the viewfinder visible, 4-cell metrics grid below
    // the viewfinder visible, idle-only paused-playlist row hidden.
    await expect(page.locator(".live-stop")).toBeVisible();
    await expect(page.locator(".live-go-live")).toBeHidden();
    await expect(page.locator(".live-live-pill")).toBeVisible();
    await expect(page.locator(".live-metrics-grid")).toBeVisible();
    await expect(page.locator(".live-paused-row")).toBeHidden();
    await expect(page.locator(".live-status")).toContainText("Live");

    // Phase A.2: Elapsed cell ticks against the wire-served started_at
    // (mocked 5s ago in the /start route above), NOT phone-local time.
    // Cell value is MM:SS, total seconds >= 5 by the time this expect
    // resolves. Playwright's expect.poll retries until the predicate
    // passes or 5s default timeout, comfortably covering the 1Hz tick.
    await expect
        .poll(async () => {
            const txt = await page
                .locator('[data-metric="elapsed"]')
                .textContent();
            const match = /^(\d\d):(\d\d)$/.exec(txt || "");
            if (!match) return 0;
            return Number(match[1]) * 60 + Number(match[2]);
        })
        .toBeGreaterThanOrEqual(5);

    // Phase B.1: latency cell rewrites from the template default
    // ('78 ms') to the polled value derived from the stub's RTT
    // (0.042s -> 42 ms). Proves pollStats() ran against the stubbed
    // getStats(). Note: 78 ms is also the template default so this
    // check is meaningful — without B.1 wired, the cell would stay
    // at '78 ms' which would still match a /^\d+ ms$/ regex but
    // wouldn't equal '42 ms'.
    await expect(page.locator('[data-metric="latency"]')).toHaveText("42 ms");

    await page.locator(".live-stop").click();
    await expect(page.locator(".live-go-live")).toBeVisible();
    await expect(page.locator(".live-stop")).toBeHidden();
    await expect(page.locator(".live-live-pill")).toBeHidden();
    await expect(page.locator(".live-metrics-grid")).toBeHidden();
    await expect(page.locator(".live-paused-row")).toBeVisible();
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
    await page.route("/api/live/status", (route) => {
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

    await page.goto("/#/live");
    // Mount-init's own /status pre-flight surfaces the take-over UI —
    // no Go-live click needed (and clicking would race mount-init).

    await expect(page.locator(".live-take-over")).toBeVisible();
    await expect(page.locator(".live-cancel-takeover")).toBeVisible();
    await expect(page.locator(".live-go-live")).toBeHidden();
    await expect(page.locator(".live-status")).toContainText("Someone else");

    // Camera permission was NOT requested — saves the operator a dialog
    // they'd just dismiss after seeing the "take over" prompt.
    expect(await page.evaluate(() => window.__cameraOpened)).toBe(false);
});
