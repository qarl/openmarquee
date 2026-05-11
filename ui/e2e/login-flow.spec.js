// E2E: returning-operator login flow.
//
// Pre-configures auth.json via /api/auth/set-password (no UI involved),
// then drives the operator from /login.html → editor with the correct
// password + the 401 error path + the 404 redirect to /set-password.html.
//
// Same OPENMARQUEE_DISABLE_AUTH=1 harness rationale as set-password-flow:
// the bearer-token gate is bypassed for the rest of the e2e suite, but
// the /api/auth/* endpoints themselves are real and exercise the
// AuthStorage just like a production unit.

import { expect, test } from "@playwright/test";
import { resetServerState } from "./_helpers.js";

const PRESET_PASSWORD = "hunter2hunter";

test.beforeEach(async ({ page }) => {
    resetServerState();
    await page.context().clearCookies();
    // Stamp a password directly into the backend so /login.html has
    // something to authenticate against. resetServerState() cleared
    // E2E_AUTH_PATH; /api/auth/set-password lands it fresh.
    // No addInitScript localStorage.removeItem -- Playwright auto-
    // isolates contexts, and a per-load removeItem would wipe the
    // token between /login.html success + the redirect to /.
    const resp = await page.request.post("/api/auth/set-password", {
        data: { password: PRESET_PASSWORD, password_confirm: PRESET_PASSWORD },
    });
    expect(resp.status()).toBe(200);
});

test("returning operator: /login → correct password → editor", async ({ page }) => {
    // Main.js boot would normally bounce /login.html here too because
    // auth.configured && no token, but landing directly avoids the
    // dependency on the boot's redirect chain in this test.
    await page.goto("/login.html");
    await expect(page.locator("h2#title")).toHaveText("Sign in");

    const submit = page.locator('button[type="submit"]');
    await expect(submit).toBeDisabled();
    await page.fill('input[name="password"]', PRESET_PASSWORD);
    await expect(submit).toBeEnabled();
    await submit.click();

    await expect(page).toHaveURL(/\/(\?|#|$)/);
    await expect(page.locator(".om-wordmark")).toBeVisible();
    const token = await page.evaluate(() => localStorage.getItem("openmarquee_auth_token"));
    expect(token).toMatch(/^1\./);
});

test("wrong password shows 'Incorrect password.' inline + leaves submit re-enabled", async ({ page }) => {
    await page.goto("/login.html");
    await page.fill('input[name="password"]', "obviously-wrong-pw");
    await page.locator('button[type="submit"]').click();

    await expect(page.locator("[data-form-error]")).toHaveText("Incorrect password.");
    // Still on /login.html (no redirect happened).
    await expect(page).toHaveURL(/\/login\.html$/);
    // No token landed.
    const token = await page.evaluate(() => localStorage.getItem("openmarquee_auth_token"));
    expect(token).toBeNull();
    // Submit re-enables so the operator can retry.
    await expect(page.locator('button[type="submit"]')).toBeEnabled();
});

test("/login.html on an unconfigured device → /set-password.html", async ({ page }) => {
    // Wipe the auth.json that beforeEach just stamped.
    resetServerState();

    await page.goto("/login.html");
    await page.fill('input[name="password"]', "anything");
    await page.locator('button[type="submit"]').click();

    // 404 from /api/auth/login → bounce to /set-password.html
    await expect(page).toHaveURL(/\/set-password\.html$/);
});

test("main.js boot: configured + no token → redirect to /login.html", async ({ page }) => {
    // ui_first_run_seen=true (resetServerState default) + no localStorage
    // token + auth.configured → boot redirects to /login.html.
    await page.goto("/");
    await expect(page).toHaveURL(/\/login\.html$/);
    await expect(page.locator("h2#title")).toHaveText("Sign in");
});
