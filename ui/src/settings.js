// Settings panel — editable form bound to /api/settings.
//
// Every field on the SystemSettings Pydantic model is surfaced. Client-side
// validation uses HTML form attributes (pattern / min / max) to catch the
// obvious mistakes before a round-trip; the server's Pydantic validators
// are still the source of truth and surface as the status line on 4xx.
//
// Fields are persisted *now* but several are advisory until later phases
// wire them into the running system:
//   - output_mode picks the renderer (Phase 6 HDMI, Phase 8 HUB75, etc.)
//   - wifi_ssid / wifi_password rewrite hostapd.conf (Phase 7)
//   - brightness / gamma feed the active renderer at playback time
// See backend/openmarquee/settings.py for the authoritative scope notes.

import { listTimezones, US_COMMON_TIMEZONES } from "./iana-timezones.js";

const OUTPUT_MODES = [
    { value: "hdmi", label: "HDMI" },
    { value: "hub75", label: "HUB75 panel (LED matrix)" },
    { value: "ws281x", label: "WS2812B / addressable strip" },
    { value: "composite", label: "Composite / RF (via modulator)" },
];

const SECTION_TEMPLATE = `
    <section class="settings">
        <h2 class="settings-heading">System settings</h2>
        <p class="settings-hint">
            Device-wide configuration. Some fields are stored now but
            <em>take effect at a later phase</em> — hostapd (WiFi AP)
            rewrites and the non-HDMI renderers land in subsequent
            commits; changing those values today persists the value and
            will be honored when the wiring arrives.
        </p>

        <form class="settings-form" autocomplete="off">
            <div class="row">
                <label class="field">
                    <span>Sign name</span>
                    <input type="text" class="field-sign-name" maxlength="64" required>
                </label>
                <label class="field">
                    <span>Output mode</span>
                    <select class="field-output-mode"></select>
                </label>
            </div>

            <div class="row">
                <label class="field">
                    <span>Display width (px)</span>
                    <input type="number" class="field-display-width" min="1" max="4096" step="1" required>
                </label>
                <label class="field">
                    <span>Display height (px)</span>
                    <input type="number" class="field-display-height" min="1" max="4096" step="1" required>
                </label>
            </div>

            <div class="row">
                <label class="field">
                    <span>Brightness (0-100)</span>
                    <input type="number" class="field-brightness" min="0" max="100" step="1" required>
                </label>
                <label class="field">
                    <span>Gamma</span>
                    <input type="number" class="field-gamma" min="0.1" max="3.0" step="0.1" required>
                </label>
            </div>

            <div class="row">
                <label class="field">
                    <span>WiFi SSID (access-point name)</span>
                    <input type="text" class="field-wifi-ssid" maxlength="32" required>
                </label>
                <label class="field">
                    <span>WiFi password (8-63 chars)</span>
                    <input type="password" class="field-wifi-password" minlength="8" maxlength="63" required>
                </label>
            </div>

            <div class="row">
                <label class="field">
                    <span>Timezone</span>
                    <select class="field-timezone">
                        <option value="">Device local (no explicit timezone)</option>
                    </select>
                </label>
            </div>

            <button type="submit" class="primary settings-save">Save settings</button>
            <p class="settings-status" role="status" aria-live="polite"></p>
        </form>
    </section>
`;

/**
 * Mount the settings form into `container`.
 *
 * @param {HTMLElement} container
 * @param {object} options
 * @param {() => Promise<object>} options.fetchSettings
 * @param {(payload: object) => Promise<any>} options.onSave
 */
