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

// Sensible default resolutions per output mode. Native LANDSCAPE dims —
// `display_rotation` separately handles portrait-mounted installs.
// HDMI is a placeholder: the real HDMI renderer reads EDID at boot and
// overrides these; on dev (no monitor attached) the stored value applies.
const DEFAULT_DIMS = {
    hdmi: { width: 1920, height: 1080 },
    hub75: { width: 128, height: 96 },
    ws281x: { width: 256, height: 1 },
    composite: { width: 640, height: 480 },
};

const ROTATION_OPTIONS = [
    { value: 0, label: "0° (landscape, native)" },
    { value: 90, label: "90° clockwise" },
    { value: 180, label: "180°" },
    { value: 270, label: "270° clockwise" },
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
                    <span>Display width (px, native landscape)</span>
                    <input type="number" class="field-display-width" min="1" max="4096" step="1" required>
                </label>
                <label class="field">
                    <span>Display height (px, native landscape)</span>
                    <input type="number" class="field-display-height" min="1" max="4096" step="1" required>
                </label>
                <label class="field">
                    <span>Rotation</span>
                    <select class="field-display-rotation"></select>
                </label>
            </div>
            <p class="field-hint settings-rotation-hint">
                Native dims = the panel's hardware orientation. Rotation is
                how you've physically mounted it — the renderer rotates
                frames on the way to hardware.
            </p>

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

            <fieldset class="settings-tailscale">
                <legend>Tailscale (optional remote management)</legend>
                <p class="settings-hint">
                    Bring the device up on your tailnet so you can reach
                    this UI from anywhere. Requires internet at
                    install-time (secondary WiFi or Ethernet). Actual
                    <code>tailscale up</code> wiring lands with the
                    network-provisioning work; today the values persist.
                </p>
                <label class="field-inline">
                    <input type="checkbox" class="field-tailscale-enabled">
                    Enable Tailscale
                </label>
                <div class="row">
                    <label class="field">
                        <span>Hostname on tailnet (optional)</span>
                        <input type="text" class="field-tailscale-hostname" maxlength="63" placeholder="e.g. lobby-sign-01">
                    </label>
                    <label class="field">
                        <span>Auth key (tskey-auth-… or tskey-client-…)</span>
                        <input type="password" class="field-tailscale-auth-key" placeholder="paste from Tailscale admin">
                    </label>
                </div>
            </fieldset>

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
    const rotationEl = container.querySelector(".field-display-rotation");
    const brightnessEl = container.querySelector(".field-brightness");
    const gammaEl = container.querySelector(".field-gamma");
    const ssidEl = container.querySelector(".field-wifi-ssid");
    const passwordEl = container.querySelector(".field-wifi-password");
    const tzEl = container.querySelector(".field-timezone");
    const tsEnabledEl = container.querySelector(".field-tailscale-enabled");
    const tsHostnameEl = container.querySelector(".field-tailscale-hostname");
    const tsAuthKeyEl = container.querySelector(".field-tailscale-auth-key");

    // One-time population of non-data-driven selects.
    for (const mode of OUTPUT_MODES) {
        const opt = document.createElement("option");
        opt.value = mode.value;
        opt.textContent = mode.label;
        outputModeEl.appendChild(opt);
    }
    for (const rot of ROTATION_OPTIONS) {
        const opt = document.createElement("option");
        opt.value = String(rot.value);
        opt.textContent = rot.label;
        rotationEl.appendChild(opt);
    }
    populateTimezoneSelect(tzEl);

    // Output-mode change: if the current dims match *some* mode's default,
    // the operator hasn't customized — snap to the new mode's default. If
    // they've customized, leave the numbers alone (they know what panel
    // they have).
    outputModeEl.addEventListener("change", () => {
        const currentW = Number(widthEl.value);
        const currentH = Number(heightEl.value);
        const isDefault = Object.values(DEFAULT_DIMS).some(
            (d) => d.width === currentW && d.height === currentH,
        );
        if (isDefault) {
            const d = DEFAULT_DIMS[outputModeEl.value];
            if (d) {
                widthEl.value = String(d.width);
                heightEl.value = String(d.height);
            }
        }
    });

    async function refresh() {
        statusEl.textContent = "";
        try {
            const settings = await fetchSettings();
            signNameEl.value = settings.sign_name ?? "";
            ensureSelectValue(outputModeEl, settings.output_mode);
            outputModeEl.value = settings.output_mode ?? "hdmi";
            widthEl.value = String(settings.display_width ?? 128);
            heightEl.value = String(settings.display_height ?? 96);
            rotationEl.value = String(settings.display_rotation ?? 0);
            brightnessEl.value = String(settings.brightness ?? 80);
            gammaEl.value = String(settings.gamma ?? 2.2);
            ssidEl.value = settings.wifi_ssid ?? "";
            // Round-trip the real password so the operator can resubmit
            // without retyping it. The captive-portal API already returns
            // it in plaintext on GET — this is just hydrating the form.
            passwordEl.value = settings.wifi_password ?? "";
            setTimezoneValue(tzEl, settings.timezone || "");
            tsEnabledEl.checked = Boolean(settings.tailscale_enabled);
            tsHostnameEl.value = settings.tailscale_hostname ?? "";
            tsAuthKeyEl.value = settings.tailscale_auth_key ?? "";
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
                display_rotation: Number(rotationEl.value),
                brightness: Number(brightnessEl.value),
                gamma: Number(gammaEl.value),
                wifi_ssid: ssidEl.value,
                wifi_password: passwordEl.value,
                timezone: tzEl.value || null,
                tailscale_enabled: tsEnabledEl.checked,
                tailscale_hostname: tsHostnameEl.value.trim() || null,
                tailscale_auth_key: tsAuthKeyEl.value || null,
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
