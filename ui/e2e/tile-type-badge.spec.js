// Regression for bug (#6): slide-browser tiles and pallet tiles used
// to look different for the same slide because the pallet had a
// type-badge overlay ("Aa"/"🖼"/"▶") and the browser tiles didn't.
// The same slide should render identically in either place.

import { expect, test } from "@playwright/test";
import { resetServerState } from "./_helpers.js";

test.beforeEach(() => {
    resetServerState();
});

test("slide-browser text tile has the Aa type badge", async ({ page }) => {
    await page.goto("/#/slides/text");
    await page.fill(".editor .field-name", "BadgeTest");
    await page.fill(".editor .field-text", "BadgeTest");
    await page.locator(".editor .field-save").click();
    await expect(page.locator(".editor .editor-status")).toContainText(/Saved|Updated/);

    const tile = page.locator('.slide-browser-tile[data-id]', { hasText: "BadgeTest" });
    const badge = tile.locator(".slide-browser-tile-type");
    await expect(badge).toHaveCount(1);
    await expect(badge).toHaveText("Aa");
});

test("pallet tile for the same slide also has the Aa badge (consistency)", async ({ page }) => {
    await page.goto("/#/slides/text");
    await page.fill(".editor .field-name", "BadgeTest");
    await page.fill(".editor .field-text", "BadgeTest");
    await page.locator(".editor .field-save").click();
    await expect(page.locator(".editor .editor-status")).toContainText(/Saved|Updated/);

    await page.goto("/#/playlists");
    const tile = page.locator('.pallet-tile[data-id]', { hasText: "BadgeTest" });
    const badge = tile.locator(".pallet-tile-type");
    await expect(badge).toHaveCount(1);
    await expect(badge).toHaveText("Aa");
});
