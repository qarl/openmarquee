// E2E: Settings secret-field rotation flow (Phase A.5).
//
// Each of the 3 secret fields (wifi_password / wifi_station_password /
// tailscale_auth_key) has a Change… inline form. The form drives a
// PATCH /api/settings/<endpoint> with current_password + new_value.
// The 20.4 wiring sets skipAuth401Redirect=true so a wrong-current-
// password 401 stays inline rather than bouncing to /login.html
// (which would be the wrong UX -- 401 here means "you typed the
// wrong current password," not "your bearer token is stale").
//
// Same OPENMARQUEE_DISABLE_AUTH=1 harness as the other auth specs:
// AuthMiddleware bypassed for /api/* in general so editor mounts
// cleanly, but the /api/auth/* + /api/settings/* endpoints under
// test still run real validators + AuthStorage round-trips.

import { expect, test } from "@playwright/test";
import { resetServerState } from "./_helpers.js";

const INITIAL_PASSWORD = "hunter2hunter";
const NEW_STATION_PASSWORD = "home-wifi-pass-1";

test.beforeEach(async ({ page }) => {
    resetServerState();
    await page.context().clearCookies();
    // Configure auth + stash token so the editor mounts directly.
    // Playwright auto-isolates localStorage between tests so no
    // paired removeItem init-script is needed.
    const resp = await page.request.post("/api/auth/set-password", {
        data: { password: INITIAL_PASSWORD, password_confirm: INITIAL_PASSWORD },
    });
    expect(resp.status()).toBe(200);
    const { token } = await resp.json();
    await page.addInitScript((t) => {
        try { localStorage.setItem("openmarquee_auth_token", t); } catch {}
    }, token);
});

test("wifi-station-password Change… → fill → Save → indicator updates", async ({ page }) => {
    await page.goto("/#/settings");
    const row = page.locator('.secret-field[data-secret="wifi-station-password"]');
    await expect(row).toBeVisible();
    // Pre-rotation: defaults have wifi_station_password=null -> "Not set".
    await expect(row.locator(".secret-status")).toContainText(/Not set/i);

    await row.locator(".secret-change-btn").click();
    await expect(row.locator(".secret-form")).toBeVisible();

    await row.locator(".secret-current-password").fill(INITIAL_PASSWORD);
    await row.locator(".secret-new-value").fill(NEW_STATION_PASSWORD);
    await row.locator(".secret-save-btn").click();

    // Form collapses + indicator updates to "Set".
    await expect(row.locator(".secret-form")).toBeHidden({ timeout: 5000 });
    await expect(row.locator(".secret-status")).toContainText(/Set/i);
});

test("wifi-station-password Change… with wrong current_password: 401 inline, no redirect", async ({ page }) => {
    await page.goto("/#/settings");
    const row = page.locator('.secret-field[data-secret="wifi-station-password"]');
    await row.locator(".secret-change-btn").click();
    await row.locator(".secret-current-password").fill("wrong-current-password");
    await row.locator(".secret-new-value").fill(NEW_STATION_PASSWORD);
    await row.locator(".secret-save-btn").click();

    await expect(row.locator(".secret-error")).toContainText(/Incorrect current password/i);
    // Still on settings; form stays open so the operator can retry.
    await expect(row.locator(".secret-form")).toBeVisible();
    await expect(page).toHaveURL(/\/(\?|#|$)/);
});

test("wifi-ap-password Change… rejects empty new_value (422 from backend)", async ({ page }) => {
    await page.goto("/#/settings");
    const row = page.locator('.secret-field[data-secret="wifi-ap-password"]');
    await row.locator(".secret-change-btn").click();
    await row.locator(".secret-current-password").fill(INITIAL_PASSWORD);
    // Leave new_value empty -- the AP password can't be cleared.
    await row.locator(".secret-save-btn").click();

    // Backend returns 422 with the explicit "wifi_password cannot be
    // empty" detail message. The handler surfaces detail inline.
    await expect(row.locator(".secret-error")).toContainText(
        /wifi_password cannot be empty|HTTP 422/i,
    );
});

test("tailscale-auth-key Change… → Save then Cancel re-open clears form", async ({ page }) => {
    await page.goto("/#/settings");
    const row = page.locator('.secret-field[data-secret="tailscale-auth-key"]');
    await expect(row.locator(".secret-status")).toContainText(/Not set/i);

    await row.locator(".secret-change-btn").click();
    await row.locator(".secret-current-password").fill(INITIAL_PASSWORD);
    await row.locator(".secret-new-value").fill("tskey-auth-aaaaaaaaaaaaaaaaaa");
    await row.locator(".secret-save-btn").click();
    await expect(row.locator(".secret-form")).toBeHidden({ timeout: 5000 });
    await expect(row.locator(".secret-status")).toContainText(/Set/i);

    // Re-open + cancel: inputs are cleared, form collapses.
    await row.locator(".secret-change-btn").click();
    await row.locator(".secret-current-password").fill("partial-typing");
    await row.locator(".secret-cancel-btn").click();
    await expect(row.locator(".secret-form")).toBeHidden();
    // Next open() starts clean.
    await row.locator(".secret-change-btn").click();
    await expect(row.locator(".secret-current-password")).toHaveValue("");
});
