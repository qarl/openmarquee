// Welcome screen at first-boot display size (1920×1080). Verifies the
// polished layout: big QR, monospace credentials with copy buttons,
// headline instruction, branding. Also lays down a screenshot at the
// HDMI target size for visual inspection in CI artifacts.

import { expect, test } from "@playwright/test";

test.use({ viewport: { width: 1920, height: 1080 } });

test("welcome page lays out correctly at 1920×1080 and is screenshot-able", async ({ page }) => {
    await page.goto("/welcome.html");

    // Brand: monogram + wordmark at the top.
    await expect(page.locator(".brand-mark")).toBeVisible();
    await expect(page.locator(".brand-mark")).toHaveText("oM");
    await expect(page.locator(".brand-word")).toHaveText("openMarquee");

    // Headline instruction — what the user must do.
    await expect(page.locator(".welcome-instruction")).toContainText(
        /Connect to this WiFi/i,
    );
    await expect(page.locator(".welcome-instruction strong")).toHaveText(
        "setup page",
    );

    // QR is swapped in by welcome.js (no .qr-placeholder class remains).
    await expect(page.locator(".welcome-qr")).toBeVisible();
    await expect(page.locator(".welcome-qr")).not.toHaveClass(/qr-placeholder/);

    // At 1920×1080 the QR should occupy ~560px (clamp upper bound). A
    // tight bound catches layout regressions (bad flex/grid collapse)
    // without being pixel-exact.
    const qrBox = await page.locator(".welcome-qr").boundingBox();
    expect(qrBox).not.toBeNull();
    expect(qrBox.width).toBeGreaterThan(500);
    expect(qrBox.width).toBeLessThan(620);

    // Caption under the QR.
    await expect(page.locator(".welcome-qr-caption")).toHaveText(/Scan to join/i);

    // Credentials: both rows with monospace values + copy buttons.
    const ssidRow = page.locator('.creds-row:has([data-field="ssid"])');
    await expect(ssidRow.locator(".creds-value")).toBeVisible();
    await expect(ssidRow.locator(".copy-btn")).toHaveText("Copy");
    const pwRow = page.locator('.creds-row:has([data-field="password"])');
    await expect(pwRow.locator(".creds-value")).toBeVisible();
    await expect(pwRow.locator(".copy-btn")).toHaveText("Copy");

    // Footer with fallback hostname.
    await expect(page.locator(".welcome-footer code")).toHaveText(
        "http://openmarquee.local",
    );

    // Drop a screenshot so a visual regression shows up in the test
    // artifact bundle — reviewers can eyeball it alongside the code diff.
    await page.screenshot({
        path: "test-results/welcome-1080p.png",
        fullPage: false,
    });
});

test("Copy button on credential row gives user feedback when clicked", async ({ page, context }) => {
    // Grant clipboard perms so navigator.clipboard.writeText is callable
    // under Playwright's HTTP test origin.
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.goto("/welcome.html");
    const btn = page.locator('[data-copy-for="password"]');
    await btn.click();
    // Whether the native API or the execCommand fallback ran, the
    // button must visibly update — "Copied" on success, "Select" on the
    // select-fallback path. Playwright retries the assertion, so the
    // async copy has time to resolve without a hand-rolled sleep.
    await expect(btn).toHaveText(/Copied|Select/);
});
