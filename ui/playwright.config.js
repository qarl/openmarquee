// Playwright config for OpenMarquee end-to-end tests.
//
// Auto-starts uvicorn on port 8765 with isolated content/preview paths in
// /tmp so e2e doesn't pollute dev data. Picks up the venv'd uvicorn via
// OPENMARQUEE_VENV (matches scripts/test.sh + scripts/dev.sh) and falls
// back to PATH for CI.

import { existsSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "@playwright/test";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const venvBase = process.env.OPENMARQUEE_VENV || path.join(os.homedir(), "tmp/venv/openmarquee");
const venvUvicorn = path.join(venvBase, "bin/uvicorn");
const uvicornCmd = existsSync(venvUvicorn) ? venvUvicorn : "uvicorn";

export const E2E_CONTENT_ROOT = "/tmp/openmarquee-e2e-content";
export const E2E_PREVIEW_PATH = "/tmp/openmarquee-e2e-preview.png";
export const E2E_PLAYLIST_PATH = "/tmp/openmarquee-e2e-playlist.json";
export const E2E_SCHEDULE_PATH = "/tmp/openmarquee-e2e-schedules.json";
export const E2E_SETTINGS_PATH = "/tmp/openmarquee-e2e-settings.json";
export const E2E_SEED_MARKER_PATH = "/tmp/openmarquee-e2e-seeded.json";

export default defineConfig({
    testDir: "./e2e",
    // Single backend instance — keep tests sequential to avoid filesystem races
    // on the shared content root.
    fullyParallel: false,
    workers: 1,
    timeout: 30_000,
    expect: { timeout: 5_000 },
    use: {
        baseURL: "http://localhost:8765",
        trace: "on-first-retry",
        launchOptions: {
            slowMo: Number(process.env.PW_SLOWMO || 0),
        },
    },
    webServer: {
        command: `${uvicornCmd} openmarquee.app:app --port 8765 --host 127.0.0.1`,
        cwd: "../backend",
        // Spread process.env so PATH/HOME survive — the PATH-fallback uvicorn
        // branch needs them.
        env: {
            ...process.env,
            OPENMARQUEE_CONTENT_ROOT: E2E_CONTENT_ROOT,
            OPENMARQUEE_DEV_PREVIEW_PATH: E2E_PREVIEW_PATH,
            OPENMARQUEE_PLAYLIST_PATH: E2E_PLAYLIST_PATH,
            OPENMARQUEE_SCHEDULE_PATH: E2E_SCHEDULE_PATH,
            OPENMARQUEE_SETTINGS_PATH: E2E_SETTINGS_PATH,
            OPENMARQUEE_SEED_MARKER_PATH: E2E_SEED_MARKER_PATH,
            // e2e specs assume empty state and reset between tests; the
            // startup seed would pre-populate four slides and break the
            // smoke tests. Seeding is unit-tested in test_seed.py.
            OPENMARQUEE_DISABLE_SEED: "1",
            // Resolve the UI dir relative to THIS file, not cwd, so e2e works
            // regardless of where Playwright is launched from.
            OPENMARQUEE_UI_DIR: __dirname,
        },
        url: "http://localhost:8765/healthz",
        reuseExistingServer: !process.env.CI,
        timeout: 30_000,
    },
    projects: [
        {
            name: "chromium",
            use: { browserName: "chromium" },
        },
    ],
});
