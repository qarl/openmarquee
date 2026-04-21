// Device-simulator pop-out end-to-end: for each supported output_mode,
// open /simulator.html directly (bypassing the window.open pop-out
// which Playwright handles specially), render a frame, and screenshot
// it. Validates that:
//   - the page loads cleanly per mode
//   - the mode badge in the corner reflects the current output_mode
//   - the canvas is drawn (non-empty)
//
// The visual skins are exercised by unit tests against a fake 2D
// context; this spec is about the full-stack load path + screenshot
// artifacts for visual review.

import { expect, test } from "@playwright/test";
import { resetServerState } from "./_helpers.js";

function settingsPayload(outputMode, width, height) {
    return {
        output_mode: outputMode,
        display_width: width,
        display_height: height,
        display_rotation: 0,
        wifi_ap_enabled: true,
        wifi_station_enabled: false,
        wifi_station_ssid: null,
        wifi_station_password: null,
        timezone: "UTC",
        tailscale_enabled: false,
        tailscale_auth_key: null,
        tailscale_hostname: null,
    };
}

test.beforeEach(async ({ page }) => {
    resetServerState();
    // Prime a rendered frame by saving a text slide and starting
    // playback — otherwise /dev/preview/frame.png 404s and the
    // simulator shows its placeholder.
    await page.goto("/");
    await page.locator(".editor .field-name").fill("SimTarget");
    await page.locator(".editor .field-text").fill("HI");
    await page.locator(".editor .field-duration").fill("5");
    await page.locator(".editor .field-save").click();
    await expect(page.locator(".editor-status")).toHaveText("Saved.");

    await page.request.post("/api/playback/start");
    // Give the loop a beat to render the first frame to disk.
    await page.waitForTimeout(400);
});

test.afterEach(async ({ page }) => {
    await page.request.post("/api/playback/stop");
    await page.request.put("/api/settings", {
        data: settingsPayload("hdmi", 128, 96),
    });
});

async function loadSimulatorFor({ page, outputMode, width, height, label }) {
    await page.request.put("/api/settings", {
        data: settingsPayload(outputMode, width, height),
    });
    // viewport controls the initial pop-up size the simulator would
    // get from window.open; the page's own resizeTo call is a no-op
    // under Playwright but the CSS max-width:100% handles the fallback.
    await page.setViewportSize({ width: 960, height: 720 });
    await page.goto("/simulator.html");

    // Mode badge reflects the current setting.
    await expect(page.locator('[data-field="mode"]')).toHaveText(outputMode);

    // Canvas exists and has non-zero dims.
    const canvas = page.locator(".simulator-canvas");
    await expect(canvas).toBeVisible();
    const box = await canvas.boundingBox();
    expect(box).not.toBeNull();
    expect(box.width).toBeGreaterThan(10);
    expect(box.height).toBeGreaterThan(10);

    // Give the frame-poll loop time to fetch + draw at least once.
    await page.waitForTimeout(600);

    // Placeholder should be hidden now (frame landed).
    await expect(page.locator(".simulator-placeholder")).toHaveClass(/hidden/);

    // Screenshot for visual inspection. File per mode.
    await page.screenshot({
        path: `test-results/simulator-${label}.png`,
        fullPage: false,
    });
}

test("simulator renders HDMI mode — plain aspect-ratio window", async ({ page }) => {
    await loadSimulatorFor({
        page,
        outputMode: "hdmi",
        width: 1920,
        height: 1080,
        label: "hdmi",
    });
});

test("simulator renders HUB75 mode — LED-matrix skin", async ({ page }) => {
    await loadSimulatorFor({
        page,
        outputMode: "hub75",
        width: 64,
        height: 32,
        label: "hub75",
    });
});

test("simulator renders WS2812B mode — strip/LED-dot skin", async ({ page }) => {
    await loadSimulatorFor({
        page,
        outputMode: "ws281x",
        width: 32,
        height: 8,
        label: "ws281x",
    });
});

test("simulator renders composite mode — plain skin, NTSC-ish aspect", async ({ page }) => {
    await loadSimulatorFor({
        page,
        outputMode: "composite",
        width: 720,
        height: 480,
        label: "composite",
    });
});

test("Open simulator button on Playlists panel opens the pop-out", async ({ page }) => {
    // The button uses window.open — in Playwright, capture the new
    // page via the 'popup' event on the context.
    await page.goto("/");
    await page.locator('.nav-link[data-section="playlists"]').click();
    const [popup] = await Promise.all([
        page.context().waitForEvent("page"),
        page.locator(".playback-simulator").click(),
    ]);
    await popup.waitForLoadState("domcontentloaded");
    expect(popup.url()).toMatch(/simulator\.html$/);
    await expect(popup.locator(".simulator-canvas")).toBeVisible();
});