export function mountSettings(container, { fetchSettings, onSave }) {
    container.innerHTML = SECTION_TEMPLATE;
    const form = container.querySelector(".settings-form");
    const statusEl = container.querySelector(".settings-status");
    const saveBtn = container.querySelector(".settings-save");

    const signNameEl = container.querySelector(".field-sign-name");
    const outputModeEl = container.querySelector(".field-output-mode");
    const widthEl = container.querySelector(".field-display-width");
    const heightEl = container.querySelector(".field-display-height");
    const brightnessEl = container.querySelector(".field-brightness");
    const gammaEl = container.querySelector(".field-gamma");
    const ssidEl = container.querySelector(".field-wifi-ssid");
    const passwordEl = container.querySelector(".field-wifi-password");
    const tzEl = container.querySelector(".field-timezone");

    // One-time population of non-data-driven selects.
    for (const mode of OUTPUT_MODES) {
        const opt = document.createElement("option");
        opt.value = mode.value;
        opt.textContent = mode.label;
        outputModeEl.appendChild(opt);
    }
    populateTimezoneSelect(tzEl);

    async function refresh() {
        statusEl.textContent = "";
        try {
            const settings = await fetchSettings();
            signNameEl.value = settings.sign_name ?? "";
            ensureSelectValue(outputModeEl, settings.output_mode);
            outputModeEl.value = settings.output_mode ?? "hdmi";
            widthEl.value = String(settings.display_width ?? 128);
            heightEl.value = String(settings.display_height ?? 96);
            brightnessEl.value = String(settings.brightness ?? 80);
            gammaEl.value = String(settings.gamma ?? 2.2);
            ssidEl.value = settings.wifi_ssid ?? "";
            // Round-trip the real password so the operator can resubmit
            // without retyping it. The captive-portal API already returns
            // it in plaintext on GET — this is just hydrating the form.
            passwordEl.value = settings.wifi_password ?? "";
            setTimezoneValue(tzEl, settings.timezone || "");
        } catch (err) {
            statusEl.textContent = `Could not load settings: ${err.message}`;
        }
    }

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (!form.reportValidity()) return;
        saveBtn.disabled = true;
        statusEl.textContent = "Saving…";
        try {
            const payload = {
                sign_name: signNameEl.value,
                output_mode: outputModeEl.value,
                display_width: Number(widthEl.value),
                display_height: Number(heightEl.value),
                brightness: Number(brightnessEl.value),
                gamma: Number(gammaEl.value),
                wifi_ssid: ssidEl.value,
                wifi_password: passwordEl.value,
                timezone: tzEl.value || null,
            };
            await onSave(payload);
            statusEl.textContent = "Saved.";
        } catch (err) {
            statusEl.textContent = `Save failed: ${err.message}`;
        } finally {
            saveBtn.disabled = false;
        }
    });

    refresh();
    return { refresh };
}

function populateTimezoneSelect(selectEl) {
    // listTimezones() front-loads common U.S. zones; insert a disabled
    // divider so the visual split between "quick picks" and the full
    // IANA dump is obvious.
    const zones = listTimezones();
    const commonSet = new Set(US_COMMON_TIMEZONES);
    let dividerPlaced = false;
    for (const zone of zones) {
        if (!commonSet.has(zone) && !dividerPlaced) {
            const divider = document.createElement("option");
            divider.disabled = true;
            divider.textContent = "──────── all timezones ────────";
            selectEl.appendChild(divider);
            dividerPlaced = true;
        }
        const opt = document.createElement("option");
        opt.value = zone;
        opt.textContent = zone;
        selectEl.appendChild(opt);
    }
}

function setTimezoneValue(selectEl, value) {
    if (!value) {
        selectEl.value = "";
        return;
    }
    const known = Array.from(selectEl.options).some((opt) => opt.value === value);
    if (!known) {
        const opt = document.createElement("option");
        opt.value = value;
        opt.textContent = `${value} (stored)`;
        selectEl.appendChild(opt);
    }
    selectEl.value = value;
}

function ensureSelectValue(selectEl, value) {
    if (!value) return;
    const known = Array.from(selectEl.options).some((opt) => opt.value === value);
    if (!known) {
        const opt = document.createElement("option");
        opt.value = value;
        opt.textContent = `${value} (unknown)`;
        selectEl.appendChild(opt);
    }
}
