// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mountLoginForm, TOKEN_KEY } from "./login.js";

function makeFormFixture() {
    document.body.innerHTML = `
        <form data-form="login" novalidate>
            <input name="password" type="password">
            <button type="button" data-reveal-for="password">Show</button>
            <button type="submit" disabled>Sign in</button>
            <p data-form-error></p>
        </form>
    `;
    return document.querySelector("form");
}

function fakeStorage() {
    const store = new Map();
    return {
        setItem: (k, v) => store.set(k, v),
        getItem: (k) => (store.has(k) ? store.get(k) : null),
        removeItem: (k) => store.delete(k),
    };
}

function jsonResponse(body, status = 200) {
    return new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
    });
}

afterEach(() => {
    vi.restoreAllMocks();
    document.body.innerHTML = "";
});

describe("mountLoginForm — submit gate", () => {
    it("disables submit while the field is empty", () => {
        const form = makeFormFixture();
        mountLoginForm(form, { fetch: vi.fn() });
        const submit = form.querySelector('button[type="submit"]');
        expect(submit.disabled).toBe(true);
        const pw = form.querySelector('input[name="password"]');
        pw.value = "x";
        pw.dispatchEvent(new Event("input"));
        expect(submit.disabled).toBe(false);
        pw.value = "";
        pw.dispatchEvent(new Event("input"));
        expect(submit.disabled).toBe(true);
    });

    it("flips the reveal button between Show/Hide", () => {
        const form = makeFormFixture();
        mountLoginForm(form, { fetch: vi.fn() });
        const pw = form.querySelector('input[name="password"]');
        const reveal = form.querySelector('[data-reveal-for="password"]');
        expect(pw.type).toBe("password");
        reveal.click();
        expect(pw.type).toBe("text");
        expect(reveal.textContent).toBe("Hide");
        reveal.click();
        expect(pw.type).toBe("password");
        expect(reveal.textContent).toBe("Show");
    });
});

