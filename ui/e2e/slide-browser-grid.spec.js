// Regression for bug list: "slide selection should be a grid like in
// playlists, not a side scrolling list." Assert the slide-browser
// container on each slide subpage uses CSS Grid — the old flex-row
// `overflow-x: auto` layout forced the operator into horizontal
// scrolling on tall viewports.

import { expect, test } from "@playwright/test";
import { resetServerState } from "./_helpers.js";

test.beforeEach(() => {
    resetServerState();
});

test("text-slide browser list uses grid layout", async ({ page }) => {
    await page.goto("/#/slides/text");
    const display = await page.locator(".editor .slide-browser-list").evaluate(
        (el) => getComputedStyle(el).display,
    );
    expect(display).toBe("grid");
});

test("image-slide browser list uses grid layout", async ({ page }) => {
    await page.goto("/#/slides/image");
    const display = await page.locator(".image-upload .slide-browser-list").evaluate(
        (el) => getComputedStyle(el).display,
    );
    expect(display).toBe("grid");
});

test("video-slide browser list uses grid layout", async ({ page }) => {
    await page.goto("/#/slides/video");
    const display = await page.locator(".video-upload .slide-browser-list").evaluate(
        (el) => getComputedStyle(el).display,
    );
    expect(display).toBe("grid");
});
