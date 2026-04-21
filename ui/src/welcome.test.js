// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import {
    buildWifiPayload,
    copyToClipboard,
    renderWelcomeQR,
    wireCopyButtons,
} from "./welcome.js";

afterEach(() => {
    vi.restoreAllMocks();
    document.body.innerHTML = "";
});

describe("buildWifiPayload", () => {
    it("formats a WPA WIFI: payload with the SSID and password", () => {
        expect(buildWifiPayload("openMarquee-A3F7", "openmarquee")).toBe(
            "WIFI:T:WPA;S:openMarquee-A3F7;P:openmarquee;;",
        );
    });

    it("escapes the WIFI: control characters in the SSID and password", () => {
        // Backslash, semicolon, comma, colon, and double-quote all need
        // backslash-escaping per the WIFI: format spec.
        expect(buildWifiPayload('a;b,c:d"e\\f', "p:q;r")).toBe(
            'WIFI:T:WPA;S:a\\;b\\,c\\:d\\"e\\\\f;P:p\\:q\\;r;;',
        );
    });
});

describe("renderWelcomeQR", () => {
    it("replaces the placeholder SVG with a real QR and removes the placeholder class", async () => {
        document.body.innerHTML = `
            <dd data-field="ssid">openMarquee-A3F7</dd>
            <dd data-field="password">openmarquee</dd>
            <div class="qr qr-placeholder">
                <svg viewBox="0 0 21 21"><rect width="21" height="21"/></svg>
            </div>
        `;

        await renderWelcomeQR();

        const qr = document.querySelector(".qr");
        expect(qr.classList.contains("qr-placeholder")).toBe(false);
        // The qrcode lib produces an SVG with a path element representing
        // the modules — totally different shape from the placeholder above.
        const svg = qr.querySelector("svg");
        expect(svg).not.toBeNull();
        // The library uses a viewBox sized to the actual QR module count
        // (e.g. "0 0 33 33" for our payload), not the placeholder's 21x21.
        expect(svg.getAttribute("viewBox")).not.toBe("0 0 21 21");
    });

    it("leaves the placeholder visible when the SSID/password elements are missing", async () => {
        document.body.innerHTML = `<div class="qr qr-placeholder"></div>`;
        await renderWelcomeQR();
        // No-op: still has the placeholder class so the watermark stays.
        expect(document.querySelector(".qr").classList.contains("qr-placeholder")).toBe(
            true,
        );
    });

    it("preserves the QR caption across the placeholder → real-QR swap", async () => {
        document.body.innerHTML = `
            <dd data-field="ssid">openMarquee-A3F7</dd>
            <dd data-field="password">openmarquee</dd>
            <div class="qr qr-placeholder">
                <svg viewBox="0 0 21 21"><rect width="21" height="21"/></svg>
                <p class="welcome-qr-caption">Scan to join</p>
            </div>
        `;
        await renderWelcomeQR();
        const caption = document.querySelector(".welcome-qr-caption");
        expect(caption).not.toBeNull();
        expect(caption.textContent).toBe("Scan to join");
    });
});

describe("copyToClipboard", () => {
    it("uses navigator.clipboard.writeText when available in a secure context", async () => {
        const writeText = vi.fn().mockResolvedValue(undefined);
        vi.stubGlobal("navigator", { clipboard: { writeText } });
        // jsdom's window is secure-context-ish; pretend to be sure.
        Object.defineProperty(window, "isSecureContext", {
            configurable: true,
            value: true,
        });

        const result = await copyToClipboard("hello");
        expect(result).toBe("copied");
        expect(writeText).toHaveBeenCalledWith("hello");
    });

    it("falls back to execCommand when clipboard API is absent", async () => {
        vi.stubGlobal("navigator", { /* no clipboard */ });
        const execCommand = vi.fn().mockReturnValue(true);
        document.execCommand = execCommand;
        const result = await copyToClipboard("fallback-text");
        expect(result).toBe("copied");
        expect(execCommand).toHaveBeenCalledWith("copy");
        delete document.execCommand;
    });

    it("returns 'fallback' when both paths fail", async () => {
        vi.stubGlobal("navigator", { /* no clipboard */ });
        document.execCommand = vi.fn().mockReturnValue(false);
        const result = await copyToClipboard("nope");
        expect(result).toBe("fallback");
        delete document.execCommand;
    });
});

describe("wireCopyButtons", () => {
    function mountFixture() {
        document.body.innerHTML = `
            <dd data-field="ssid">openMarquee-A3F7</dd>
            <dd data-field="password">hunter2-network</dd>
            <button type="button" class="copy-btn"
                    data-copy-for="ssid">Copy</button>
            <button type="button" class="copy-btn"
                    data-copy-for="password">Copy</button>
            <p data-field="copy-status"></p>
        `;
    }

    it("copies the matching field on click and announces success", async () => {
        mountFixture();
        const writeText = vi.fn().mockResolvedValue(undefined);
        vi.stubGlobal("navigator", { clipboard: { writeText } });
        Object.defineProperty(window, "isSecureContext", {
            configurable: true,
            value: true,
        });

        wireCopyButtons();
        document
            .querySelector('[data-copy-for="password"]')
            .click();
        // Let the async copy resolve.
        await new Promise((r) => setTimeout(r, 0));

        expect(writeText).toHaveBeenCalledWith("hunter2-network");
        const btn = document.querySelector('[data-copy-for="password"]');
        expect(btn.dataset.state).toBe("copied");
        expect(btn.textContent).toBe("Copied");
        expect(
            document.querySelector('[data-field="copy-status"]').textContent,
        ).toMatch(/Password copied/);
    });

    it("surfaces a select-fallback state when both copy paths fail", async () => {
        mountFixture();
        vi.stubGlobal("navigator", { /* no clipboard */ });
        document.execCommand = vi.fn().mockReturnValue(false);

        wireCopyButtons();
        document.querySelector('[data-copy-for="ssid"]').click();
        await new Promise((r) => setTimeout(r, 0));

        const btn = document.querySelector('[data-copy-for="ssid"]');
        expect(btn.dataset.state).toBe("fallback");
        expect(btn.textContent).toMatch(/Select/);
        expect(
            document.querySelector('[data-field="copy-status"]').textContent,
        ).toMatch(/tap the network/i);
        delete document.execCommand;
    });
});
