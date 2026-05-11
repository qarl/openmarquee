// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mountSetPasswordForm, TOKEN_KEY } from "./set-password.js";

function makeFormFixture() {
    document.body.innerHTML = `
        <form data-form="set-password" novalidate>
            <input name="password" type="password">
            <button type="button" data-reveal-for="password">Show</button>
            <div data-field-hint="password"></div>
            <input name="password_confirm" type="password">
            <button type="button" data-reveal-for="password_confirm">Show</button>
            <div data-field-hint="password_confirm"></div>
            <button type="submit" disabled>Set password</button>
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
        _store: store,
    };
}

function jsonResponse(body, status = 200) {
    return new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
    });
}

let originalLocation;
beforeEach(() => {
    originalLocation = window.location;
});
afterEach(() => {
    vi.restoreAllMocks();
    document.body.innerHTML = "";
    window.location = originalLocation;
});

describe("mountSetPasswordForm — submit gate", () => {
    it("keeps submit disabled until both fields hit min-length and match", () => {
        const form = makeFormFixture();
        mountSetPasswordForm(form, { fetch: vi.fn() });
        const submit = form.querySelector('button[type="submit"]');
        const pw = form.querySelector('input[name="password"]');
        const pw2 = form.querySelector('input[name="password_confirm"]');

        // Both empty — submit stays disabled.
        expect(submit.disabled).toBe(true);

        // Only one filled — still disabled.
        pw.value = "hunter2hunter";
        pw.dispatchEvent(new Event("input"));
        expect(submit.disabled).toBe(true);

        // Confirm differs — still disabled.
        pw2.value = "hunter2DIFFERS";
        pw2.dispatchEvent(new Event("input"));
        expect(submit.disabled).toBe(true);

        // Confirm matches — enables.
        pw2.value = "hunter2hunter";
        pw2.dispatchEvent(new Event("input"));
        expect(submit.disabled).toBe(false);
    });

    it("disables submit when password is under MIN_LENGTH even if confirm matches", () => {
        const form = makeFormFixture();
        mountSetPasswordForm(form, { fetch: vi.fn() });
        const submit = form.querySelector('button[type="submit"]');
        const pw = form.querySelector('input[name="password"]');
        const pw2 = form.querySelector('input[name="password_confirm"]');
        pw.value = "short";
        pw2.value = "short";
        pw.dispatchEvent(new Event("input"));
        pw2.dispatchEvent(new Event("input"));
        expect(submit.disabled).toBe(true);
    });

    it("renders an inline 'to go' countdown while the operator types", () => {
        const form = makeFormFixture();
        mountSetPasswordForm(form, { fetch: vi.fn() });
        const pw = form.querySelector('input[name="password"]');
        pw.value = "abc";
        pw.dispatchEvent(new Event("input"));
        const hint = form.querySelector('[data-field-hint="password"]');
        expect(hint.textContent).toMatch(/5 to go/);
        expect(hint.classList.contains("error")).toBe(true);
    });

    it("renders 'Looks good' once the password meets min-length", () => {
        const form = makeFormFixture();
        mountSetPasswordForm(form, { fetch: vi.fn() });
        const pw = form.querySelector('input[name="password"]');
        pw.value = "hunter2hunter";
        pw.dispatchEvent(new Event("input"));
        const hint = form.querySelector('[data-field-hint="password"]');
        expect(hint.textContent).toMatch(/Looks good/);
        expect(hint.classList.contains("ok")).toBe(true);
    });

    it("renders a mismatch hint while the operator is still typing the confirm", () => {
        const form = makeFormFixture();
        mountSetPasswordForm(form, { fetch: vi.fn() });
        const pw = form.querySelector('input[name="password"]');
        const pw2 = form.querySelector('input[name="password_confirm"]');
        pw.value = "hunter2hunter";
        pw.dispatchEvent(new Event("input"));
        pw2.value = "hunter2hunte";
        pw2.dispatchEvent(new Event("input"));
        const hint = form.querySelector('[data-field-hint="password_confirm"]');
        expect(hint.textContent).toMatch(/don't match/i);
        expect(hint.classList.contains("error")).toBe(true);
    });
});

describe("mountSetPasswordForm — reveal toggle", () => {
    it("flips password input between type=password and type=text on the reveal button", () => {
        const form = makeFormFixture();
        mountSetPasswordForm(form, { fetch: vi.fn() });
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

describe("mountSetPasswordForm — submit POST", () => {
    it("POSTs the password pair and stashes the returned token on 200", async () => {
        const form = makeFormFixture();
        const fetchMock = vi.fn().mockResolvedValue(
            jsonResponse({ token: "1.fake-token-value" }, 200),
        );
        const redirect = vi.fn();
        const storage = fakeStorage();
        mountSetPasswordForm(form, { fetch: fetchMock, redirect, storage });

        const pw = form.querySelector('input[name="password"]');
        const pw2 = form.querySelector('input[name="password_confirm"]');
        pw.value = "hunter2hunter";
        pw2.value = "hunter2hunter";
        pw.dispatchEvent(new Event("input"));
        pw2.dispatchEvent(new Event("input"));
        form.dispatchEvent(new Event("submit", { cancelable: true }));

        await new Promise((r) => setTimeout(r, 0));

        expect(fetchMock).toHaveBeenCalledTimes(1);
        const [url, init] = fetchMock.mock.calls[0];
        expect(url).toBe("/api/auth/set-password");
        expect(init.method).toBe("POST");
        expect(JSON.parse(init.body)).toEqual({
            password: "hunter2hunter",
            password_confirm: "hunter2hunter",
        });
        expect(storage.getItem(TOKEN_KEY)).toBe("1.fake-token-value");
        expect(redirect).toHaveBeenCalledWith("/");
    });

    it("redirects to /login.html when the backend says configured (409)", async () => {
        const form = makeFormFixture();
        const fetchMock = vi.fn().mockResolvedValue(
            jsonResponse({ detail: "already configured" }, 409),
        );
        const redirect = vi.fn();
        const storage = fakeStorage();
        mountSetPasswordForm(form, { fetch: fetchMock, redirect, storage });

        const pw = form.querySelector('input[name="password"]');
        const pw2 = form.querySelector('input[name="password_confirm"]');
        pw.value = "hunter2hunter";
        pw2.value = "hunter2hunter";
        pw.dispatchEvent(new Event("input"));
        pw2.dispatchEvent(new Event("input"));
        form.dispatchEvent(new Event("submit", { cancelable: true }));

        await new Promise((r) => setTimeout(r, 0));

        expect(redirect).toHaveBeenCalledWith("/login.html");
        expect(storage.getItem(TOKEN_KEY)).toBeNull();
    });

    it("surfaces the detail message inline on 422 and re-enables submit", async () => {
        const form = makeFormFixture();
        const fetchMock = vi.fn().mockResolvedValue(
            jsonResponse(
                { detail: [{ msg: "Passwords don't match", loc: ["body"] }] },
                422,
            ),
        );
        const redirect = vi.fn();
        mountSetPasswordForm(form, {
            fetch: fetchMock,
            redirect,
            storage: fakeStorage(),
        });

        const pw = form.querySelector('input[name="password"]');
        const pw2 = form.querySelector('input[name="password_confirm"]');
        pw.value = "hunter2hunter";
        pw2.value = "hunter2hunter";
        pw.dispatchEvent(new Event("input"));
        pw2.dispatchEvent(new Event("input"));
        form.dispatchEvent(new Event("submit", { cancelable: true }));

        await new Promise((r) => setTimeout(r, 0));

        const err = form.querySelector("[data-form-error]");
        expect(err.textContent).toMatch(/Passwords don't match/);
        const submit = form.querySelector('button[type="submit"]');
        expect(submit.disabled).toBe(false);
        expect(redirect).not.toHaveBeenCalled();
    });

    it("surfaces 'Network error' when fetch throws", async () => {
        const form = makeFormFixture();
        const fetchMock = vi.fn().mockRejectedValue(new Error("offline"));
        const redirect = vi.fn();
        mountSetPasswordForm(form, {
            fetch: fetchMock,
            redirect,
            storage: fakeStorage(),
        });

        const pw = form.querySelector('input[name="password"]');
        const pw2 = form.querySelector('input[name="password_confirm"]');
        pw.value = "hunter2hunter";
        pw2.value = "hunter2hunter";
        pw.dispatchEvent(new Event("input"));
        pw2.dispatchEvent(new Event("input"));
        form.dispatchEvent(new Event("submit", { cancelable: true }));

        await new Promise((r) => setTimeout(r, 0));

        const err = form.querySelector("[data-form-error]");
        expect(err.textContent).toMatch(/Network error/);
        const submit = form.querySelector('button[type="submit"]');
        expect(submit.disabled).toBe(false);
    });
});
