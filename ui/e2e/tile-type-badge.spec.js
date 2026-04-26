// Pallet tiles carry an "Aa"/"🖼"/"▶" type badge so an operator scanning
// a mixed-type pallet can spot a slide's medium at a glance. The
// slide-browser tiles deliberately don't — the slides shell tab subnav
// (Text / Image / Video) already filters by type, making the per-tile
// corner badge redundant chrome (see slide-browser.js renderTile).
//
// Original bug #6 ("browser tiles look different from pallet tiles")
// was resolved by retiring the badge on the browser side, not by
// adding it everywhere.

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

test("pallet tile for a text slide carries an Aa badge", async ({ page }) => {
    await saveTextSlide(page, "BadgeTest");

    await page.goto("/#/playlists");
    const tile = page.locator('.pallet-tile[data-id]', { hasText: "BadgeTest" });
    const badge = tile.locator(".pallet-tile-type");
    await expect(badge).toHaveCount(1);
    await expect(badge).toHaveText("Aa");
});
