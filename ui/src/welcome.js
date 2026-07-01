// Real QR code generation + copy-to-clipboard affordances for the
// welcome page.
//
// Reads the SSID + password from the rendered page (data-field
// attributes in welcome.html), encodes them in the standard
// WIFI:T:WPA;S:...;P:...;; payload format that iOS and Android camera
// apps recognize, and replaces the hand-drawn placeholder SVG with a
// scannable code.
//
// Copy-to-clipboard is intentionally pessimistic: the welcome page
// runs over the captive-portal AP without HTTPS, which disables
// navigator.clipboard on most browsers. We try the modern API first
// (might work on Chrome's localhost-isn't-insecure exception), then
// fall back to the deprecated-but-widely-supported
// document.execCommand("copy") against a temporary <textarea>. The
// .creds-value elements also have `user-select: all` so a plain
// click-on-the-text selects it for a manual Cmd+C as a last resort.
//
// Phase 7 (real captive portal) will template the SSID/password into
// the HTML on the device side; this script then picks them up
// automatically.

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

    const payload = buildWifiPayload(
        ssidEl.textContent.trim(),
        passwordEl.textContent.trim(),
    );

    try {
        const svg = await QRCode.toString(payload, {
            type: "svg",
            margin: 0,
            errorCorrectionLevel: "M",
            color: { dark: "#000000", light: "#FFFFFF" },
        });
        // The `<p class="welcome-qr-caption">` lives inside the same
        // container as the SVG but must survive the replacement.
        const caption = target.querySelector(".welcome-qr-caption");
        target.innerHTML = svg;
        if (caption) target.appendChild(caption);
        target.classList.remove("qr-placeholder");
    } catch (err) {
        // Leave the placeholder visible so it's obvious something went
        // wrong — better than rendering an unscannable QR silently.
        console.error("QR generation failed:", err);
    }
}

/**
 * Copy `text` to the clipboard. Returns:
 *   "copied"   — native clipboard API succeeded
 *   "copied"   — execCommand("copy") fallback succeeded
 *   "fallback" — both failed; caller should prompt the user to select
 *                manually (the .creds-value has user-select: all so a
 *                tap already selects everything).
 */
export async function copyToClipboard(text) {
    try {
        if (
            typeof navigator !== "undefined"
            && navigator.clipboard
            && typeof navigator.clipboard.writeText === "function"
            && (typeof window === "undefined" || window.isSecureContext)
        ) {
            await navigator.clipboard.writeText(text);
            return "copied";
        }
    } catch {
        // fall through to execCommand fallback
    }
    try {
        const ta = document.createElement("textarea");
        ta.value = text;
        // Offscreen-but-focusable so execCommand("copy") picks up the
        // selection without a visible flash.
        ta.setAttribute("readonly", "");
        ta.style.position = "fixed";
        ta.style.top = "0";
        ta.style.left = "0";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        const ok = document.execCommand("copy");
        ta.remove();
        if (ok) return "copied";
    } catch {
        // fall through
    }
    return "fallback";
}

/**
 * Wire every `[data-copy-for="<field>"]` button to copy the text
 * content of the matching `[data-field="<field>"]` element.
 */
export function wireCopyButtons(root = document) {
    const buttons = root.querySelectorAll("[data-copy-for]");
    const statusEl = root.querySelector('[data-field="copy-status"]');
    for (const btn of buttons) {
        btn.addEventListener("click", async () => {
            const field = btn.dataset.copyFor;
            const source = root.querySelector(`[data-field="${field}"]`);
            if (!source) return;
            const text = source.textContent.trim();
            const result = await copyToClipboard(text);

            const label = field === "ssid" ? "Network" : "Password";
            if (result === "copied") {
                btn.dataset.state = "copied";
                btn.textContent = "Copied";
                if (statusEl) statusEl.textContent = `${label} copied.`;
            } else {
                // Clipboard APIs both failed — point the user at the
                // click-to-select affordance.
                btn.dataset.state = "fallback";
                btn.textContent = "Select";
                if (statusEl) {
                    statusEl.textContent =
                        `Couldn't copy automatically — tap the ${label.toLowerCase()} to select it, then copy.`;
                }
                // Auto-select the source element so a single Cmd+C finishes the job.
                try {
                    const range = document.createRange();
                    range.selectNodeContents(source);
                    const sel = window.getSelection();
                    sel.removeAllRanges();
                    sel.addRange(range);
                } catch {
                    // ignore — user-select: all in CSS still lets the user select by click
                }
            }

            // Revert the button after a beat so it doesn't lie forever.
            setTimeout(() => {
                btn.textContent = "Copy";
                delete btn.dataset.state;
            }, 2000);
        });
    }
}

import { mountPwaInstallPrompt } from "./pwa-install-prompt.js";

if (typeof window !== "undefined") {
    window.addEventListener("DOMContentLoaded", () => {
        renderWelcomeQR();
        wireCopyButtons();
        // PWA "bookmark moment" (spec §"Finding the device after
        // onboarding" #2). On iOS Safari OR Chromium fire the install
        // prompt in the small slot below the Continue button; else
        // the mount is a silent no-op. Failures never propagate —
        // welcome.js MUST keep working even if pwa-install-prompt.js
        // is missing (offline captive-portal deploys where a stale
        // dist can lack the module).
        try {
            const slot = document.getElementById("pwa-install-slot");
            if (slot) {
                mountPwaInstallPrompt(slot);
            }
        } catch (err) {
            console.warn("pwa install prompt mount failed (non-fatal):", err);
        }
    });
}

// Exported for unit tests.
export { buildWifiPayload, renderWelcomeQR };
