// Login screen — Batch 20.2 / phase A.2.
//
// Single-field auth bootstrap. Operator enters the password they
// chose at set-password time; we POST it to /api/auth/login, stash
// the returned bearer token in localStorage under TOKEN_KEY, and
// bounce to / for the editor.
//
// Standalone (no api.js import) — same rationale as set-password.js.
//
// Behavior:
//   - Submit disabled until the password field is non-empty.
//   - 401 → inline error "Incorrect password." (no specific 422
//     surface here because the backend treats login as either-it-
//     matches-or-it-doesn't.)
//   - 404 → device isn't configured; bounce to /set-password.html so
//     the operator can set one. Recovers gracefully if the operator
//     hit /login.html on a fresh device.

export const TOKEN_KEY = "openmarquee_auth_token";

/**
 * Bind the login form on the page.
 *
 * @param {HTMLFormElement} form
 * @param {object} options
 * @param {typeof fetch} [options.fetch] — injected for tests.
 * @param {(url: string) => void} [options.redirect] — injected for tests.
 * @param {Storage} [options.storage] — injected for tests.
 */
export function mountLoginForm(form, options = {}) {
    const doFetch = options.fetch || ((...a) => globalThis.fetch(...a));
    const redirect = options.redirect || ((url) => {
        window.location.replace(url);
    });
    const storage = options.storage || (typeof localStorage !== "undefined" ? localStorage : null);

    const pw = form.querySelector('input[name="password"]');
    const submit = form.querySelector('button[type="submit"]');
    const formError = form.querySelector("[data-form-error]");

    function reflectValidity() {
        submit.disabled = pw.value.length === 0;
        // Clear stale error on edit.
        if (formError && formError.textContent) {
            formError.textContent = "";
        }
    }

    for (const btn of form.querySelectorAll("[data-reveal-for]")) {
        btn.addEventListener("click", () => {
            const field = form.querySelector(`input[name="${btn.dataset.revealFor}"]`);
            if (!field) return;
            const showing = field.type === "text";
            field.type = showing ? "password" : "text";
            btn.textContent = showing ? "Show" : "Hide";
            btn.setAttribute("aria-label", showing ? "Show password" : "Hide password");
        });
    }

    pw.addEventListener("input", reflectValidity);
    reflectValidity();

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (submit.disabled) return;
        submit.disabled = true;
        if (formError) formError.textContent = "";
        try {
            const response = await doFetch("/api/auth/login", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ password: pw.value }),
            });
            if (response.status === 404) {
                // Device isn't actually configured — bounce to
                // set-password so the operator can pick one. This
                // covers the "operator typed /login.html into a fresh
                // browser before the welcome flow" case.
                redirect("/set-password.html");
                return;
            }
            if (response.status === 401) {
                if (formError) formError.textContent = "Incorrect password.";
                submit.disabled = pw.value.length === 0;
                pw.select?.();
                return;
            }
            if (!response.ok) {
                const detail = await extractDetail(response);
                if (formError) {
                    formError.textContent = detail || `Sign-in failed (HTTP ${response.status}).`;
                }
                submit.disabled = pw.value.length === 0;
                return;
            }
            const body = await response.json();
            if (storage && body.token) {
                storage.setItem(TOKEN_KEY, body.token);
            }
            redirect("/");
        } catch (err) {
            if (formError) formError.textContent = "Network error — try again.";
            submit.disabled = pw.value.length === 0;
            console.error("[login] submit failed:", err);
        }
    });
}

async function extractDetail(response) {
    try {
        const body = await response.json();
        const detail = body?.detail;
        if (typeof detail === "string") return detail;
        if (Array.isArray(detail) && detail.length > 0) {
            const first = detail[0];
            if (typeof first === "string") return first;
            if (first?.msg) return first.msg;
        }
    } catch {
        /* falls through */
    }
    return null;
}

if (typeof window !== "undefined" && typeof document !== "undefined") {
    window.addEventListener("DOMContentLoaded", () => {
        const form = document.querySelector('[data-form="login"]');
        if (form) mountLoginForm(form);
    });
}
