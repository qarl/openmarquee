// Real QR code generation for the welcome page.
//
// Reads the SSID + password from the rendered page (data-field attributes
// in welcome.html), encodes them in the standard WIFI:T:WPA;S:...;P:...;;
// payload format that iOS and Android camera apps recognize, and replaces
// the hand-drawn placeholder SVG with a scannable code.
//
// Phase 7 (real captive portal) will template the SSID/password into the
// HTML on the device side; this script then picks them up automatically.

import QRCode from "qrcode";

function buildWifiPayload(ssid, password) {
    // Escape the four characters with special meaning in WIFI: payloads:
    // backslash, semicolon, comma, colon, double-quote.
    const escape = (value) =>
        String(value).replace(/[\\;,":]/g, (ch) => `\\${ch}`);
    return `WIFI:T:WPA;S:${escape(ssid)};P:${escape(password)};;`;
}

async function renderWelcomeQR() {
    const ssidEl = document.querySelector('[data-field="ssid"]');
    const passwordEl = document.querySelector('[data-field="password"]');
    const target = document.querySelector(".qr");
    if (!ssidEl || !passwordEl || !target) return;

    const payload = buildWifiPayload(ssidEl.textContent.trim(), passwordEl.textContent.trim());

    try {
        const svg = await QRCode.toString(payload, {
            type: "svg",
            margin: 0,
            errorCorrectionLevel: "M",
            color: { dark: "#000000", light: "#FFFFFF" },
        });
        target.innerHTML = svg;
        target.classList.remove("qr-placeholder");
    } catch (err) {
        // Leave the placeholder visible so it's obvious something went wrong
        // — better than rendering an unscannable QR silently.
        console.error("QR generation failed:", err);
    }
}

if (typeof window !== "undefined") {
    window.addEventListener("DOMContentLoaded", renderWelcomeQR);
}

// Exported for unit tests.
export { buildWifiPayload, renderWelcomeQR };
