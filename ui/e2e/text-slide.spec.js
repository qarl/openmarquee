import { expect, test } from "@playwright/test";
import { resetServerState } from "./_helpers.js";

test.beforeEach(() => {
    resetServerState();
});

test("end-to-end: save → list → play → delete", async ({ page }) => {
    await page.goto("/");

    // Type a slide.
    await page.locator(".field-name").fill("Opening");
    await page.locator(".field-text").fill("GRAND OPENING");
    await page.locator(".field-text-color").evaluate((el) => {
        el.value = "#ffffff";
        el.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await page.locator(".field-bg-color").evaluate((el) => {
        el.value = "#cc0000";
        el.dispatchEvent(new Event("input", { bubbles: true }));
    });

    // Save.
    await page.locator(".field-save").click();
    await expect(page.locator(".editor-status")).toHaveText("Saved.");

    // The new slide shows in the list.
    await expect(page.locator(".slide")).toHaveCount(1);
    await expect(page.locator(".slide-name").first()).toHaveText("Opening");
    await expect(page.locator(".slide-text").first()).toHaveText("GRAND OPENING");

    // The thumbnail loads.
    const thumb = page.locator(".slide-thumb").first();
    await expect(thumb).toBeVisible();
    const thumbResp = await page.request.get(await thumb.getAttribute("src"));
    expect(thumbResp.status()).toBe(200);
    expect(thumbResp.headers()["content-type"]).toBe("image/png");

    // Click Play; the preview frame should now exist on the backend.
    await page.locator(".slide button", { hasText: "Play" }).click();
    // Wait for the play request to land before checking the preview.
    await expect
        .poll(async () => {
            const preview = await page.request.get("/dev/preview/frame.png");
            return preview.status();
        })
        .toBe(200);
    const preview = await page.request.get("/dev/preview/frame.png");
    expect(preview.headers()["content-type"]).toBe("image/png");
    const previewBody = await preview.body();
    expect(previewBody.length).toBeGreaterThan(0);
    // PNG magic number — confirms the bytes are an actual image, not an
    // empty file or HTML 404 page.
    expect(previewBody.slice(0, 8)).toEqual(
        Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    );

    // Click Delete; the slide disappears.
    await page.locator(".slide button", { hasText: "Delete" }).click();
    await expect(page.locator(".slide")).toHaveCount(0);
    await expect(page.locator(".list-status")).toContainText("No slides yet");
});

test("two slides can coexist, both retrievable", async ({ page }) => {
    await page.goto("/");

    for (const name of ["Open", "Closed"]) {
        await page.locator(".field-name").fill(name);
        await page.locator(".field-text").fill(name.toUpperCase());
        await page.locator(".field-save").click();
        await expect(page.locator(".editor-status")).toHaveText("Saved.");
    }

    await expect(page.locator(".slide")).toHaveCount(2);
    const names = await page.locator(".slide-name").allTextContents();
    expect(new Set(names)).toEqual(new Set(["Open", "Closed"]));
});

test("rejected save (text too long) surfaces the error", async ({ page }) => {
    await page.goto("/");
    await page.locator(".field-name").fill("Big");
    await page.locator(".field-text").fill("X".repeat(10_001));
    await page.locator(".field-save").click();
    await expect(page.locator(".editor-status")).toContainText("Save failed");
});
