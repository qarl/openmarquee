// Settings save in the main window flows through a BroadcastChannel
// to any open simulator pop-out, which re-applies the skin + mode
// label without the operator having to reload the window.

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
    await page.request.put("/api/settings", {
        data: settingsPayload("hdmi", 128, 96),
    });
});

test("simulator pop-out updates its skin when settings change in the opener", async ({ page }) => {
    await page.goto("/");
    await page.locator('.nav-link[data-section="playlists"]').click();

    // Open the simulator pop-out via the button so BroadcastChannel
    // binds across the two same-origin windows.
    const [popup] = await Promise.all([
        page.context().waitForEvent("page"),
        page.locator(".playback-simulator").click(),
    ]);
    await popup.waitForLoadState("domcontentloaded");
    // Initial mode = hdmi.
    await expect(popup.locator('[data-field="mode"]')).toHaveText("hdmi");

    // Switch settings in the main window.
    await page.locator('.nav-link[data-section="settings"]').click();
    await page.locator(".field-output-mode").selectOption("hub75");
    await page.locator(".field-display-width").fill("64");
    await page.locator(".field-display-height").fill("32");
    await page.locator(".settings-save").click();
    await expect(page.locator(".settings-status")).toHaveText("Saved.");

    // The simulator's mode label reflects the change — no reload
    // needed. The broadcast + re-apply is ~100ms tops; Playwright's
    // toHaveText retries so the timing isn't brittle.
    await expect(popup.locator('[data-field="mode"]')).toHaveText("hub75");
});
