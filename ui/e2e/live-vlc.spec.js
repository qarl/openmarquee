// Live (stream-transport) takeover e2e (STREAM/VLC slice 5).
//
// The stream-transport source needs no camera and no WebRTC — the
// panel just POSTs the operator's stream URL to /api/live/start with
// kind:"stream". So this spec only mocks the backend Live endpoints;
// no getUserMedia / RTCPeerConnection stubs are needed (unlike
// live-panel.spec.js).

import { expect, test } from "@playwright/test";
import { resetServerState } from "./_helpers.js";

test.beforeEach(() => {
    resetServerState();
});

function mockIdleStatus(page) {
    return page.route("/api/live/status", (route) => {
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
    await page.goto("/#/live");
    await expect(page.locator('.panel[data-section="live"]')).toBeVisible();

    // Camera is the default source: viewfinder shown, VLC panel hidden.
    await expect(
        page.locator('.live-source-opt[data-source="camera"]'),
    ).toHaveClass(/is-selected/);
    await expect(page.locator(".live-vlc-panel")).toBeHidden();

    // Playwright auto-waits for the toggle to be enabled — it is
    // render()-disabled during the brief camera mount-init window.
    await page.locator('.live-source-opt[data-source="vlc"]').click();

    await expect(
        page.locator('.live-source-opt[data-source="vlc"]'),
    ).toHaveClass(/is-selected/);
    await expect(page.locator(".live-vlc-panel")).toBeVisible();
    await expect(page.locator(".live-vlc-url")).toBeVisible();
    await expect(page.locator(".live-start-vlc")).toBeVisible();
    await expect(page.locator(".live-go-live")).toBeHidden();
    await expect(page.locator(".live-stage")).toBeHidden();
});

test("Live (stream-transport) Start → live → Stop posts a kind=stream body and cycles to idle", async ({
    page,
}) => {
    await mockIdleStatus(page);
    let startBody = null;
    await page.route("/api/live/start", (route) => {
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
    await page.route("/api/live/stop", (route) => route.fulfill({ status: 204 }));

    await page.goto("/#/live");
    await page.locator('.live-source-opt[data-source="vlc"]').click();
    await page
        .locator(".live-vlc-url")
        .fill("rtsp://laptop.tail-net:8554/live");
    await page.locator(".live-start-vlc").click();

    // Live state: Stop visible, Start hidden, status says Live. VLC
    // mode has no viewfinder, so no LIVE pill — the camera stage is
    // hidden.
    await expect(page.locator(".live-stop")).toBeVisible();
    await expect(page.locator(".live-start-vlc")).toBeHidden();
    await expect(page.locator(".live-stage")).toBeHidden();
    await expect(page.locator(".live-status")).toContainText("Live");

    // The panel POSTed a kind=stream body carrying the operator's URL.
    expect(startBody).toEqual({
        kind: "stream",
        url: "rtsp://laptop.tail-net:8554/live",
    });

    await page.locator(".live-stop").click();
    // VLC has no preview phase — Stop returns to idle (Start streaming
    // is back, Stop + LIVE pill gone).
    await expect(page.locator(".live-start-vlc")).toBeVisible();
    await expect(page.locator(".live-stop")).toBeHidden();
    await expect(page.locator(".live-live-pill")).toBeHidden();
});

test("the How-to-publish-from-VLC disclosure expands", async ({ page }) => {
    await mockIdleStatus(page);
    await page.goto("/#/live");
    await page.locator('.live-source-opt[data-source="vlc"]').click();

    await expect(page.locator(".live-vlc-help")).toBeVisible();
    // <details> is collapsed by default — its <ol> is hidden until the
    // summary is clicked.
    await expect(page.locator(".live-vlc-help ol")).toBeHidden();
    await page.locator(".live-vlc-help summary").click();
    await expect(page.locator(".live-vlc-help ol")).toBeVisible();
});
