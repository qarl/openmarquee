// Captive-portal wifi-entry onboarding page (P0-1d part 2).
//
// The unauth first-run surface where an operator, phone-in-hand on the
// sign's setup AP, puts the sign onto their home WiFi. Backed by the
// already-shipped unauth endpoints:
//   POST /api/onboarding/submit-credentials  {ssid, password}
//   GET  /api/onboarding/status  -> {state, wifi_station_state, wifi_station_ssid}
// (api_onboarding.py). The flow, per that module's contract: POST the
// form, then poll the status endpoint and reflect the supervisor state
// machine (SETUP -> CONNECTING -> LINGER/ONLINE, or back to SETUP on a
// bad password) so the operator sees "Connecting…" -> "Connected" without
// a page reload.
//
// Standalone page — does NOT import api.js (which injects a bearer token);
// this runs BEFORE any token exists. Plain fetch only. `fetch` + `wait`
// are injectable so the tests are DETERMINISTIC (no real timers, no
// wall-clock races).

const TERMINAL_OK = new Set(["LINGER", "ONLINE"]);

/**
 * Bind the onboarding wifi form.
 * @param {HTMLElement} root - container holding the form + status region.
 * @param {object} [options]
 * @param {typeof fetch} [options.fetch] - injected for tests.
 * @param {(ms:number)=>Promise<void>} [options.wait] - injected for tests.
 * @param {number} [options.pollIntervalMs=1000]
 * @param {number} [options.maxPolls=60]
 */
export function mountOnboarding(root, options = {}) {
    const doFetch = options.fetch || ((...a) => globalThis.fetch(...a));
    const wait = options.wait || ((ms) => new Promise((r) => setTimeout(r, ms)));
    const pollIntervalMs = options.pollIntervalMs ?? 1000;
    const maxPolls = options.maxPolls ?? 60;

    const form = root.querySelector('[data-form="onboarding"]');
    if (!form) return;
    const ssidEl = form.querySelector('input[name="ssid"]');
    const pwEl = form.querySelector('input[name="password"]');
    const submit = form.querySelector('button[type="submit"]');
    const statusEl = root.querySelector("[data-status]");
    const errorEl = form.querySelector("[data-form-error]");
    const doneEl = root.querySelector("[data-done]");

    // Reveal buttons (Show/Hide) mirror set-password.js.
    for (const btn of form.querySelectorAll("[data-reveal-for]")) {
        btn.addEventListener("click", () => {
            const field = form.querySelector(`input[name="${btn.dataset.revealFor}"]`);
            if (!field) return;
            const showing = field.type === "text";
            field.type = showing ? "password" : "text";
            btn.textContent = showing ? "Show" : "Hide";
            // Keep the screen-reader label in sync (mirrors set-password.js).
            btn.setAttribute("aria-label", showing ? "Show password" : "Hide password");
        });
    }

    function setStatus(msg, kind = "") {
        if (statusEl) {
            statusEl.textContent = msg;
            statusEl.className = kind ? `status ${kind}` : "status";
        }
    }
    function setError(msg) {
        if (errorEl) errorEl.textContent = msg || "";
    }

    // Enable submit only when both fields are plausibly valid, so the
    // operator can't fire an obviously-bad request.
    function validate() {
        const ok = ssidEl.value.trim().length >= 1 && pwEl.value.length >= 8;
        submit.disabled = !ok;
        return ok;
    }
    ssidEl.addEventListener("input", () => {
        setError("");
        validate();
    });
    pwEl.addEventListener("input", () => {
        setError("");
        validate();
    });
    validate();

    async function pollUntilSettled(ssid) {
        for (let i = 0; i < maxPolls; i++) {
            await wait(pollIntervalMs);
            let body;
            try {
                const res = await doFetch("/api/onboarding/status");
                if (!res.ok) continue; // transient; keep polling
                body = await res.json();
            } catch {
                continue; // network blip during the wifi switch; keep polling
            }
            const state = body?.state;
            if (TERMINAL_OK.has(state)) {
                setStatus(`Connected to ${body.wifi_station_ssid || ssid}.`, "ok");
                if (doneEl) doneEl.hidden = false;
                return "connected";
            }
            if (state === "SETUP") {
                // Dropped back to SETUP after we started connecting = the
                // creds were rejected (bad password / SSID not found).
                setStatus("", "");
                setError("Couldn't connect — double-check the password and try again.");
                submit.disabled = false;
                submit.textContent = "Connect";
                return "failed";
            }
            // CONNECTING (or a transient DEGRADED) — keep waiting.
            setStatus(`Connecting to ${ssid}…`);
        }
        setStatus(
            "Still trying to connect. You can keep waiting, or check the network and retry.",
        );
        submit.disabled = false;
        submit.textContent = "Connect";
        return "timeout";
    }

    form.addEventListener("submit", async (e) => {
        e.preventDefault();
        setError("");
        if (!validate()) {
            setError("Enter a network name and a password of at least 8 characters.");
            return;
        }
        const ssid = ssidEl.value.trim();
        const password = pwEl.value;
        submit.disabled = true;
        submit.textContent = "Connecting…";
        setStatus(`Connecting to ${ssid}…`);
        try {
            const res = await doFetch("/api/onboarding/submit-credentials", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ ssid, password }),
            });
            if (!res.ok) {
                let detail = `HTTP ${res.status}`;
                try {
                    const b = await res.json();
                    // Only a string detail is safe to show; a 422's detail
                    // is a list of objects → would render "[object Object]".
                    if (typeof b?.detail === "string") detail = b.detail;
                } catch {
                    /* non-JSON */
                }
                throw new Error(detail);
            }
        } catch (err) {
            setStatus("", "");
            setError(`Couldn't start connecting: ${err.message}`);
            submit.disabled = false;
            submit.textContent = "Connect";
            return;
        }
        await pollUntilSettled(ssid);
    });
}

// Auto-mount on the real page (skipped in tests, which import + call
// mountOnboarding directly against a fixture root).
if (typeof document !== "undefined") {
    const root = document.querySelector("[data-onboarding-root]");
    if (root) mountOnboarding(root);
}
