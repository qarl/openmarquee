// E2E: change-password flow + token-version-bump invalidation (Phase A.5).
//
// Walks the operator through rotating their login password from the
// Settings panel's "Operator login" card. Verifies:
//   - happy path (current + new + confirm -> 200, new token stashed)
//   - wrong current_password surfaces inline (no /login redirect)
//   - new-password-mismatch surfaces inline before the round-trip
//   - the pre-rotation token bumps to invalid: a manually-captured
//     stale token returns 401 from /api/auth/change-password if reused
//
// E2E harness runs with OPENMARQUEE_DISABLE_AUTH=1 so the editor's
// other API surfaces don't 401 on token absence; the auth endpoints
// themselves still run against real AuthStorage. The token-bump
// invalidation test exercises that via direct request.fetch with an
// explicit Authorization header rather than relying on the
// middleware-engaged path.

import { expect, test } from "@playwright/test";
import { resetServerState } from "./_helpers.js";

const INITIAL_PASSWORD = "hunter2hunter";
const NEW_PASSWORD = "freshpassword1";

test.beforeEach(async ({ page }) => {
    resetServerState();
    await page.context().clearCookies();
    // Stamp initial password via API.
    const resp = await page.request.post("/api/auth/set-password", {
        data: { password: INITIAL_PASSWORD, password_confirm: INITIAL_PASSWORD },
    });
    expect(resp.status()).toBe(200);
    const { token } = await resp.json();
    // Pre-stash the token via init-script so main.js boots straight to
    // the editor (otherwise it'd redirect to /login.html on a
    // configured device without a token). Playwright auto-isolates
    // localStorage between tests, so we don't need a paired remove.
    await page.addInitScript((t) => {
        try { localStorage.setItem("openmarquee_auth_token", t); } catch {}
    }, token);
});

test("change-password happy path: form submits + stores new token", async ({ page }) => {
    await page.goto("/#/settings");
    await expect(page.locator(".settings-operator-password")).toBeVisible();

    // Open the change-password form.
    await page.locator(".change-pw-btn").click();
    await expect(page.locator(".change-pw-form")).toBeVisible();

    await page.fill(".change-pw-current", INITIAL_PASSWORD);
    await page.fill(".change-pw-new", NEW_PASSWORD);
    await page.fill(".change-pw-confirm", NEW_PASSWORD);
    await page.locator(".change-pw-save").click();

    // Form collapses (success).
    await expect(page.locator(".change-pw-form")).toBeHidden({ timeout: 5000 });
    await expect(page.locator(".settings-status")).toContainText(/Password changed/i);

    // New token in localStorage; token_version bumped to 2 so prefix changes.
    const newToken = await page.evaluate(() =>
        localStorage.getItem("openmarquee_auth_token"),
    );
    expect(newToken).toMatch(/^2\./);
});

test("change-password with wrong current_password: 401 inline, no redirect", async ({ page }) => {
    await page.goto("/#/settings");
    await page.locator(".change-pw-btn").click();
    await page.fill(".change-pw-current", "obviously-wrong-pw");
    await page.fill(".change-pw-new", NEW_PASSWORD);
    await page.fill(".change-pw-confirm", NEW_PASSWORD);
    await page.locator(".change-pw-save").click();

    await expect(page.locator(".change-pw-error")).toContainText(/Incorrect current password/i);
    // Form stays open, settings page didn't redirect (the
    // skipAuth401Redirect option on apiFetch keeps the operator
    // on /settings).
    await expect(page.locator(".change-pw-form")).toBeVisible();
    await expect(page).toHaveURL(/\/(\?|#|$)/);
});

test("change-password: new+confirm mismatch surfaces inline before round-trip", async ({ page }) => {
    await page.goto("/#/settings");
    await page.locator(".change-pw-btn").click();
    await page.fill(".change-pw-current", INITIAL_PASSWORD);
    await page.fill(".change-pw-new", NEW_PASSWORD);
    await page.fill(".change-pw-confirm", "different-from-new");
    await page.locator(".change-pw-save").click();

    await expect(page.locator(".change-pw-error")).toContainText(/don't match/i);
    // Token unchanged (no round-trip fired).
    const token = await page.evaluate(() =>
        localStorage.getItem("openmarquee_auth_token"),
    );
    expect(token).toMatch(/^1\./);
});

test("token-version bump: stale token returns 401 after change-password", async ({ page, request }) => {
    // Navigate first so the page has a real origin -- page.evaluate
    // can't access localStorage on about:blank.
    await page.goto("/#/settings");
    // Snapshot the pre-rotation token.
    const staleToken = await page.evaluate(() =>
        localStorage.getItem("openmarquee_auth_token"),
    );
    expect(staleToken).toMatch(/^1\./);

    await page.locator(".change-pw-btn").click();
    await page.fill(".change-pw-current", INITIAL_PASSWORD);
    await page.fill(".change-pw-new", NEW_PASSWORD);
    await page.fill(".change-pw-confirm", NEW_PASSWORD);
    await page.locator(".change-pw-save").click();
    await expect(page.locator(".change-pw-form")).toBeHidden({ timeout: 5000 });

    // Now hit /api/auth/change-password directly with the OLD token.
    // Should 401 because token_version is now 2; the stale token's
    // version prefix doesn't match.
    const replay = await request.post("/api/auth/change-password", {
        headers: { Authorization: `Bearer ${staleToken}` },
        data: {
            current_password: NEW_PASSWORD,
            new_password: "another-fresh-1",
            new_password_confirm: "another-fresh-1",
        },
    });
    expect(replay.status()).toBe(401);
});

test("change-password Cancel collapses form without saving", async ({ page }) => {
    await page.goto("/#/settings");
    await page.locator(".change-pw-btn").click();
    await page.fill(".change-pw-current", INITIAL_PASSWORD);
    await page.fill(".change-pw-new", NEW_PASSWORD);
    await page.fill(".change-pw-confirm", NEW_PASSWORD);
    await page.locator(".change-pw-cancel").click();

    await expect(page.locator(".change-pw-form")).toBeHidden();
    // Token unchanged (token_version still 1).
    const token = await page.evaluate(() =>
        localStorage.getItem("openmarquee_auth_token"),
    );
    expect(token).toMatch(/^1\./);
});
