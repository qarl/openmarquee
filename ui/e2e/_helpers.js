// Shared helpers for e2e specs. Mostly: per-test isolation by wiping the
// content the webServer is configured against, while leaving the root dir
// itself in place (the backend tolerates a missing root, but keeping it
// keeps test logs cleaner).

import { mkdirSync, readdirSync, rmSync } from "node:fs";
import path from "node:path";
import {
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
}
