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
        <h2 class="subpage-title">System settings</h2>
        <div class="schedule-now" data-field="now">
            <span class="schedule-now-label">Device time</span>
            <span class="schedule-now-value" data-field="now-value">—</span>
        </div>

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
                <label class="field">
                    <span>Rotation</span>
                    <select class="field-display-rotation"></select>
                </label>
            </div>
            <button type="button" class="settings-detect-dims field-hint-btn">
                Detect from device
            </button>
            <p class="field-hint settings-detect-status" role="status"></p>

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

            <label class="field settings-ws281x-pixel-order-wrap" hidden>
                <span>Addressable strip ordering</span>
                <select class="field-ws281x-pixel-order">
                    <option value="row_major">Row-major (wired raster-order)</option>
                    <option value="serpentine">Serpentine (rows alternate direction)</option>
                </select>
            </label>

            <fieldset class="settings-wifi-ap">
                <legend>
                    <label class="field-inline">
                        <input type="checkbox" class="field-wifi-ap-enabled" checked>
                        Access point (captive-portal network phones join during setup)
                    </label>
                </legend>
                <div class="row">
                    <label class="field">
                        <span>AP SSID</span>
                        <input type="text" class="field-wifi-ssid" maxlength="32">
                    </label>
                    <label class="field">
                        <span>AP password (8-63 chars)</span>
                        <input type="password" class="field-wifi-password" minlength="8" maxlength="63">
                    </label>
                </div>
            </fieldset>

            <fieldset class="settings-wifi-station">
                <legend>
                    <label class="field-inline">
                        <input type="checkbox" class="field-wifi-station-enabled">
                        Join existing WiFi
                    </label>
                </legend>
                <div class="row">
                    <label class="field">
                        <span>WiFi SSID</span>
                        <select class="field-wifi-station-ssid-picker">
                            <option value="__other__">(type manually)</option>
                        </select>
                        <input type="text" class="field-wifi-station-ssid" maxlength="32" placeholder="SSID">
                    </label>
                    <label class="field">
                        <span>WiFi password (8-63 chars)</span>
                        <input type="password" class="field-wifi-station-password" minlength="8" maxlength="63">
                    </label>
                </div>
                <button type="button" class="settings-wifi-rescan field-hint-btn">
                    Rescan nearby networks
                </button>
                <p class="field-hint">
                    Runs concurrently with the access point on the Pi's single
                    radio; both modes share the same channel. Disabling both
                    modes isn't allowed — the device would be unreachable.
                </p>

                <fieldset class="settings-tailscale">
                    <legend>
                        <label class="field-inline">
                            <input type="checkbox" class="field-tailscale-enabled">
                            Tailscale
                        </label>
                    </legend>
                    <p class="settings-hint">
                        Bring the device up on your tailnet so you can reach
                        this UI from anywhere. Requires internet at
                        install-time (secondary WiFi or Ethernet).
                    </p>
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
            </fieldset>

            <label class="field">
                <span>Timezone</span>
                <select class="field-timezone">
                    <option value="">Device local (no explicit timezone)</option>
                </select>
            </label>

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
    const detectBtn = container.querySelector(".settings-detect-dims");
    const detectStatusEl = container.querySelector(".settings-detect-status");
    const brightnessEl = container.querySelector(".field-brightness");
    const gammaEl = container.querySelector(".field-gamma");
    const ws281xOrderWrap = container.querySelector(
        ".settings-ws281x-pixel-order-wrap",
    );
    const ws281xOrderEl = container.querySelector(".field-ws281x-pixel-order");
    const apEnabledEl = container.querySelector(".field-wifi-ap-enabled");
    const ssidEl = container.querySelector(".field-wifi-ssid");
    const passwordEl = container.querySelector(".field-wifi-password");
    const stationEnabledEl = container.querySelector(".field-wifi-station-enabled");
    const stationSsidEl = container.querySelector(".field-wifi-station-ssid");
    const stationSsidPickerEl = container.querySelector(
        ".field-wifi-station-ssid-picker",
    );
    const stationPasswordEl = container.querySelector(".field-wifi-station-password");
    const wifiRescanBtn = container.querySelector(".settings-wifi-rescan");
    const tzEl = container.querySelector(".field-timezone");
    const tsEnabledEl = container.querySelector(".field-tailscale-enabled");
    const tsHostnameEl = container.querySelector(".field-tailscale-hostname");
    const tsAuthKeyEl = container.querySelector(".field-tailscale-auth-key");
    const nowValueEl = container.querySelector('[data-field="now-value"]');

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

    // WiFi enable checkboxes: gray out the matching fieldset when off,
    // and prevent disabling both (the server validator rejects that too,
    // but catching it client-side avoids a confusing 422 at save time).
    function syncWifiGrayOut() {
        const apOn = apEnabledEl.checked;
        const stationOn = stationEnabledEl.checked;
        ssidEl.disabled = !apOn;
        passwordEl.disabled = !apOn;
        stationSsidEl.disabled = !stationOn;
        stationSsidPickerEl.disabled = !stationOn;
        stationPasswordEl.disabled = !stationOn;
        container
            .querySelector(".settings-wifi-ap")
            .classList.toggle("is-disabled", !apOn);
        container
            .querySelector(".settings-wifi-station")
            .classList.toggle("is-disabled", !stationOn);
    }
    // Tailscale section header toggle gates everything below it the
    // same way wifi-ap / wifi-station do. Tailscale also requires wifi
    // station (internet) to function, so when station is off the whole
    // tailscale subsection is force-disabled + force-unchecked.
    function syncTailscaleGrayOut() {
        const on = tsEnabledEl.checked && !tsEnabledEl.disabled;
        tsHostnameEl.disabled = !on;
        tsAuthKeyEl.disabled = !on;
        container
            .querySelector(".settings-tailscale")
            .classList.toggle("is-disabled", !on);
    }
    function syncTailscaleStationGating() {
        const stationOn = stationEnabledEl.checked;
        tsEnabledEl.disabled = !stationOn;
        if (!stationOn) tsEnabledEl.checked = false;
        syncTailscaleGrayOut();
    }
    tsEnabledEl.addEventListener("change", syncTailscaleGrayOut);
    // Reveal the WS2812B-only ordering control when the operator picks
    // that output mode; hide otherwise so it doesn't clutter HDMI / HUB75.
    function syncWs281xOrderVisibility() {
        ws281xOrderWrap.hidden = outputModeEl.value !== "ws281x";
    }
    outputModeEl.addEventListener("change", syncWs281xOrderVisibility);
    function guardDisableBoth(toggledEl, otherEl) {
        // If the user just turned off the LAST enabled mode, bounce the
        // checkbox back on and flash a status message.
        if (!apEnabledEl.checked && !stationEnabledEl.checked) {
            toggledEl.checked = true;
            statusEl.textContent =
                "Can't disable both WiFi modes — the device wouldn't be reachable.";
        }
    }
    apEnabledEl.addEventListener("change", () => {
        guardDisableBoth(apEnabledEl, stationEnabledEl);
        syncWifiGrayOut();
    });
    stationEnabledEl.addEventListener("change", () => {
        guardDisableBoth(stationEnabledEl, apEnabledEl);
        syncWifiGrayOut();
        syncTailscaleStationGating();
    });

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
            apEnabledEl.checked = settings.wifi_ap_enabled !== false; // default on
            ssidEl.value = settings.wifi_ssid ?? "";
            // Round-trip the real password so the operator can resubmit
            // without retyping it. The captive-portal API already returns
            // it in plaintext on GET — this is just hydrating the form.
            passwordEl.value = settings.wifi_password ?? "";
            stationEnabledEl.checked = Boolean(settings.wifi_station_enabled);
            stationSsidEl.value = settings.wifi_station_ssid ?? "";
            stationPasswordEl.value = settings.wifi_station_password ?? "";
            ws281xOrderEl.value = settings.ws281x_pixel_order || "row_major";
            syncWifiGrayOut();
            syncTailscaleStationGating();
            syncWs281xOrderVisibility();
            setTimezoneValue(tzEl, settings.timezone || "");
            tsEnabledEl.checked = Boolean(settings.tailscale_enabled);
            tsHostnameEl.value = settings.tailscale_hostname ?? "";
            tsAuthKeyEl.value = settings.tailscale_auth_key ?? "";
            // Trigger a wifi scan in the background so the dropdown is
            // useful by the time the operator gets to it.
            populateWifiScan();
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
                wifi_ap_enabled: apEnabledEl.checked,
                wifi_ssid: ssidEl.value,
                wifi_password: passwordEl.value,
                wifi_station_enabled: stationEnabledEl.checked,
                wifi_station_ssid: stationSsidEl.value.trim() || null,
                wifi_station_password: stationPasswordEl.value || null,
                ws281x_pixel_order: ws281xOrderEl.value || "row_major",
                timezone: tzEl.value || null,
                tailscale_enabled: tsEnabledEl.checked,
                tailscale_hostname: tsHostnameEl.value.trim() || null,
                tailscale_auth_key: tsAuthKeyEl.value || null,
            };
            await onSave(payload);
            statusEl.textContent = "Saved.";
            // Tell the rest of the app the settings changed. main.js
            // re-mounts the editor + uploader panels with fresh dims so
            // the canvas size matches the operator's new display config.
            // Existing stored slides keep their old-dim PNGs until
            // re-saved — that's expected, not a bug.
            document.dispatchEvent(
                new CustomEvent("openmarquee:settings-updated", {
                    detail: { settings: payload },
                }),
            );
        } catch (err) {
            statusEl.textContent = `Save failed: ${err.message}`;
        } finally {
            saveBtn.disabled = false;
        }
    });

    // --- detect dims from device's framebuffer / display probe ---

    detectBtn.addEventListener("click", async () => {
        detectStatusEl.textContent = "Probing display…";
        try {
            const res = await fetch("/api/system/display-dims");
            const data = await res.json();
            if (data.width && data.height) {
                widthEl.value = String(data.width);
                heightEl.value = String(data.height);
                detectStatusEl.textContent = `Detected ${data.width}×${data.height} (${data.source}).`;
            } else {
                detectStatusEl.textContent =
                    "Couldn't detect — type the dims manually.";
            }
        } catch (err) {
            detectStatusEl.textContent = `Probe failed: ${err.message}`;
        }
    });

    // --- WiFi scan: populate the SSID picker so operator picks from
    //     nearby networks. "Other" reveals the manual text input. ---

    async function populateWifiScan() {
        try {
            const res = await fetch("/api/system/wifi-scan");
            if (!res.ok) return;
            const data = await res.json();
            const networks = Array.isArray(data?.networks) ? data.networks : [];
            const currentSsid = stationSsidEl.value;
            stationSsidPickerEl.innerHTML = "";
            for (const net of networks) {
                const opt = document.createElement("option");
                opt.value = net.ssid;
                const sig = net.signal_dbm != null ? ` (${net.signal_dbm} dBm)` : "";
                opt.textContent = `${net.ssid}${sig}`;
                stationSsidPickerEl.appendChild(opt);
            }
            const otherOpt = document.createElement("option");
            otherOpt.value = "__other__";
            otherOpt.textContent = "(type manually)";
            stationSsidPickerEl.appendChild(otherOpt);

            // Sync picker selection to the current text value.
            const known = networks.some((n) => n.ssid === currentSsid);
            if (known) {
                stationSsidPickerEl.value = currentSsid;
            } else {
                stationSsidPickerEl.value = "__other__";
            }
            stationSsidEl.hidden = stationSsidPickerEl.value !== "__other__";
        } catch (err) {
            // Silent — picker stays as the "(type manually)" fallback.
            console.debug("[settings] wifi-scan failed:", err);
        }
    }
    stationSsidPickerEl.addEventListener("change", () => {
        if (stationSsidPickerEl.value === "__other__") {
            stationSsidEl.hidden = false;
            stationSsidEl.focus();
        } else {
            stationSsidEl.hidden = true;
            stationSsidEl.value = stationSsidPickerEl.value;
        }
    });
    wifiRescanBtn.addEventListener("click", populateWifiScan);

    // --- ticking device-time display ---

    function tickNow() {
        if (!nowValueEl) return;
        const now = new Date();
        const options = {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
            weekday: "short",
            hour12: false,
        };
        if (tzEl.value) options.timeZone = tzEl.value;
        try {
            nowValueEl.textContent = new Intl.DateTimeFormat(
                undefined,
                options,
            ).format(now);
        } catch {
            delete options.timeZone;
            nowValueEl.textContent = new Intl.DateTimeFormat(
                undefined,
                options,
            ).format(now);
        }
    }
    setInterval(tickNow, 1000);

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
