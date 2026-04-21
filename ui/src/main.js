// OpenMarquee web UI — entry point.
//
// Phase 3 replaces this skeleton with the real text-slide editor. For now,
// we just bring up the page, hit /healthz, and report what version the
// backend is running so the dev knows the UI/backend pair is live.

import { fetchHealth } from "./api.js";

async function boot() {
    const root = document.getElementById("app");
    try {
        const { status, version } = await fetchHealth();
        root.innerHTML = `<p class="status">Backend ${status} — v${version}. Editor lands in the next commit.</p>`;
    } catch (err) {
        root.innerHTML = `<p class="status">Could not reach backend: ${err.message}</p>`;
    }
}

if (typeof window !== "undefined") {
    window.addEventListener("DOMContentLoaded", boot);
}
