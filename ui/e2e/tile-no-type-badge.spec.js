// The slide-browser tiles deliberately don't carry a type badge —
// the slides shell tab subnav (Text / Image / Video) already filters
// by type, making the per-tile corner badge redundant chrome (see
// slide-browser.js renderTile). Pallet tiles also dropped their
// badge in the qarl UX-cleanup batch — the thumbnail conveys medium
// well enough on its own.

import { expect, test } from "@playwright/test";
import { resetServerState, saveTextSlide } from "./_helpers.js";

test.beforeEach(() => {
    resetServerState();
});

test("slide-browser tile does NOT carry a type badge (subnav handles type filtering)", async ({ page }) => {
    await saveTextSlide(page, "BadgeTest");

    const tile = page.locator('.slide-browser-tile[data-id]', { hasText: "BadgeTest" });
    await expect(tile).toBeVisible();
    await expect(tile.locator(".slide-browser-tile-type")).toHaveCount(0);
});

test("pallet tile does NOT carry a type badge (thumbnail conveys medium)", async ({ page }) => {
    await saveTextSlide(page, "BadgeTest");

    await page.goto("/#/playlists");
    const tile = page.locator('.pallet-tile[data-id]', { hasText: "BadgeTest" });
    await expect(tile).toBeVisible();
    await expect(tile.locator(".pallet-tile-type")).toHaveCount(0);
});
