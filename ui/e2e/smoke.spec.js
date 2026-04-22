import { expect, test } from "@playwright/test";
import { resetServerState } from "./_helpers.js";

test.beforeEach(() => {
    resetServerState();
});

test("app loads on the Text subpage with the text editor visible", async ({ page }) => {
    await page.goto("/");

    await expect(page).toHaveTitle("openMarquee");
    await expect(page.locator("header h1")).toHaveText("openMarquee");

    await expect(page.locator(".editor .editor-canvas")).toBeVisible();
    await expect(page.locator(".editor .field-text")).toBeVisible();
    await expect(page.locator(".editor .field-save")).toBeVisible();

    // Image uploader lives on its own subpage — hidden on boot.
    await expect(page.locator(".image-upload")).toBeHidden();
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

test("sidebar nav defaults to Text and routes through every section on click", async ({ page }) => {
    await page.goto("/");

    // Text is the default section.
    await expect(page.locator('.panel[data-section="slides/text"]')).toBeVisible();
    await expect(page.locator('.nav-link[data-section="slides/text"]')).toHaveClass(/active/);
    await expect(page.locator('.panel[data-section="slides/image"]')).toBeHidden();

    // Click through every section; only that panel should be visible at a time.
    const routes = [
        "slides/image",
        "slides/video",
        "playlists",
        "schedule",
        "settings",
        "slides/text",
    ];
    for (const name of routes) {
        await page.locator(`.nav-link[data-section="${name}"]`).click();
        await expect(page.locator(`.panel[data-section="${name}"]`)).toBeVisible();
        await expect(page.locator(`.nav-link[data-section="${name}"]`)).toHaveClass(/active/);
    }

    // Settings section renders an editable form hydrated from /api/settings.
    await page.locator('.nav-link[data-section="settings"]').click();
    await expect(
        page.locator('section.settings .subpage-title'),
    ).toHaveText("System settings");
    await expect(page.locator(".field-output-mode")).toHaveValue("hdmi");
    await expect(page.locator(".field-display-width")).toHaveValue("128");
});

test("ffmpeg.wasm spike page renders both pipeline buttons + the file picker", async ({ page }) => {
    await page.goto("/spike.html");
    await expect(page).toHaveTitle(/ffmpeg\.wasm spike/);
    await expect(page.locator("#run-h264")).toBeVisible();
    await expect(page.locator("#run-rgb")).toBeVisible();
    await expect(page.locator("#source-file")).toBeVisible();
    // Initial status line says "ready." after boot() fires.
    await expect(page.locator("#spike-status")).toContainText("ready");
});

test("spike page serves the bundled ffmpeg worker + vendored core assets", async ({ page }) => {
    // Worker and core files must exist on the captive portal — otherwise
    // the first click on a pipeline button 404s and the operator sees an
    // ffmpeg.wasm init error. Regression guard for the esbuild worker-
    // entry-point bundling step.
    const worker = await page.request.get("/dist/ffmpeg-worker.js");
    expect(worker.status()).toBe(200);
    const coreJs = await page.request.get("/dist/vendor/ffmpeg-core/ffmpeg-core.js");
    expect(coreJs.status()).toBe(200);
    const coreWasm = await page.request.head("/dist/vendor/ffmpeg-core/ffmpeg-core.wasm");
    expect(coreWasm.status()).toBe(200);
});

test("welcome page renders SSID, password, and a real (not placeholder) QR", async ({ page }) => {
    // Phase 7 swaps in real values from the device side; this smoke test
    // ensures the page chrome stays wired up AND the welcome.js script
    // generates a real QR (not the no-JS placeholder fallback).
    await page.goto("/welcome.html");
    await expect(page).toHaveTitle(/openMarquee/);
    await expect(page.locator(".brand")).toHaveText("openMarquee");
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
