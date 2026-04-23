// Regression for bug (#5): videos should play without sound and the
// browser's volume slider should be absent from the preview element.
// Captive-portal signage isn't meant to blast audio at whoever walks by.

import { expect, test } from "@playwright/test";

test("video preview element is muted and hides the volume slider", async ({ page }) => {
    await page.goto("/#/slides/video");
    const video = page.locator(".video-upload-video");
    await expect(video).toBeVisible();

    // `muted` is an HTML boolean attribute — set at mount time via the
    // template. `toHaveJSProperty` reads the DOM property, which is the
    // reliable check for boolean attrs.
    await expect(video).toHaveJSProperty("muted", true);
    await expect(video).toHaveAttribute("controlslist", /novolumeslider/);

    // Double-check the DOM property too — a video element stays muted
    // across src swaps, which matters for the edit-existing flow.
    const muted = await video.evaluate((el) => el.muted);
    expect(muted).toBe(true);
});
