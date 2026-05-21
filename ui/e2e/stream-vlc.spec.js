// Stream takeover e2e (STREAM/VLC slice 5).
//
// The stream source needs no camera and no WebRTC — the panel just
// POSTs the operator's stream URL to /api/stream/start with
// kind:"stream". So this spec only mocks the backend Stream endpoints;
// no getUserMedia / RTCPeerConnection stubs are needed (unlike
// stream-panel.spec.js).

import { expect, test } from "@playwright/test";
import { resetServerState } from "./_helpers.js";

test.beforeEach(() => {
    resetServerState();
});

function mockIdleStatus(page) {
    return page.route("/api/stream/status", (route) => {
        route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({
                state: "idle",
                session_id: null,
                tier: {
                    name: "basic",
                    max_width: 854,
                    max_height: 480,
                    max_fps: 30,
                },
            }),
        });
    });
}

test("source toggle switches to VLC: URL field + Start streaming appear", async ({
    page,
}) => {
    await mockIdleStatus(page);
    await page.goto("/#/stream");
    await expect(page.locator('.panel[data-section="stream"]')).toBeVisible();

    // Camera is the default source: viewfinder shown, VLC panel hidden.
    await expect(
        page.locator('.stream-source-opt[data-source="camera"]'),
    ).toHaveClass(/is-selected/);
    await expect(page.locator(".stream-vlc-panel")).toBeHidden();

    // Playwright auto-waits for the toggle to be enabled — it is
    // render()-disabled during the brief camera mount-init window.
    await page.locator('.stream-source-opt[data-source="vlc"]').click();

    await expect(
        page.locator('.stream-source-opt[data-source="vlc"]'),
    ).toHaveClass(/is-selected/);
    await expect(page.locator(".stream-vlc-panel")).toBeVisible();
    await expect(page.locator(".stream-vlc-url")).toBeVisible();
    await expect(page.locator(".stream-start-vlc")).toBeVisible();
    await expect(page.locator(".stream-go-live")).toBeHidden();
    await expect(page.locator(".stream-stage")).toBeHidden();
});

test("Stream Start → live → Stop posts a kind=stream body and cycles to idle", async ({
    page,
}) => {
    await mockIdleStatus(page);
    let startBody = null;
    await page.route("/api/stream/start", (route) => {
        startBody = route.request().postDataJSON();
        route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({
                session_id: "33333333-3333-3333-3333-333333333333",
                sdp_answer: null,
                started_at: new Date(Date.now() - 3000).toISOString(),
            }),
        });
    });
    await page.route("/api/stream/stop", (route) => route.fulfill({ status: 204 }));

    await page.goto("/#/stream");
    await page.locator('.stream-source-opt[data-source="vlc"]').click();
    await page
        .locator(".stream-vlc-url")
        .fill("rtsp://laptop.tail-net:8554/live");
    await page.locator(".stream-start-vlc").click();

    // Live state: Stop visible, Start hidden, status says Live. VLC
    // mode has no viewfinder, so no LIVE pill — the camera stage is
    // hidden.
    await expect(page.locator(".stream-stop")).toBeVisible();
    await expect(page.locator(".stream-start-vlc")).toBeHidden();
    await expect(page.locator(".stream-stage")).toBeHidden();
    await expect(page.locator(".stream-status")).toContainText("Live");

    // The panel POSTed a kind=stream body carrying the operator's URL.
    expect(startBody).toEqual({
        kind: "stream",
        url: "rtsp://laptop.tail-net:8554/live",
    });

    await page.locator(".stream-stop").click();
    // VLC has no preview phase — Stop returns to idle (Start streaming
    // is back, Stop + LIVE pill gone).
    await expect(page.locator(".stream-start-vlc")).toBeVisible();
    await expect(page.locator(".stream-stop")).toBeHidden();
    await expect(page.locator(".stream-live-pill")).toBeHidden();
});

test("the How-to-publish-from-VLC disclosure expands", async ({ page }) => {
    await mockIdleStatus(page);
    await page.goto("/#/stream");
    await page.locator('.stream-source-opt[data-source="vlc"]').click();

    await expect(page.locator(".stream-vlc-help")).toBeVisible();
    // <details> is collapsed by default — its <ol> is hidden until the
    // summary is clicked.
    await expect(page.locator(".stream-vlc-help ol")).toBeHidden();
    await page.locator(".stream-vlc-help summary").click();
    await expect(page.locator(".stream-vlc-help ol")).toBeVisible();
});
