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

test("welcome wireframe page renders with SSID, password, and QR placeholder", async ({ page }) => {
    // The wireframe ships at /welcome.html. Phase 7 swaps in real values
    // and a real backend-rendered QR code; this smoke test ensures the
    // wireframe stays wired up and shows the expected chrome.
    await page.goto("/welcome.html");
    await expect(page).toHaveTitle(/OpenMarquee/);
    await expect(page.locator(".brand")).toHaveText("OpenMarquee");
    await expect(page.locator('[data-field="ssid"]')).toBeVisible();
    await expect(page.locator('[data-field="password"]')).toBeVisible();
    await expect(page.locator(".qr svg")).toBeVisible();
});