describe("mountLoginForm — submit POST", () => {
    it("POSTs and stashes the token on 200", async () => {
        const form = makeFormFixture();
        const fetchMock = vi.fn().mockResolvedValue(
            jsonResponse({ token: "1.fake-login-token" }, 200),
        );
        const redirect = vi.fn();
        const storage = fakeStorage();
        mountLoginForm(form, { fetch: fetchMock, redirect, storage });

        const pw = form.querySelector('input[name="password"]');
        pw.value = "hunter2hunter";
        pw.dispatchEvent(new Event("input"));
        form.dispatchEvent(new Event("submit", { cancelable: true }));

        await new Promise((r) => setTimeout(r, 0));

        expect(fetchMock).toHaveBeenCalledTimes(1);
        const [url, init] = fetchMock.mock.calls[0];
        expect(url).toBe("/api/auth/login");
        expect(init.method).toBe("POST");
        expect(JSON.parse(init.body)).toEqual({ password: "hunter2hunter" });
        expect(storage.getItem(TOKEN_KEY)).toBe("1.fake-login-token");
        expect(redirect).toHaveBeenCalledWith("/");
    });

    it("shows 'Incorrect password.' inline on 401 and re-enables submit", async () => {
        const form = makeFormFixture();
        const fetchMock = vi.fn().mockResolvedValue(
            jsonResponse({ detail: "wrong password" }, 401),
        );
        const redirect = vi.fn();
        mountLoginForm(form, {
            fetch: fetchMock,
            redirect,
            storage: fakeStorage(),
        });

        const pw = form.querySelector('input[name="password"]');
        pw.value = "wrong-password";
        pw.dispatchEvent(new Event("input"));
        form.dispatchEvent(new Event("submit", { cancelable: true }));

        await new Promise((r) => setTimeout(r, 0));

        const err = form.querySelector("[data-form-error]");
        expect(err.textContent).toBe("Incorrect password.");
        const submit = form.querySelector('button[type="submit"]');
        expect(submit.disabled).toBe(false);
        expect(redirect).not.toHaveBeenCalled();
    });

    it("redirects to /set-password.html on 404 (device not configured)", async () => {
        const form = makeFormFixture();
        const fetchMock = vi.fn().mockResolvedValue(
            jsonResponse({ detail: "not configured" }, 404),
        );
        const redirect = vi.fn();
        const storage = fakeStorage();
        mountLoginForm(form, { fetch: fetchMock, redirect, storage });

        const pw = form.querySelector('input[name="password"]');
        pw.value = "anything";
        pw.dispatchEvent(new Event("input"));
        form.dispatchEvent(new Event("submit", { cancelable: true }));

        await new Promise((r) => setTimeout(r, 0));

        expect(redirect).toHaveBeenCalledWith("/set-password.html");
        expect(storage.getItem(TOKEN_KEY)).toBeNull();
    });

    it("surfaces 'Network error' when fetch throws", async () => {
        const form = makeFormFixture();
        const fetchMock = vi.fn().mockRejectedValue(new Error("offline"));
        const redirect = vi.fn();
        mountLoginForm(form, {
            fetch: fetchMock,
            redirect,
            storage: fakeStorage(),
        });

        const pw = form.querySelector('input[name="password"]');
        pw.value = "hunter2";
        pw.dispatchEvent(new Event("input"));
        form.dispatchEvent(new Event("submit", { cancelable: true }));

        await new Promise((r) => setTimeout(r, 0));

        const err = form.querySelector("[data-form-error]");
        expect(err.textContent).toMatch(/Network error/);
        const submit = form.querySelector('button[type="submit"]');
        expect(submit.disabled).toBe(false);
    });

    // Round 22 Bug A regression-locks: silent login-loop when token
    // can't be persisted. Pre-fix, redirect fired unconditionally on
    // 200; if storage was unavailable the editor 401'd on its first
    // authed call and bounced back. Now: formError + no redirect.
    it("does NOT redirect when storage.setItem throws (quota exceeded / blocked)", async () => {
        const form = makeFormFixture();
        const fetchMock = vi.fn().mockResolvedValue(
            jsonResponse({ token: "1.fake-login-token" }, 200),
        );
        const redirect = vi.fn();
        const storage = {
            setItem: () => { throw new Error("QuotaExceeded"); },
            getItem: () => null,
            removeItem: () => {},
        };
        mountLoginForm(form, { fetch: fetchMock, redirect, storage });

        const pw = form.querySelector('input[name="password"]');
        pw.value = "hunter2hunter";
        pw.dispatchEvent(new Event("input"));
        form.dispatchEvent(new Event("submit", { cancelable: true }));
        await new Promise((r) => setTimeout(r, 0));

        expect(redirect).not.toHaveBeenCalled();
        expect(form.querySelector("[data-form-error]").textContent).toMatch(
            /storage is unavailable/,
        );
    });

    it("does NOT redirect when 200 response is missing body.token", async () => {
        const form = makeFormFixture();
        const fetchMock = vi.fn().mockResolvedValue(
            jsonResponse({ /* no token field */ }, 200),
        );
        const redirect = vi.fn();
        const storage = fakeStorage();
        mountLoginForm(form, { fetch: fetchMock, redirect, storage });

        const pw = form.querySelector('input[name="password"]');
        pw.value = "hunter2";
        pw.dispatchEvent(new Event("input"));
        form.dispatchEvent(new Event("submit", { cancelable: true }));
        await new Promise((r) => setTimeout(r, 0));

        expect(redirect).not.toHaveBeenCalled();
        expect(storage.getItem(TOKEN_KEY)).toBeNull();
        expect(form.querySelector("[data-form-error]").textContent).toMatch(
            /storage is unavailable/,
        );
    });

    // Round 22 Bug B regression-lock: bfcache / form-restore password
    // leak. The pw field must be cleared BEFORE the redirect so a
    // Back-nav repopulate doesn't expose plaintext via the Show toggle.
    it("clears the password field before redirecting (bfcache leak guard)", async () => {
        const form = makeFormFixture();
        const fetchMock = vi.fn().mockResolvedValue(
            jsonResponse({ token: "1.fake-login-token" }, 200),
        );
        let pwAtRedirect = null;
        const redirect = vi.fn(() => {
            pwAtRedirect = form.querySelector('input[name="password"]').value;
        });
        mountLoginForm(form, { fetch: fetchMock, redirect, storage: fakeStorage() });

        const pw = form.querySelector('input[name="password"]');
        pw.value = "should-be-wiped";
        pw.dispatchEvent(new Event("input"));
        form.dispatchEvent(new Event("submit", { cancelable: true }));
        await new Promise((r) => setTimeout(r, 0));

        expect(redirect).toHaveBeenCalledWith("/");
        expect(pwAtRedirect).toBe("");
    });
});
