// Regression: every slide tile (slide-browser on Text/Image/Video
// subpages + pallet-tile on the Playlists panel) must expose a delete
// affordance that actually removes the slide. Bug report: "slide
// thumbnail buttons should include delete functionality."

import { expect, test } from "@playwright/test";
import { resetServerState, saveTextSlide } from "./_helpers.js";

test.beforeEach(() => {
    resetServerState();
});

test("slide-browser tile delete button removes the slide", async ({ page }) => {
    await saveTextSlide(page, "DeleteMe");
    // The tile should be present before delete.
    const tile = page.locator(`.slide-browser-tile[data-id]`, { hasText: "DeleteMe" });
    await expect(tile).toHaveCount(1);
    // Confirm the window.confirm() dialog on delete.
    page.once("dialog", (d) => d.accept());
    await tile.locator(".slide-browser-tile-delete").click();
    await expect(tile).toHaveCount(0, { timeout: 5_000 });
});

test("pallet-tile delete button removes the slide from the Playlists panel", async ({ page }) => {
    await saveTextSlide(page, "PalletKill");
    await page.goto("/#/playlists");
    const tile = page.locator(`.pallet-tile[data-id]`, { hasText: "PalletKill" });
    await expect(tile).toHaveCount(1);
    page.once("dialog", (d) => d.accept());
    await tile.locator(".pallet-tile-delete").click();
    await expect(tile).toHaveCount(0, { timeout: 5_000 });
});

test("delete is cancelable — Cancel in the confirm dialog leaves the slide alone", async ({ page }) => {
    await saveTextSlide(page, "KeepMe");
    const tile = page.locator(`.slide-browser-tile[data-id]`, { hasText: "KeepMe" });
    await expect(tile).toHaveCount(1);
    page.once("dialog", (d) => d.dismiss());
    await tile.locator(".slide-browser-tile-delete").click();
    // Give the (non-)delete time to not happen.
    await page.waitForTimeout(300);
    await expect(tile).toHaveCount(1);
});
