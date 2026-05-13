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

import { apiFetch } from "./api.js";
import { attachAutoSave } from "./auto-save.js";
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

// Eyebrow row used at the top of each settings card. Mirrors the
// Claude Design "DISPLAY / NETWORK / TAILSCALE / TIME" pattern —
// uppercase mono micro-label, ~10px, faded color.
const CARD_EYEBROW = `style="font-family: var(--om-mono); font-size: 10.5px; letter-spacing: 0.14em; color: var(--om-text-fade); text-transform: uppercase; margin-bottom: 12px; display: block;"`;

const SECTION_TEMPLATE = `
    <section class="settings">
        <div class="om-page-head">
            <div>
                <span class="om-eyebrow" data-field="device-now">Device · <span data-field="device-now-value">—</span></span>
                <h1>Settings</h1>
                <p>Output mode, network, sync. Changes save to the device's SD card immediately.</p>
            </div>
        </div>

        <form class="settings-form om-stack" autocomplete="off" style="gap: 14px;">

            <div class="om-card">
                <div ${CARD_EYEBROW}>Display</div>
                <div class="om-stack" style="gap: 12px;">
                    <!-- qarl 2026-05-12 (a2): device_id is the
                         factory-stamped MySignXXX identifier set at
                         first boot. IMMUTABLE -- drives hostapd SSID,
                         /etc/hostname, Tailscale magic-DNS. Read-only
                         here; renaming the display below doesn't
                         touch it. Row is hidden off-device (no
                         identity.json) since there's nothing to show. -->
                    <label class="field om-field field-device-id-row" hidden>
                        <span>Device ID</span>
                        <input type="text" class="om-input field-device-id" readonly aria-readonly="true">
                    </label>
                    <label class="field om-field">
                        <span>Display name</span>
                        <input type="text" class="om-input field-sign-name" maxlength="64" required>
                    </label>
                    <label class="field om-field">
                        <span>Output mode</span>
                        <select class="om-select field-output-mode"></select>
                    </label>
                    <div class="row" style="gap: 10px;">
                        <label class="field om-field" style="flex: 1;">
                            <span>Width (px)</span>
                            <input type="number" class="om-input field-display-width" min="1" max="4096" step="1" required>
                        </label>
                        <label class="field om-field" style="flex: 1;">
                            <span>Height (px)</span>
                            <input type="number" class="om-input field-display-height" min="1" max="4096" step="1" required>
                        </label>
                        <label class="field om-field" style="flex: 1;">
                            <span>Rotation</span>
                            <select class="om-select field-display-rotation"></select>
                        </label>
                    </div>
                    <div>
                        <button type="button" class="om-btn sm settings-detect-dims field-hint-btn">
                            Detect from device
                        </button>
                        <p class="field-hint settings-detect-status" role="status" style="margin: 6px 0 0;"></p>
                    </div>
                    <div class="row" style="gap: 10px;">
                        <label class="field om-field" style="flex: 1;">
                            <span>Brightness (0-100)</span>
                            <input type="number" class="om-input field-brightness" min="0" max="100" step="1" required>
                        </label>
                        <label class="field om-field" style="flex: 1;">
                            <span>Gamma</span>
                            <input type="number" class="om-input field-gamma" min="0.1" max="3.0" step="0.1" required>
                        </label>
                    </div>
                    <label class="field om-field settings-ws281x-pixel-order-wrap" hidden>
                        <span>Addressable strip ordering</span>
                        <select class="om-select field-ws281x-pixel-order">
                            <option value="row_major">Row-major (wired raster-order)</option>
                            <option value="serpentine">Serpentine (rows alternate direction)</option>
                        </select>
                    </label>
                </div>
            </div>

            <div class="om-card">
                <div ${CARD_EYEBROW}>Network</div>
                <div class="om-stack" style="gap: 14px;">
                    <fieldset class="settings-wifi-ap">
                        <legend>
                            <label class="field-inline">
                                <input type="checkbox" class="field-wifi-ap-enabled" checked>
                                Access point (captive-portal network phones join during setup)
                            </label>
                        </legend>
                        <div class="row" style="gap: 10px;">
                            <label class="field om-field" style="flex: 1;">
                                <span>AP SSID</span>
                                <input type="text" class="om-input field-wifi-ssid" maxlength="32">
                            </label>
                            <label class="field om-field secret-field" style="flex: 1;" data-secret="wifi-ap-password">
                                <span>AP password (8-63 chars)</span>
                                <div class="secret-display"
                                     style="display: flex; gap: 8px; align-items: center; padding: 6px 0;">
                                    <span class="secret-status" style="font-family: var(--om-mono); color: var(--om-text-dim); font-size: 12px;"></span>
                                    <button type="button" class="om-btn sm secret-change-btn">Change…</button>
                                </div>
                                <div class="secret-form" hidden style="display: grid; gap: 6px; margin-top: 4px;">
                                    <input type="password" class="om-input secret-current-password"
                                           placeholder="Current login password" autocomplete="current-password">
                                    <input type="password" class="om-input secret-new-value"
                                           placeholder="New AP password (8-63 chars)" minlength="8" maxlength="63">
                                    <div style="display: flex; gap: 6px;">
                                        <button type="button" class="om-btn primary sm secret-save-btn">Save</button>
                                        <button type="button" class="om-btn sm secret-cancel-btn">Cancel</button>
                                    </div>
                                    <p class="secret-error" role="alert" aria-live="polite" style="min-height: 1.2em; color: #ff6b6b; font-size: 12px; margin: 0;"></p>
                                </div>
                                <input type="hidden" class="field-wifi-password">
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
                        <div class="row" style="gap: 10px;">
                            <label class="field om-field" style="flex: 1;">
                                <span>WiFi SSID</span>
                                <select class="om-select field-wifi-station-ssid-picker">
                                    <option value="__other__">(type manually)</option>
                                </select>
                                <input type="text" class="om-input field-wifi-station-ssid" maxlength="32" placeholder="SSID">
                            </label>
                            <label class="field om-field secret-field" style="flex: 1;" data-secret="wifi-station-password">
                                <span>WiFi password (8-63 chars)</span>
                                <div class="secret-display"
                                     style="display: flex; gap: 8px; align-items: center; padding: 6px 0;">
                                    <span class="secret-status" style="font-family: var(--om-mono); color: var(--om-text-dim); font-size: 12px;"></span>
                                    <button type="button" class="om-btn sm secret-change-btn">Change…</button>
                                </div>
                                <div class="secret-form" hidden style="display: grid; gap: 6px; margin-top: 4px;">
                                    <input type="password" class="om-input secret-current-password"
                                           placeholder="Current login password" autocomplete="current-password">
                                    <input type="password" class="om-input secret-new-value"
                                           placeholder="New WiFi password (blank = clear)" maxlength="63">
                                    <div style="display: flex; gap: 6px;">
                                        <button type="button" class="om-btn primary sm secret-save-btn">Save</button>
                                        <button type="button" class="om-btn sm secret-cancel-btn">Cancel</button>
                                    </div>
                                    <p class="secret-error" role="alert" aria-live="polite" style="min-height: 1.2em; color: #ff6b6b; font-size: 12px; margin: 0;"></p>
                                </div>
                                <input type="hidden" class="field-wifi-station-password">
                            </label>
                        </div>
                        <button type="button" class="om-btn sm settings-wifi-rescan field-hint-btn">
                            Rescan nearby networks
                        </button>
                        <p class="field-hint" style="margin: 6px 0 0;">
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
                            <!-- qarl 2026-05-12 (arc 4): URL-auth flow.
                                 No more pasting tskey-auth-... from the
                                 admin console. Click Enable → backend
                                 spawns 'tailscale up', captures the auth
                                 URL → operator opens it on their phone /
                                 signs in → poll detects authenticated. -->
                            <div class="row" style="gap: 10px; align-items: flex-end;">
                                <label class="field om-field" style="flex: 1;">
                                    <span>Hostname on tailnet</span>
                                    <input type="text" class="om-input field-tailscale-hostname"
                                           maxlength="63" readonly aria-readonly="true"
                                           title="Pinned to the device ID. Renaming the display label doesn't churn magic-DNS.">
                                </label>
                                <div class="field om-field" style="flex: 1;">
                                    <span class="field-tailscale-state-label">Status</span>
                                    <div style="display: flex; gap: 8px; align-items: center;">
                                        <span class="field-tailscale-state om-mono" style="color: var(--om-text-dim); font-size: 13px;">Disabled</span>
                                        <button type="button" class="om-btn primary sm field-tailscale-enable-btn">
                                            Enable Tailscale
                                        </button>
                                    </div>
                                </div>
                            </div>
                            <div class="field-tailscale-auth" hidden style="margin-top: 10px; padding: 12px; background: var(--om-card-bg-2); border-radius: 8px;">
                                <p style="margin: 0 0 6px;">Open this URL on any device with a browser to finish sign-in:</p>
                                <a class="field-tailscale-auth-url" target="_blank" rel="noopener"
                                   style="font-family: var(--om-mono); font-size: 13px; word-break: break-all;"></a>
                                <p class="field-tailscale-auth-poll" style="margin: 8px 0 0; color: var(--om-text-dim); font-size: 12px;">Waiting for sign-in…</p>
                            </div>
                            <!-- Hidden field carrying the (deprecated) tskey
                                 auth-key for back-compat: the wire schema still
                                 has tailscale_auth_key; the new flow leaves it
                                 empty. Keep the hidden input so collectPayload
                                 echoes the redacted sentinel intact and the
                                 backend's secret-substitution doesn't clobber. -->
                            <input type="hidden" class="field-tailscale-auth-key">
                        </fieldset>
                    </fieldset>
                </div>
            </div>

            <div class="om-card">
                <div ${CARD_EYEBROW}>Time</div>
                <label class="field om-field">
                    <span>Timezone</span>
                    <select class="om-select field-timezone">
                        <option value="">Device local (no explicit timezone)</option>
                    </select>
                </label>
            </div>

            <div class="om-card settings-operator-password">
                <div ${CARD_EYEBROW}>Operator login</div>
                <p class="settings-hint" style="margin: 0 0 12px;">
                    The password gates this editor. Changing it invalidates
                    every existing session on every device.
                </p>
                <div class="change-pw-display"
                     style="display: flex; gap: 8px; align-items: center;">
                    <span class="change-pw-status"
                          style="font-family: var(--om-mono); color: var(--om-text-dim); font-size: 12px;">
                        Set
                    </span>
                    <button type="button" class="om-btn sm change-pw-btn">Change…</button>
                </div>
                <div class="change-pw-form" hidden
                     style="display: grid; gap: 6px; margin-top: 10px;">
                    <input type="password" class="om-input change-pw-current"
                           placeholder="Current password" autocomplete="current-password">
                    <input type="password" class="om-input change-pw-new"
                           placeholder="New password (8+ chars)" minlength="8"
                           autocomplete="new-password">
                    <input type="password" class="om-input change-pw-confirm"
                           placeholder="Confirm new password" minlength="8"
                           autocomplete="new-password">
                    <div style="display: flex; gap: 6px;">
                        <button type="button" class="om-btn primary sm change-pw-save">Save</button>
                        <button type="button" class="om-btn sm change-pw-cancel">Cancel</button>
                    </div>
                    <p class="change-pw-error" role="alert" aria-live="polite"
                       style="min-height: 1.2em; color: #ff6b6b; font-size: 12px; margin: 0;"></p>
                </div>
            </div>

            <!-- qarl 2026-05-12 (arc 3): the explicit "Save settings"
                 submit button is gone -- every field auto-saves on
                 input/change via attachAutoSave (auto-save.js). The
                 status pill below shows the persisted state
                 (saving/saved/error) so the operator has feedback
                 without having to hunt for a button. The 4 inline
                 secret-save buttons (SSH / Wi-Fi STA / Tailscale auth
                 key / change-password) keep their explicit Save
                 affordance because the inputs are write-only +
                 redacted-on-read -- autosaving an empty redacted
                 field every keystroke would clobber the stored secret. -->
            <p class="settings-status om-auto-save-status" role="status"
               aria-live="polite"
               style="margin: 6px 0 0; min-height: 1.2em; color: var(--om-text-dim); font-size: 12.5px;"></p>
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
export function mountSettings(container, { fetchSettings, onSave, debounceMs }) {
    container.innerHTML = SECTION_TEMPLATE;
    const form = container.querySelector(".settings-form");
    const statusEl = container.querySelector(".settings-status");

    const signNameEl = container.querySelector(".field-sign-name");
    const deviceIdEl = container.querySelector(".field-device-id");
    const deviceIdRow = container.querySelector(".field-device-id-row");
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
    const tsStateEl = container.querySelector(".field-tailscale-state");
    const tsEnableBtn = container.querySelector(".field-tailscale-enable-btn");
    const tsAuthBox = container.querySelector(".field-tailscale-auth");
    const tsAuthUrlEl = container.querySelector(".field-tailscale-auth-url");
    const tsAuthPollEl = container.querySelector(".field-tailscale-auth-poll");
    const nowValueEl = container.querySelector('[data-field="device-now-value"]');

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
    //
    // Only add tailscale's own .is-disabled class when station is ON
    // but tailscale is off. When station is off, the parent station's
    // dim styling already covers the whole tailscale subsection; adding
    // tailscale's own dim on top would stack opacity (0.45 × 0.45 ≈ 0.20).
    function syncTailscaleGrayOut() {
        const stationOn = stationEnabledEl.checked;
        const tsOn = tsEnabledEl.checked;
        const bodyEnabled = stationOn && tsOn;
        tsHostnameEl.disabled = !bodyEnabled;
        tsAuthKeyEl.disabled = !bodyEnabled;
        container
            .querySelector(".settings-tailscale")
            .classList.toggle("is-disabled", stationOn && !tsOn);
    }
    function syncTailscaleStationGating() {
        const stationOn = stationEnabledEl.checked;
        // Only disable the checkbox — don't overwrite its checked value.
        // Preserving the user's tailscale preference across a station
        // toggle means re-enabling station doesn't silently wipe what
        // they had set. Persisted state of tailscale=true + station=false
        // is a valid "tailscale is pre-configured, waiting for wifi" state
        // — the tailscale unit just won't start until station comes up.
        tsEnabledEl.disabled = !stationOn;
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
                // arc-3: programmatic .value= writes don't fire input
                // events, so attachAutoSave wouldn't see the new dims.
                // Dispatch synthetic events so autosave picks them up.
                widthEl.dispatchEvent(new Event("input", { bubbles: true }));
                heightEl.dispatchEvent(new Event("input", { bubbles: true }));
            }
        }
    });

    // Batch 20.4: secret-field UI -- redacted display + Change… inline
    // form per field. The hidden inputs (field-wifi-password etc.)
    // still carry whatever GET returned ("<set>" or null) so the main
    // PUT round-trip echoes it back unchanged; rotations land via the
    // PATCH /api/settings/{...} endpoints called directly from this
    // helper. Each .secret-field row is self-contained: it picks up
    // its target endpoint from `data-secret`.
    const PATCH_PATH_BY_SECRET = {
        "wifi-ap-password": "/api/settings/wifi-ap-password",
        "wifi-station-password": "/api/settings/wifi-station-password",
    };

    function updateSecretIndicator(secretId, wireValue) {
        const row = container.querySelector(
            `.secret-field[data-secret="${secretId}"]`,
        );
        if (!row) return;
        const status = row.querySelector(".secret-status");
        // wireValue == "<set>": secret is configured; wireValue == null
        // or "": secret is unset.
        if (wireValue && wireValue === "<set>") {
            status.textContent = "•••• Set";
        } else if (wireValue) {
            // Defensive: a real value somehow leaked into the wire shape
            // (shouldn't happen post-20.4 redaction). Show the same
            // "Set" indicator -- the operator can rotate via Change…
            // and the new value flows through the redacted path.
            status.textContent = "•••• Set";
        } else {
            status.textContent = "Not set";
        }
    }

    function wireSecretFields() {
        for (const row of container.querySelectorAll(".secret-field")) {
            const secretId = row.dataset.secret;
            const display = row.querySelector(".secret-display");
            const formEl = row.querySelector(".secret-form");
            const changeBtn = row.querySelector(".secret-change-btn");
            const cancelBtn = row.querySelector(".secret-cancel-btn");
            const saveBtn = row.querySelector(".secret-save-btn");
            const currentPwEl = row.querySelector(".secret-current-password");
            const newValueEl = row.querySelector(".secret-new-value");
            const errorEl = row.querySelector(".secret-error");

            function open() {
                display.hidden = true;
                formEl.hidden = false;
                currentPwEl.value = "";
                newValueEl.value = "";
                errorEl.textContent = "";
                currentPwEl.focus();
            }
            function close() {
                formEl.hidden = true;
                display.hidden = false;
                currentPwEl.value = "";
                newValueEl.value = "";
                errorEl.textContent = "";
                // 20.4 subagent: saveBtn gets .disabled=true on submit
                // entry; the error paths re-enable but the success path
                // calls close() before resetting it. Reset here so a
                // second rotation in the same session isn't dead.
                saveBtn.disabled = false;
            }

            changeBtn.addEventListener("click", open);
            cancelBtn.addEventListener("click", close);

            saveBtn.addEventListener("click", async () => {
                errorEl.textContent = "";
                saveBtn.disabled = true;
                try {
                    const response = await apiFetch(PATCH_PATH_BY_SECRET[secretId], {
                        method: "PATCH",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            current_password: currentPwEl.value,
                            new_value: newValueEl.value,
                        }),
                        // 20.4: 401 from this endpoint means "wrong
                        // current_password" -- a re-auth gate signal,
                        // not "your bearer token expired". Surface
                        // inline rather than redirecting to /login.html.
                        skipAuth401Redirect: true,
                    });
                    if (response.status === 401) {
                        errorEl.textContent = "Incorrect current password.";
                        saveBtn.disabled = false;
                        return;
                    }
                    if (!response.ok) {
                        let detail = `HTTP ${response.status}`;
                        try {
                            const body = await response.json();
                            if (typeof body.detail === "string") detail = body.detail;
                        } catch { /* ignore */ }
                        errorEl.textContent = detail;
                        saveBtn.disabled = false;
                        return;
                    }
                    // 200: refresh the settings panel so the indicator
                    // updates to reflect the new redacted state.
                    close();
                    await refresh();
                } catch (err) {
                    // apiFetch throws "authentication required" on 401
                    // AND triggers a /login.html redirect -- if we got
                    // here from a 401 the page is already navigating.
                    // Otherwise surface the message inline.
                    errorEl.textContent = err?.message || "Network error.";
                    saveBtn.disabled = false;
                }
            });
        }
    }
    wireSecretFields();

    // Batch 20.5: operator-password Change… card. Mirrors the secret-
    // field pattern but POSTs /api/auth/change-password (3-field
    // body: current_password + new_password + new_password_confirm)
    // and stashes the rotated token in localStorage so subsequent
    // apiFetch calls don't 401-then-redirect on the now-bumped
    // token_version.
    function wireChangePasswordCard() {
        const card = container.querySelector(".settings-operator-password");
        if (!card) return;
        const display = card.querySelector(".change-pw-display");
        const formEl = card.querySelector(".change-pw-form");
        const changeBtn = card.querySelector(".change-pw-btn");
        const cancelBtn = card.querySelector(".change-pw-cancel");
        const saveBtn = card.querySelector(".change-pw-save");
        const currentEl = card.querySelector(".change-pw-current");
        const newEl = card.querySelector(".change-pw-new");
        const confirmEl = card.querySelector(".change-pw-confirm");
        const errorEl = card.querySelector(".change-pw-error");

        function open() {
            display.hidden = true;
            formEl.hidden = false;
            currentEl.value = "";
            newEl.value = "";
            confirmEl.value = "";
            errorEl.textContent = "";
            saveBtn.disabled = false;
            currentEl.focus();
        }
        function close() {
            formEl.hidden = true;
            display.hidden = false;
            currentEl.value = "";
            newEl.value = "";
            confirmEl.value = "";
            errorEl.textContent = "";
            saveBtn.disabled = false;
        }

        changeBtn.addEventListener("click", open);
        cancelBtn.addEventListener("click", close);

        saveBtn.addEventListener("click", async () => {
            errorEl.textContent = "";
            if (newEl.value !== confirmEl.value) {
                errorEl.textContent = "New passwords don't match.";
                return;
            }
            if (newEl.value.length < 8) {
                errorEl.textContent = "New password must be at least 8 characters.";
                return;
            }
            saveBtn.disabled = true;
            try {
                const response = await apiFetch("/api/auth/change-password", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        current_password: currentEl.value,
                        new_password: newEl.value,
                        new_password_confirm: confirmEl.value,
                    }),
                    // 20.5: 401 here means "current_password wrong" --
                    // surface inline rather than bouncing to /login.
                    skipAuth401Redirect: true,
                });
                if (response.status === 401) {
                    errorEl.textContent = "Incorrect current password.";
                    saveBtn.disabled = false;
                    return;
                }
                if (!response.ok) {
                    let detail = `HTTP ${response.status}`;
                    try {
                        const body = await response.json();
                        if (typeof body.detail === "string") detail = body.detail;
                    } catch { /* ignore */ }
                    errorEl.textContent = detail;
                    saveBtn.disabled = false;
                    return;
                }
                // 200: backend bumped token_version + returned a new
                // token. Stash it so the next apiFetch carries it
                // instead of the now-invalidated old one.
                const body = await response.json();
                if (body.token) {
                    try {
                        localStorage.setItem("openmarquee_auth_token", body.token);
                    } catch { /* private-browsing */ }
                }
                close();
                statusEl.textContent = "Password changed.";
            } catch (err) {
                errorEl.textContent = err?.message || "Network error.";
                saveBtn.disabled = false;
            }
        });
    }
    wireChangePasswordCard();

    async function refresh() {
        statusEl.textContent = "";
        try {
            const settings = await fetchSettings();
            // Best-effort fetch /api/system/info for the factory-stamped
            // device_id. Off-device dev (no identity.json) returns null;
            // we hide the row in that case. Failure is non-fatal -- the
            // rest of the settings page must still load.
            let systemInfo = null;
            try {
                const r = await fetch("/api/system/info", {
                    headers: { "Authorization":
                        `Bearer ${localStorage.getItem("openmarquee_auth_token") || ""}` },
                });
                if (r.ok) systemInfo = await r.json();
            } catch { /* network glitch -- leave device_id row hidden */ }
            if (systemInfo?.device_id) {
                deviceIdEl.value = systemInfo.device_id;
                deviceIdRow.hidden = false;
            } else {
                deviceIdRow.hidden = true;
            }
            signNameEl.value = settings.sign_name ?? "";
            ensureSelectValue(outputModeEl, settings.output_mode);
            outputModeEl.value = settings.output_mode ?? "hdmi";
            // Defaults match the backend (HDMI output → 1920×1080) so
            // a response missing dims still paints a consistent mode +
            // size pair.
            widthEl.value = String(settings.display_width ?? 1920);
            heightEl.value = String(settings.display_height ?? 1080);
            rotationEl.value = String(settings.display_rotation ?? 0);
            brightnessEl.value = String(settings.brightness ?? 80);
            gammaEl.value = String(settings.gamma ?? 2.2);
            apEnabledEl.checked = settings.wifi_ap_enabled !== false; // default on
            ssidEl.value = settings.wifi_ssid ?? "";
            // Batch 20.4: GET returns "<set>" / null for the 3 secret
            // fields. The hidden inputs hold the wire value verbatim so
            // PUT can echo them back (the backend substitutes the
            // sentinel for the stored value, so a Save without a
            // Change-form rotation is a no-op for these fields).
            passwordEl.value = settings.wifi_password ?? "";
            updateSecretIndicator("wifi-ap-password", settings.wifi_password);
            stationEnabledEl.checked = Boolean(settings.wifi_station_enabled);
            stationSsidEl.value = settings.wifi_station_ssid ?? "";
            stationPasswordEl.value = settings.wifi_station_password ?? "";
            updateSecretIndicator("wifi-station-password", settings.wifi_station_password);
            ws281xOrderEl.value = settings.ws281x_pixel_order || "row_major";
            // Hydrate tailscale state BEFORE the sync calls — syncTailscale*
            // reads tsEnabledEl.checked to decide dim / disabled state, so
            // the wrong class would stick on first paint if this came after.
            tsEnabledEl.checked = Boolean(settings.tailscale_enabled);
            tsHostnameEl.value = settings.tailscale_hostname ?? "";
            tsAuthKeyEl.value = settings.tailscale_auth_key ?? "";
            syncWifiGrayOut();
            syncTailscaleStationGating();
            syncWs281xOrderVisibility();
            setTimezoneValue(tzEl, settings.timezone || "");
            // Trigger a wifi scan in the background so the dropdown is
            // useful by the time the operator gets to it.
            populateWifiScan();
        } catch (err) {
            statusEl.textContent = `Could not load settings: ${err.message}`;
        }
    }

    // qarl 2026-05-12 (arc 4): Tailscale URL-auth flow. Click Enable
    // -> POST /api/system/tailscale/up -> backend spawns `tailscale up`,
    // captures auth URL, returns it. We surface the URL inline +
    // start polling /api/system/tailscale/status until backend
    // reports BackendState=="Running" (authenticated). State pill
    // updates throughout.
    let tsPollTimer = null;
    function setTsState(label, color) {
        if (!tsStateEl) return;
        tsStateEl.textContent = label;
        tsStateEl.style.color = color || "var(--om-text-dim)";
    }
    async function pollTailscaleStatus() {
        try {
            const res = await apiFetch("/api/system/tailscale/status");
            const data = await res.json();
            if (data.state === "authenticated") {
                const label = data.hostname
                    ? `Authenticated as ${data.hostname}`
                    : "Authenticated";
                setTsState(label, "#7dd87a");
                tsAuthBox.hidden = true;
                if (tsPollTimer) clearInterval(tsPollTimer);
                tsPollTimer = null;
            } else if (data.state === "error") {
                setTsState("Error", "#ff6b6b");
                tsAuthPollEl.textContent =
                    data.message || "Couldn't read Tailscale status.";
                // Stop polling — the error is unlikely to clear
                // without operator action; let them click Enable
                // again to restart.
                if (tsPollTimer) clearInterval(tsPollTimer);
                tsPollTimer = null;
            } else if (data.state === "disabled") {
                setTsState("Disabled");
                // Operator declined or `tailscale up` exited before
                // sign-in. Stop polling; the auth URL is stale anyway.
                if (tsPollTimer) clearInterval(tsPollTimer);
                tsPollTimer = null;
            } else {
                setTsState("Waiting for sign-in…");
            }
        } catch (err) {
            tsAuthPollEl.textContent = `Status check failed: ${err.message}`;
        }
    }
    tsEnableBtn?.addEventListener("click", async () => {
        tsEnableBtn.disabled = true;
        setTsState("Starting…");
        tsAuthBox.hidden = true;
        try {
            const res = await apiFetch("/api/system/tailscale/up", {
                method: "POST",
            });
            const data = await res.json();
            if (data.state === "pending" && data.auth_url) {
                tsAuthUrlEl.textContent = data.auth_url;
                tsAuthUrlEl.href = data.auth_url;
                tsAuthBox.hidden = false;
                setTsState("Waiting for sign-in…");
                if (tsPollTimer) clearInterval(tsPollTimer);
                tsPollTimer = setInterval(pollTailscaleStatus, 3000);
            } else if (data.state === "authenticated") {
                setTsState("Authenticated", "#7dd87a");
            } else {
                setTsState("Error", "#ff6b6b");
                tsAuthPollEl.textContent = data.message || "tailscale up failed.";
                tsAuthBox.hidden = false;
            }
        } catch (err) {
            setTsState("Error", "#ff6b6b");
            tsAuthPollEl.textContent = `Couldn't reach backend: ${err.message}`;
            tsAuthBox.hidden = false;
        } finally {
            tsEnableBtn.disabled = false;
        }
    });
    // On mount, read existing status so an already-authenticated
    // device shows the green pill without the operator clicking
    // Enable.
    pollTailscaleStatus();

    function collectPayload() {
        return {
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
    }

    // qarl 2026-05-12 (arc 3): drop the explicit "Save settings" button
    // and route all field changes through attachAutoSave's debounced
    // PUT. The status pill shows saving / saved / error. Validation
    // runs before each save attempt so a known-invalid form (e.g.
    // empty display name) surfaces the error inline instead of
    // round-tripping to the server.
    const autoSave = attachAutoSave(form, {
        debounceMs: debounceMs,
        validate: () => form.reportValidity() ? "" : "Fix the highlighted field.",
        save: async () => {
            const payload = collectPayload();
            await onSave(payload);
            // Tell the rest of the app the settings changed. main.js
            // re-mounts the editor + uploader panels with fresh dims
            // so the canvas size matches the operator's new display
            // config. Existing stored slides keep their old-dim PNGs
            // until re-saved -- that's expected, not a bug.
            document.dispatchEvent(
                new CustomEvent("openmarquee:settings-updated", {
                    detail: { settings: payload },
                }),
            );
        },
        status: statusEl,
    });

    // --- detect dims from device's framebuffer / display probe ---

    detectBtn.addEventListener("click", async () => {
        detectStatusEl.textContent = "Probing display…";
        try {
            const res = await apiFetch("/api/system/display-dims");
            const data = await res.json();
            if (data.width && data.height) {
                widthEl.value = String(data.width);
                heightEl.value = String(data.height);
                // arc-3: synthetic input event so autosave fires.
                widthEl.dispatchEvent(new Event("input", { bubbles: true }));
                heightEl.dispatchEvent(new Event("input", { bubbles: true }));
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
            const res = await apiFetch("/api/system/wifi-scan");
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
            // arc-3: synthetic input event so autosave persists the
            // picked SSID without requiring a second touch.
            stationSsidEl.dispatchEvent(new Event("input", { bubbles: true }));
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
