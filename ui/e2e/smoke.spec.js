import { expect, test } from "@playwright/test";
import { resetServerState } from "./_helpers.js";

test.beforeEach(() => {
    resetServerState();
});

test("app loads, editor + list visible, list shows empty state", async ({ page }) => {
    await page.goto("/");

    await expect(page).toHaveTitle("OpenMarquee");
    await expect(page.locator("header h1")).toHaveText("OpenMarquee");

    // Scope to `.editor` — `.field-save` also appears inside the image
    // uploader, and Playwright's strict mode flags the duplicate.
    await expect(page.locator(".editor .editor-canvas")).toBeVisible();
    await expect(page.locator(".editor .field-text")).toBeVisible();
    await expect(page.locator(".editor .field-save")).toBeVisible();

    await expect(page.locator(".image-upload")).toBeVisible();
    await expect(page.locator(".image-upload .field-file")).toBeVisible();

    await expect(page.locator(".list")).toBeVisible();
    await expect(page.locator(".list-status")).toContainText("No slides yet");
});

test("Save button is disabled until text is entered", async ({ page }) => {
    await page.goto("/");

    const saveBtn = page.locator(".editor .field-save");
    await expect(saveBtn).toBeDisabled();

    await page.locator(".editor .field-text").fill("Hi");
    await expect(saveBtn).toBeEnabled();

    await page.locator(".editor .field-text").fill("");
    await expect(saveBtn).toBeDisabled();
});

test("sidebar nav shows Slides by default and routes to each section on click", async ({ page }) => {
    await page.goto("/");

    // Slides is the default section. Sidebar link is marked active.
    await expect(page.locator('.panel[data-section="slides"]')).toBeVisible();
    await expect(page.locator('.nav-link[data-section="slides"]')).toHaveClass(/active/);
    await expect(page.locator('.panel[data-section="playlists"]')).toBeHidden();

    // Click through each section; only that panel should be visible.
    for (const name of ["playlists", "schedule", "settings", "slides"]) {
        await page.locator(`.nav-link[data-section="${name}"]`).click();
        await expect(page.locator(`.panel[data-section="${name}"]`)).toBeVisible();
        await expect(page.locator(`.nav-link[data-section="${name}"]`)).toHaveClass(/active/);
    }

    // Settings section renders an editable form hydrated from /api/settings.
    await page.locator('.nav-link[data-section="settings"]').click();
    await expect(page.locator(".settings-heading")).toHaveText("System settings");
    await expect(page.locator(".field-output-mode")).toHaveValue("hdmi");
    await expect(page.locator(".field-display-width")).toHaveValue("128");
});

test("welcome page renders SSID, password, and a real (not placeholder) QR", async ({ page }) => {
    // Phase 7 swaps in real values from the device side; this smoke test
    // ensures the page chrome stays wired up AND the welcome.js script
    // generates a real QR (not the no-JS placeholder fallback).
    await page.goto("/welcome.html");
    await expect(page).toHaveTitle(/OpenMarquee/);
    await expect(page.locator(".brand")).toHaveText("OpenMarquee");
    await expect(page.locator('[data-field="ssid"]')).toBeVisible();
    await expect(page.locator('[data-field="password"]')).toBeVisible();

    // The welcome.js script removes the qr-placeholder class once it has
    // rendered a real QR. If the script ran, the watermark is gone.
    await expect(page.locator(".qr")).not.toHaveClass(/qr-placeholder/);
    // And the SVG inside is the qrcode library's output, not the hand-drawn
    // placeholder pattern (different module count → different viewBox).
    const viewBox = await page.locator(".qr svg").getAttribute("viewBox");
    expect(viewBox).not.toBe("0 0 21 21");
});
