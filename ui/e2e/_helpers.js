// Shared helpers for e2e specs. Mostly: per-test isolation by wiping the
// content the webServer is configured against, while leaving the root dir
// itself in place (the backend tolerates a missing root, but keeping it
// keeps test logs cleaner).

import { mkdirSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { expect } from "@playwright/test";
import {
    E2E_AUTH_PATH,
    E2E_CONTENT_ROOT,
    E2E_PLAYLIST_PATH,
    E2E_PREVIEW_PATH,
    E2E_SCHEDULE_PATH,
    E2E_SEED_MARKER_PATH,
    E2E_SETTINGS_PATH,
} from "../playwright.config.js";

export function resetServerState() {
    mkdirSync(E2E_CONTENT_ROOT, { recursive: true });
    for (const entry of readdirSync(E2E_CONTENT_ROOT)) {
        rmSync(path.join(E2E_CONTENT_ROOT, entry), { recursive: true, force: true });
    }
    rmSync(E2E_PREVIEW_PATH, { force: true });
    rmSync(E2E_PLAYLIST_PATH, { force: true });
    rmSync(E2E_SCHEDULE_PATH, { force: true });
    rmSync(E2E_SETTINGS_PATH, { force: true });
    // The seed-marker is cleared between tests too, but seeding runs at
    // startup (lifespan), not per-request — so resetServerState() alone
    // doesn't re-seed a running server. Each test that depends on empty
    // state uses resetServerState() immediately, then polls /api/content
    // to observe whatever state the server had when the test began. A
    // dev-only "reseed" endpoint is future work; for now the seed landing
    // is verified by test_seed.py + the smoke e2e below.
    rmSync(E2E_SEED_MARKER_PATH, { force: true });
    // Batch 20.2: clear the per-run auth.json so each spec starts in
    // the "auth not configured" state by default. Specs that need a
    // configured device call `configureAuthInline()` before navigating.
    rmSync(E2E_AUTH_PATH, { force: true });
    // Pre-seed the captive-portal first-run gate as already-dismissed so
    // the SPA boots straight to the editor instead of the welcome CTA.
    // SystemSettings backfills every other field from its model defaults
    // when only this key is present. Specs that exercise the welcome
    // flow itself can call `resetFirstRun()` after this.
    writeFileSync(E2E_SETTINGS_PATH, JSON.stringify({ ui_first_run_seen: true }));
}

// Inverse of the seeding done at the bottom of `resetServerState()` — drops
// the settings file entirely so the next page-load hits the first-run welcome
// path. Use this in specs that test the welcome gate.
export function resetFirstRun() {
    rmSync(E2E_SETTINGS_PATH, { force: true });
}

// Save a text slide via the editor and return when autosave has confirmed.
// Replaces the per-spec inline helper that used to click `.editor .field-save`
// (the explicit Save button was removed when auto-save shipped — see
// AGENTS.md "Known stale e2e tests"). The 900ms editor debounce means callers
// should not race a follow-up assertion before this resolves.
//
// **Mount assumption** (load-bearing): the SPA keeps panels mounted across
// hash navigation (see `main.js`'s "panels stay mounted" comment), so
// `page.goto("/#/slides/text")` after a prior `clickNewSlide()` does NOT
// re-mount the editor or reset `editingId`. That's how the
// `clickNewSlide(); saveTextSlide(...)` sequence threads correctly: the
// fresh placeholder created by clickNewSlide gets PATCHed by autosave
// instead of producing a second slide. If a future refactor re-mounts the
// editor on hash change, callers will quietly start creating extra slides;
// flag at that point.
//
// @param {import('@playwright/test').Page} page
// @param {string} name — slide name (also used as the body text by default)
// @param {{text?: string}} [opts]
export async function saveTextSlide(page, name, opts = {}) {
    const text = opts.text ?? name;
    await page.goto("/#/slides/text");
    // Editor mount has an async tail that sets `field-name` to "Text Slide N";
    // wait for that before our fill so our value isn't clobbered.
    await expect(page.locator(".editor .field-name")).toHaveValue(/Text Slide \d+/);
    await page.fill(".editor .field-name", name);
    await page.fill(".editor .field-text", text);
    // 900ms debounce + network round-trip + fade window; 5s ceiling is plenty.
    await expect(page.locator(".editor .editor-status"))
        .toContainText(/Saved/, { timeout: 5_000 });
}

// Click the +New affordance in the slides shell page-head to reset the editor
// to a blank, ready-for-a-new-slide state. Replaces the legacy
// `.slide-browser-tile--new .slide-browser-tile-action` click — the +New
// tile inside the slide-browser is gone; the button now lives in the shell
// header next to the tab subnav.
export async function clickNewSlide(page) {
    await page.locator(".slides-shell-new").click();
    // resetToBlank() runs an async tail that fills `field-name` with the
    // next "Text Slide N"; wait for it so callers' subsequent fill doesn't
    // race the auto-name.
    await expect(page.locator(".editor .field-name")).toHaveValue(/Text Slide \d+/);
}
