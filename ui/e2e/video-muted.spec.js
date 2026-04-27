// Regression for bug (#5): videos should play without sound. Captive-
// portal signage isn't meant to blast audio at whoever walks by.
//
// Note on the volume slider: an earlier version of this test also
// asserted controlslist included `novolumeslider`, but Chromium never
// recognized that token (only nodownload / nofullscreen / noplaybackrate /
// noremoteplayback are real). The slider stays as Chromium renders it;
// the muted default is what guarantees silence regardless.

import { expect, test } from "@playwright/test";

test("video preview element is muted", async ({ page }) => {
    await page.goto("/#/slides/video");
    const video = page.locator(".video-upload-video");
    await expect(video).toBeVisible();

    // `muted` is an HTML boolean attribute — set at mount time via the
    // template. `toHaveJSProperty` reads the DOM property, which is the
    // reliable check for boolean attrs.
    await expect(video).toHaveJSProperty("muted", true);

    // Double-check the DOM property too — a video element stays muted
    // across src swaps, which matters for the edit-existing flow.
    const muted = await video.evaluate((el) => el.muted);
    expect(muted).toBe(true);
});
