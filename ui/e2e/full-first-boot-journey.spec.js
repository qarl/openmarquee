// E2E: complete first-boot operator journey (Phase A.5).
//
// Walks the operator from a freshly-flashed device through every gate
// in the Phase A auth shell:
//
//   reset → / → first-run welcome card → "Make it mine"
//        → /set-password.html → fill + submit
//        → / (editor with bearer token in localStorage)
//        → simulate fresh browser (clear token)
//        → / → /login.html (no-token redirect)
//        → enter the same password → submit
//        → / (editor)
//
// The harness still runs with OPENMARQUEE_DISABLE_AUTH=1 so the
// AuthMiddleware bypass is engaged for the rest of /api/*. The
// /api/auth/* endpoints themselves are unaffected -- the gate just
// doesn't enforce on /api/content / /api/settings / etc., which would
// otherwise 401 every editor-mount fetch. The 20.5 e2e specs are
// proving the UI flow + endpoint correctness, not the
// middleware-engaged production path (that's covered by
// test_auth_middleware integration tests on the backend).

import { expect, test } from "@playwright/test";
import { resetFirstRun, resetServerState } from "./_helpers.js";

const OPERATOR_PASSWORD = "hunter2hunter";

test.beforeEach(async ({ page }) => {
    resetServerState();
    resetFirstRun();
    await page.context().clearCookies();
    // No addInitScript localStorage.remove here -- Playwright contexts
    // already isolate localStorage between tests, and a removeItem
    // init-script would fire on EVERY page load (including the
    // post-set-password navigation to /), wiping the freshly-stored
    // token before main.js's auth gate reads it.
});

test("first-boot → set-password → editor → fresh session → login → editor", async ({ page }) => {
    // 1. Fresh boot lands on the first-run welcome card.
    await page.goto("/");
    await expect(page.locator(".first-run-continue")).toBeVisible();
    await expect(page.locator(".om-welcome")).toContainText(/Your sign is on/i);

    // 2. "Make it mine" routes to set-password.html.
    await page.locator(".first-run-continue").click();
    await expect(page).toHaveURL(/\/set-password\.html$/);

    // 3. Fill the 2-field set-password form + submit.
    await page.fill('input[name="password"]', OPERATOR_PASSWORD);
    await page.fill('input[name="password_confirm"]', OPERATOR_PASSWORD);
    const setSubmit = page.locator('button[type="submit"]');
    await expect(setSubmit).toBeEnabled();
    await setSubmit.click();

    // 4. Lands on the editor; token is in localStorage.
    await expect(page).toHaveURL(/\/(\?|#|$)/);
    await expect(page.locator(".om-wordmark")).toBeVisible();
    const tokenAfterSet = await page.evaluate(() =>
        localStorage.getItem("openmarquee_auth_token"),
    );
    expect(tokenAfterSet).toMatch(/^1\./);

    // 5. Simulate a fresh browser session: clear localStorage. The
    // device's stored AuthState is still configured -- only this
    // client's token is gone. main.js boot should redirect to
    // /login.html.
    await page.evaluate(() => localStorage.clear());
    await page.goto("/");
    await expect(page).toHaveURL(/\/login\.html$/);

    // 6. Log in with the same password.
    await page.fill('input[name="password"]', OPERATOR_PASSWORD);
    await page.locator('button[type="submit"]').click();
    await expect(page).toHaveURL(/\/(\?|#|$)/);
    await expect(page.locator(".om-wordmark")).toBeVisible();
    const tokenAfterLogin = await page.evaluate(() =>
        localStorage.getItem("openmarquee_auth_token"),
    );
    expect(tokenAfterLogin).toMatch(/^1\./);
});
