// Set-password screen — Batch 20.2 / phase A.2.
//
// First-boot bootstrap page. Operator picks the password that gates
// the editor from this point forward; we POST it to
// /api/auth/set-password, stash the returned bearer token in
// localStorage under TOKEN_KEY, and bounce to / for the editor.
//
// Standalone (no api.js import) because pre-auth pages run BEFORE
// the editor's fetch wrapper exists. Inline-fetch only.
//
// Form behavior:
//   - Submit disabled until both fields non-empty and the confirm
//     matches the password.
//   - Confirm-mismatch / min-length feedback shows live (as the user
//     types) — not just on submit. Better UX, and the operator gets
//     to fix typos without waiting for a round-trip 422.
//   - Reveal buttons toggle each field between type=password and
//     type=text. Standard show-password pattern, useful on phones
//     where the soft keyboard hides what was typed.
//   - 409 (already configured) → redirect to /login.html. Recovers
//     gracefully if the operator hits /set-password.html on a device
//     that already has auth set up.
//   - 422 / other failures → inline error in the form, button re-
//     enables so the operator can retry.

export const TOKEN_KEY = "openmarquee_auth_token";
const MIN_LENGTH = 8;

/**
 * Bind the set-password form on the page.
 *
 * @param {HTMLFormElement} form
 * @param {object} options
 * @param {typeof fetch} [options.fetch] — injected for tests.
 * @param {(url: string) => void} [options.redirect] — injected for tests.
 * @param {Storage} [options.storage] — injected for tests.
 */
export function mountSetPasswordForm(form, options = {}) {
    const doFetch = options.fetch || ((...a) => globalThis.fetch(...a));
    const redirect = options.redirect || ((url) => {
        window.location.replace(url);
    });
    const storage = options.storage || (typeof localStorage !== "undefined" ? localStorage : null);

    const pw = form.querySelector('input[name="password"]');
    const pw2 = form.querySelector('input[name="password_confirm"]');
    const submit = form.querySelector('button[type="submit"]');
    const formError = form.querySelector("[data-form-error]");
    const hint = (field) => form.querySelector(`[data-field-hint="${field}"]`);

    function setHint(field, message, kind = "") {
        const el = hint(field);
        if (!el) return;
        el.textContent = message || "";
        el.classList.remove("error", "ok");
        if (kind) el.classList.add(kind);
    }

    function reflectValidity() {
        const a = pw.value;
        const b = pw2.value;
        // Password field: only nag on min-length once the operator has
        // started typing — empty + unfocused shows the static default.
        if (a.length === 0) {
            setHint("password", "At least 8 characters.");
        } else if (a.length < MIN_LENGTH) {
            setHint("password", `At least ${MIN_LENGTH} characters (${MIN_LENGTH - a.length} to go).`, "error");
        } else {
            setHint("password", "Looks good.", "ok");
        }
        // Confirm field: only show mismatch once the operator has
        // typed something in it.
        if (b.length === 0) {
            setHint("password_confirm", "");
        } else if (b !== a) {
            setHint("password_confirm", "Passwords don't match.", "error");
        } else {
            setHint("password_confirm", "Matches.", "ok");
        }
        // Submit gate: both non-empty, min-length reached, and equal.
        submit.disabled = !(
            a.length >= MIN_LENGTH &&
            b.length >= MIN_LENGTH &&
            a === b
        );
        // Clear stale form error once the operator edits anything.
        if (formError && formError.textContent) {
            formError.textContent = "";
        }
    }

    // Reveal buttons — flip type=password ↔ type=text on each field.
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
    pw2.addEventListener("input", reflectValidity);
    reflectValidity();

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (submit.disabled) return;
        submit.disabled = true;
        if (formError) formError.textContent = "";
        try {
            const response = await doFetch("/api/auth/set-password", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    password: pw.value,
                    password_confirm: pw2.value,
                }),
            });
            if (response.status === 409) {
                // Already configured — bounce to login so the operator
                // can actually get in.
                redirect("/login.html");
                return;
            }
            if (!response.ok) {
                const detail = await extractDetail(response);
                if (formError) {
                    formError.textContent = detail || `Couldn't set password (HTTP ${response.status}).`;
                }
                submit.disabled = false;
                return;
            }
            const body = await response.json();
            if (storage && body.token) {
                storage.setItem(TOKEN_KEY, body.token);
            }
            redirect("/");
        } catch (err) {
            if (formError) {
                formError.textContent = "Network error — try again.";
            }
            submit.disabled = false;
            console.error("[set-password] submit failed:", err);
        }
    });
}

// Lightweight detail extractor — mirrors the api.js extractDetailMessage
// helper from Batch 15.3 but inlined here so this page has no shared
// dependency. FastAPI's `detail` can be a string OR a list of
// pydantic error objects; we pick the first message for either shape.
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
        /* falls through to null */
    }
    return null;
}

if (typeof window !== "undefined" && typeof document !== "undefined") {
    window.addEventListener("DOMContentLoaded", () => {
        const form = document.querySelector('[data-form="set-password"]');
        if (form) mountSetPasswordForm(form);
    });
}
