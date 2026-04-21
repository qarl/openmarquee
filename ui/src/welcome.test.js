// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { buildWifiPayload, renderWelcomeQR } from "./welcome.js";

describe("buildWifiPayload", () => {
    it("formats a WPA WIFI: payload with the SSID and password", () => {
        expect(buildWifiPayload("OpenMarquee-A3F7", "openmarquee")).toBe(
            "WIFI:T:WPA;S:OpenMarquee-A3F7;P:openmarquee;;",
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
            <dd data-field="ssid">OpenMarquee-A3F7</dd>
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
});
