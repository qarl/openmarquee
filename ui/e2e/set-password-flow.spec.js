// E2E: first-boot welcome → set-password → editor.
//
// Walks the operator's-phone first-boot path end-to-end. The webServer
// runs with OPENMARQUEE_DISABLE_AUTH=1 so the bearer-token gate
// short-circuits even after set-password lands -- the e2e harness
// doesn't need to thread tokens through every later request.
// resetServerState() clears auth.json each test, so /api/auth/status
// answers configured=false on first load.

import { expect, test } from "@playwright/test";
import { resetFirstRun, resetServerState } from "./_helpers.js";

test.beforeEach(async ({ page }) => {
    resetServerState();
    resetFirstRun();
    await page.context().clearCookies();
    // No localStorage.removeItem init-script here -- Playwright
    // contexts auto-isolate localStorage between tests, and a
    // removeItem init-script fires on EVERY page load (including the
    // post-set-password navigation to /), wiping the freshly-stored
    // token before main.js's auth gate reads it (caught 2026-05-11
    // during 20.5 playwright run).
});

test("first-boot welcome → set-password → editor", async ({ page }) => {
    await page.goto("/");

    // First-run welcome card mounts because ui_first_run_seen is unset.
    await expect(page.locator(".first-run-continue")).toBeVisible();
    await expect(page.locator(".om-welcome")).toContainText(/Your sign is on/i);

    await page.locator(".first-run-continue").click();

    // The onContinue handler saves ui_first_run_seen + redirects to
    // /set-password.html since /api/auth/status returned configured=false.
    await expect(page).toHaveURL(/\/set-password\.html$/);
    await expect(page.locator("h2#title")).toHaveText("Set a password");

    // Submit disabled until both fields are filled + match + meet min length.
    const submit = page.locator('button[type="submit"]');
    await expect(submit).toBeDisabled();

    await page.fill('input[name="password"]', "hunter2hunter");
    await expect(submit).toBeDisabled(); // confirm still empty

    await page.fill('input[name="password_confirm"]', "hunter2DIFFERS");
    await expect(submit).toBeDisabled(); // mismatch

    // Mismatch hint shows live.
    await expect(page.locator('[data-field-hint="password_confirm"]'))
        .toContainText(/don't match/i);

    // Fix the confirm — submit enables.
    await page.fill('input[name="password_confirm"]', "hunter2hunter");
    await expect(submit).toBeEnabled();

    await submit.click();

    // The set-password handler stashes the token + bounces to /.
    await expect(page).toHaveURL(/\/(\?|#|$)/);
    // Editor chrome rendered (post-first-run path).
    await expect(page.locator(".om-wordmark")).toBeVisible();

    // The token survived into localStorage.
    const token = await page.evaluate(() => localStorage.getItem("openmarquee_auth_token"));
    expect(token).toMatch(/^1\./);

    // The backend now reports configured=true.
    const status = await page.request.get("/api/auth/status");
    const body = await status.json();
    expect(body.configured).toBe(true);
});

test("set-password redirects to /login.html on 409 (already configured)", async ({ page }) => {
    // Pre-configure auth.json by POSTing directly.
    const setup = await page.request.post("/api/auth/set-password", {
        data: { password: "preset-password-1", password_confirm: "preset-password-1" },
    });
    expect(setup.status()).toBe(200);

    await page.goto("/set-password.html");
    await page.fill('input[name="password"]', "ignored-attempt-1");
    await page.fill('input[name="password_confirm"]', "ignored-attempt-1");
    await page.locator('button[type="submit"]').click();

    // 409 → bounce to /login.html
    await expect(page).toHaveURL(/\/login\.html$/);
});
