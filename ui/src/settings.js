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
import { mountPerfHistogramControl } from "./perf-histogram.js";

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
                        <select class="om-pulldown om-pulldown-cased field-output-mode"></select>
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
                            <select class="om-pulldown om-pulldown-cased field-display-rotation"></select>
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
                        <select class="om-pulldown om-pulldown-cased field-ws281x-pixel-order">
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
                                <input type="radio" name="wifi-mode" value="ap" class="field-wifi-ap-enabled" checked>
                                Create WiFi Network
                            </label>
                        </legend>
                        <p class="field-hint" style="margin: 4px 0 10px;">
                            The sign broadcasts its own WiFi network so a phone can connect
                            directly to set it up — no cables, no console. It also turns its
                            network on automatically if the sign loses its home wifi, so you
                            can always reconnect and fix it.
                        </p>
                        <div class="row" style="gap: 10px;">
                            <label class="field om-field" style="flex: 1;">
                                <span>Network name (SSID)</span>
                                <input type="text" class="om-input field-wifi-ssid" maxlength="32">
                            </label>
                            <label class="field om-field secret-field" style="flex: 1;" data-secret="wifi-ap-password">
                                <span>Network password (8-63 chars)</span>
                                <div class="secret-display"
                                     style="display: flex; gap: 8px; align-items: center; padding: 6px 0;">
                                    <span class="secret-status" style="font-family: var(--om-mono); color: var(--om-text-dim); font-size: 12px;"></span>
                                    <button type="button" class="om-btn sm secret-change-btn">Change…</button>
                                </div>
                                <div class="secret-form" hidden style="display: grid; gap: 6px; margin-top: 4px;">
                                    <input type="password" class="om-input secret-current-password"
                                           placeholder="Current login password" autocomplete="current-password">
                                    <input type="password" class="om-input secret-new-value"
                                           placeholder="New network password (8-63 chars)" minlength="8" maxlength="63">
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
                                <input type="radio" name="wifi-mode" value="station" class="field-wifi-station-enabled">
                                Join Existing Network
                            </label>
                        </legend>
                        <!-- qarl 2026-07-16: the sign was sitting on NEBULA while
                             this section rendered a BLANK SSID box. Actual cause:
                             the box below binds to the PERSISTED
                             settings.wifi_station_ssid, which is seeded ONLY by
                             captive-portal onboarding (api_onboarding
                             submit_credentials). A sign provisioned via the
                             saved-networks/supervisor path never gets it written
                             — that path doesn't touch the field — so the box
                             renders blank on a sign that is happily connected.
                             The row above shows the LIVE association instead.
                             This row answers "what are we actually on?" from the
                             LIVE association, the same source the boot card uses,
                             and the box below is prefilled from it. -->
                        <p class="field-wifi-connected-row settings-hint"
                           style="margin: 0 0 10px; display: flex; gap: 8px; align-items: baseline;">
                            <span>Currently connected:</span>
                            <span class="field-wifi-connected-ssid om-mono"
                                  role="status" aria-live="polite"
                                  style="color: var(--om-accent); font-size: 13px;">—</span>
                        </p>
                        <p class="settings-hint" style="margin: 0 0 10px;">
                            Pick a saved network or type one below to join it now.
                            Networks you add to <strong>Saved networks</strong> are
                            auto-joined later without asking.
                        </p>
                        <div class="row" style="gap: 10px;">
                            <label class="field om-field" style="flex: 1;">
                                <span>Join this network</span>
                                <select class="om-pulldown om-pulldown-cased field-wifi-station-ssid-picker">
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
                        <p class="settings-wifi-status" role="status" aria-live="polite" hidden
                           style="margin: 6px 0 0; color: var(--om-text-dim); font-size: 12px;"></p>
                        <p class="field-hint" style="margin: 6px 0 0;">
                            The sign uses this network as its primary connection. If it
                            drops off (wrong password, network out of range, router down)
                            the sign turns its own WiFi network back on so you can join it
                            directly and fix things.
                        </p>

                        <!-- 2026-07-03 (qarl handover Phase B2): multi-network
                             wifi list. The device round-robins these on link
                             loss so a sign with 3 saved networks (qarl / NEBULA /
                             admin) auto-recovers when the primary drops.
                             2026-07-14 (qarl): moved INTO the Join Existing
                             Network section (was a sibling below Tailscale);
                             hidden entirely when Create WiFi Network is
                             selected — the list is only relevant when joining. -->
                        <fieldset class="settings-wifi-networks">
                            <legend>Saved networks</legend>
                            <p class="settings-hint">
                                Networks the sign can auto-join, in priority order.
                                The device rotates through them if the current one
                                drops. New devices adopt any pre-existing NetworkManager
                                profiles on first boot.
                            </p>
                            <ul class="field-wifi-networks-list"
                                style="list-style: none; padding: 0; margin: 8px 0; display: flex; flex-direction: column; gap: 6px;">
                                <!-- rendered by renderWifiNetworksList() -->
                            </ul>
                            <p class="field-wifi-networks-empty"
                               hidden
                               style="margin: 6px 0; color: var(--om-text-dim); font-size: 13px;">
                                No networks saved yet. Add one below.
                            </p>
                            <div class="field-wifi-networks-add"
                                 style="display: grid; gap: 8px; margin-top: 12px; padding: 10px; background: var(--om-card-bg-2); border-radius: 8px;">
                                <div style="font-family: var(--om-mono); font-size: 11px; letter-spacing: 0.12em; color: var(--om-text-fade); text-transform: uppercase;">Add network</div>
                                <div class="row" style="gap: 8px;">
                                    <label class="field om-field" style="flex: 1;">
                                        <span>SSID</span>
                                        <input type="text" class="om-input field-wifi-networks-add-ssid"
                                               maxlength="32" placeholder="Network name" autocomplete="off">
                                    </label>
                                    <label class="field om-field" style="flex: 1;">
                                        <span>Password</span>
                                        <!-- No minlength on the DOM node: attachAutoSave gates
                                             saves on form.reportValidity(), so a partial password
                                             here would block autosave on ALL other fields until
                                             cleared. JS handler (see click listener below)
                                             enforces the 8-63 chars rule at Add time instead. -->
                                        <input type="password" class="om-input field-wifi-networks-add-password"
                                               maxlength="63" placeholder="8-63 chars"
                                               autocomplete="new-password">
                                    </label>
                                </div>
                                <div style="display: flex; gap: 8px; align-items: center;">
                                    <button type="button" class="om-btn primary sm field-wifi-networks-add-btn">
                                        Add network
                                    </button>
                                    <p class="field-wifi-networks-add-status"
                                       role="status" aria-live="polite"
                                       style="margin: 0; color: var(--om-text-dim); font-size: 12px;"></p>
                                </div>
                            </div>
                        </fieldset>

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
                            <!-- qarl 2026-07-16: the tailnet-hostname control is GONE.
                                 The system hostname is the single source of truth now --
                                 name_actuator propagates it to Tailscale, avahi, hostapd and
                                 settings, and reconcile_names_from_hostname_at_boot re-derives
                                 every surface from it on each backend start. A second,
                                 TS-specific name control could only ever disagree with that,
                                 which is the two-way-sync trap that renamed a sign back to
                                 fireplaceSign. Omitting tailscale_hostname from the PUT leaves
                                 it None, which the field itself documents as "defaults to the
                                 operating-system hostname when unset". -->
                            <div class="row" style="gap: 10px; align-items: flex-end;">
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
                            <!-- r53 (2026-06-03): HTTPS provisioning toggle.
                                 Closes the 9-day backlog from r49 F078 +
                                 project_https_phase_1_shipped memory.
                                 When checked, the device runs tailscale serve
                                 with bg + https=443 so the magic-DNS FQDN
                                 serves HTTPS (FqdnRedirectMiddleware also 301s
                                 non-FQDN traffic to the canonical FQDN). When
                                 unchecked, the device serves HTTP only on
                                 port 80. Settings.tailscale_https_enabled
                                 default in the model is True; checked-by-
                                 default here matches that. -->
                            <label class="field-inline" style="margin-top: 10px; display: flex; gap: 8px; align-items: center;">
                                <input type="checkbox" class="field-tailscale-https-enabled">
                                <span>Enable HTTPS on Tailscale FQDN</span>
                            </label>
                        </fieldset>
                    </fieldset>
                </div>
            </div>

            <div class="om-card">
                <div ${CARD_EYEBROW}>Time</div>
                <label class="field om-field">
                    <span>Timezone</span>
                    <select class="om-pulldown om-pulldown-cased field-timezone">
                        <option value="">Device local (no explicit timezone)</option>
                    </select>
                </label>
            </div>

            <!-- Perf-night r2 (2026-05-26): toggleable operator-
                 facing perf overlay (corner-floating fixed-position
                 panel). The toggle persists in localStorage as
                 'om.perf.show' (perf-overlay.js). Settings doesn't
                 own the overlay lifecycle - flipping the toggle just
                 dispatches 'openmarquee:perf-overlay-toggle'; main.js
                 listens + mounts/unmounts. Off by default. -->
            <div class="om-card settings-perf-overlay">
                <div ${CARD_EYEBROW}>Diagnostics</div>
                <label class="field-inline" style="display: flex; align-items: center; gap: 8px;">
                    <input type="checkbox" class="field-perf-overlay-toggle">
                    <span>Show perf overlay (live fps + over-budget rate)</span>
                </label>
                <p class="settings-hint" style="margin: 8px 0 0; color: var(--om-text-dim); font-size: 12px;">
                    Floats a small panel in the corner with the renderer's last 30s window stats.
                    Threshold-coded color: green &lt;5%, yellow &lt;15%, red &gt;=15% frames over the 30fps budget.
                </p>
                <!-- Perf-night r8 (2026-05-26, r2.5 follow-up): phase
                     histogram capture button. Mounts independently of
                     the corner-overlay toggle (operator may want to
                     capture without showing the overlay). The output
                     <pre> is rendered inline in this card rather than
                     in the corner overlay: ~30 lines of histogram text
                     are too big for the corner widget. Backed by
                     code1's /api/playback/perf/start + dump endpoints
                     (commit dbca3e2 on origin/main). -->
                <div class="settings-perf-histogram-slot" style="margin-top: 12px;"></div>
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

            <!-- Recovery A4 (2026-07-08): operator-triggered reboot.
                 Two-step inline confirm (no window.confirm — a native
                 dialog blocks the captive-portal page). POSTs
                 /api/system/restart, which crosses to the root netctl
                 daemon (systemctl reboot). -->
            <div class="om-card settings-device">
                <div ${CARD_EYEBROW}>Device</div>
                <p class="settings-hint" style="margin: 0 0 12px;">
                    Restart the sign. The screen goes dark briefly and
                    playback resumes automatically after about a minute.
                </p>
                <div class="device-restart-display"
                     style="display: flex; gap: 8px; align-items: center;">
                    <button type="button" class="om-btn sm device-restart-btn">Restart device…</button>
                    <span class="device-restart-status"
                          role="status" aria-live="polite"
                          style="font-family: var(--om-mono); color: var(--om-text-dim); font-size: 12px;"></span>
                </div>
                <div class="device-restart-confirm" hidden
                     style="display: grid; gap: 6px; margin-top: 10px;">
                    <p style="margin: 0; font-size: 13px;">
                        Restart now? The sign will be unreachable for about a minute.
                    </p>
                    <div style="display: flex; gap: 6px;">
                        <button type="button" class="om-btn primary sm device-restart-go">Restart</button>
                        <button type="button" class="om-btn sm device-restart-cancel">Cancel</button>
                    </div>
                </div>

                <!-- Recovery A3 (2026-07-08): factory reset. DESTRUCTIVE —
                     wipes all content + settings + wifi and restarts into
                     setup. Guarded by a type-to-confirm (the operator must
                     type "factory-reset") so a stray click can't erase the
                     sign; the confirm token is echoed to the API. -->
                <div class="device-factory-reset"
                     style="margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--om-hairline, rgba(128,128,128,0.25));">
                    <p class="settings-hint" style="margin: 0 0 10px; color: #ff8f6b;">
                        <strong>Factory reset</strong> erases all slides, playlists,
                        schedules, settings and saved Wi-Fi, then restarts the sign in
                        setup mode. The sign's name and setup network stay the same.
                        This can't be undone.
                    </p>
                    <div class="device-factory-display"
                         style="display: flex; gap: 8px; align-items: center;">
                        <button type="button" class="om-btn sm device-factory-btn">Factory reset…</button>
                        <span class="device-factory-status"
                              role="status" aria-live="polite"
                              style="font-family: var(--om-mono); color: var(--om-text-dim); font-size: 12px;"></span>
                    </div>
                    <div class="device-factory-confirm" hidden
                         style="display: grid; gap: 6px; margin-top: 10px;">
                        <label class="field om-field" style="margin: 0;">
                            <span style="font-size: 12px;">Type <code>factory-reset</code> to confirm</span>
                            <input type="text" class="om-input device-factory-input"
                                   autocomplete="off" autocapitalize="off" spellcheck="false"
                                   placeholder="factory-reset">
                        </label>
                        <div style="display: flex; gap: 6px;">
                            <button type="button" class="om-btn primary sm device-factory-go" disabled
                                    style="background: #c0392b; border-color: #c0392b;">
                                Erase &amp; restart
                            </button>
                            <button type="button" class="om-btn sm device-factory-cancel">Cancel</button>
                        </div>
                    </div>
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
    const tsAuthKeyEl = container.querySelector(".field-tailscale-auth-key");
    const tsHttpsEnabledEl = container.querySelector(
        ".field-tailscale-https-enabled",
    );
    const tsStateEl = container.querySelector(".field-tailscale-state");
    const tsEnableBtn = container.querySelector(".field-tailscale-enable-btn");
    const tsAuthBox = container.querySelector(".field-tailscale-auth");
    const tsAuthUrlEl = container.querySelector(".field-tailscale-auth-url");
    const tsAuthPollEl = container.querySelector(".field-tailscale-auth-poll");
    const nowValueEl = container.querySelector('[data-field="device-now-value"]');

    // 2026-07-03 (qarl handover Phase B2): multi-network wifi_networks
    // list + add-form. Kept in a local mutable array (`wifiNetworks`)
    // that the render + collectPayload paths both read; the operator's
    // add/remove/edit actions mutate the array + trigger autosave.
    const wifiNetworksListEl = container.querySelector(
        ".field-wifi-networks-list",
    );
    const wifiNetworksEmptyEl = container.querySelector(
        ".field-wifi-networks-empty",
    );
    const wifiNetworksAddSsidEl = container.querySelector(
        ".field-wifi-networks-add-ssid",
    );
    const wifiNetworksAddPasswordEl = container.querySelector(
        ".field-wifi-networks-add-password",
    );
    const wifiNetworksAddBtn = container.querySelector(
        ".field-wifi-networks-add-btn",
    );
    const wifiNetworksAddStatusEl = container.querySelector(
        ".field-wifi-networks-add-status",
    );

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

    // WiFi mode radios (single-select): gray out the non-selected mode's
    // fieldset and disable its inputs. Exactly one radio is always active,
    // so the both-off state the server validator rejects is structurally
    // unreachable here.
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
        // 2026-07-14 (qarl): the saved-networks list lives inside the Join
        // Existing Network section; hide it entirely (not just gray it)
        // when Create is selected — it's only relevant when joining.
        const networksFieldset = container.querySelector(".settings-wifi-networks");
        if (networksFieldset) networksFieldset.hidden = !stationOn;
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
    // qarl 2026-07-14: the two WiFi modes are single-select radios
    // (name="wifi-mode") — exactly one is active at a time. The radio
    // group makes the both-off state structurally unreachable, so the
    // old "can't disable both" guard is gone (the server validator
    // _check_wifi_has_at_least_one_mode_enabled still backstops it).
    // A radio 'change' fires only on the newly-selected input, so both
    // handlers re-sync the whole WiFi + Tailscale gating.
    function syncWifiMode() {
        syncWifiGrayOut();
        syncTailscaleStationGating();
    }
    apEnabledEl.addEventListener("change", syncWifiMode);
    stationEnabledEl.addEventListener("change", syncWifiMode);

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

            const { close } = wireRevealForm({
                display, formEl, changeBtn, cancelBtn, saveBtn, errorEl,
                inputs: [currentPwEl, newValueEl],
            });

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
                    if (!(await handleSecretResponseError(response, errorEl, saveBtn))) return;
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

        const { close } = wireRevealForm({
            display, formEl, changeBtn, cancelBtn, saveBtn, errorEl,
            inputs: [currentEl, newEl, confirmEl],
        });

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
                if (!(await handleSecretResponseError(response, errorEl, saveBtn))) return;
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

    // Recovery A4 (2026-07-08): two-step inline Restart-device control.
    // Clicking "Restart device…" reveals an inline confirm (native
    // window.confirm would block the captive-portal page). Confirming
    // POSTs /api/system/restart; the backend returns 202 immediately
    // (systemctl reboot only enqueues) so we show a reconnect hint
    // rather than waiting on a response the reboot would kill.
    function wireDeviceRestart() {
        const btn = container.querySelector(".device-restart-btn");
        const confirmBox = container.querySelector(".device-restart-confirm");
        const goBtn = container.querySelector(".device-restart-go");
        const cancelBtn = container.querySelector(".device-restart-cancel");
        const status = container.querySelector(".device-restart-status");
        if (!btn || !confirmBox || !goBtn || !cancelBtn || !status) return;

        function reset() {
            confirmBox.hidden = true;
            btn.hidden = false;
            goBtn.disabled = false;
            cancelBtn.disabled = false;
        }
        btn.addEventListener("click", () => {
            status.textContent = "";
            btn.hidden = true;
            confirmBox.hidden = false;
        });
        cancelBtn.addEventListener("click", reset);
        goBtn.addEventListener("click", async () => {
            goBtn.disabled = true;
            cancelBtn.disabled = true;
            status.textContent = "Restarting…";
            try {
                // apiFetch throws on 401 (redirect to login) but returns
                // the raw Response for other non-2xx, so check .ok for
                // the 503 the endpoint raises when the netctl daemon is
                // unreachable.
                const res = await apiFetch("/api/system/restart", { method: "POST" });
                if (!res.ok) {
                    let detail = `HTTP ${res.status}`;
                    try {
                        const body = await res.json();
                        if (body?.detail) detail = body.detail;
                    } catch { /* non-JSON body; keep the status code */ }
                    throw new Error(detail);
                }
                confirmBox.hidden = true;
                btn.hidden = true;
                status.textContent =
                    "Restarting — this page will reconnect in about a minute.";
            } catch (err) {
                // 503 (daemon/socket unavailable) or network error. Let
                // the operator retry rather than leaving a dead button.
                status.textContent = `Restart failed: ${err.message}`;
                reset();
            }
        });
    }
    wireDeviceRestart();

    // Recovery A3 (2026-07-08): DESTRUCTIVE factory reset. Type-to-confirm
    // (must type "factory-reset") so a stray click can't erase the sign;
    // the same token is POSTed as the API's confirm guard. On success the
    // sign wipes + reboots, so we show a terminal status rather than
    // waiting on a response the reboot kills.
    const FACTORY_CONFIRM = "factory-reset";
    function wireDeviceFactoryReset() {
        const btn = container.querySelector(".device-factory-btn");
        const confirmBox = container.querySelector(".device-factory-confirm");
        const input = container.querySelector(".device-factory-input");
        const goBtn = container.querySelector(".device-factory-go");
        const cancelBtn = container.querySelector(".device-factory-cancel");
        const status = container.querySelector(".device-factory-status");
        if (!btn || !confirmBox || !input || !goBtn || !cancelBtn || !status) return;

        function reset() {
            confirmBox.hidden = true;
            btn.hidden = false;
            input.value = "";
            goBtn.disabled = true;
            cancelBtn.disabled = false;
        }
        btn.addEventListener("click", () => {
            status.textContent = "";
            btn.hidden = true;
            confirmBox.hidden = false;
            input.focus();
        });
        // The Erase button only enables once the exact phrase is typed.
        input.addEventListener("input", () => {
            goBtn.disabled = input.value.trim() !== FACTORY_CONFIRM;
        });
        cancelBtn.addEventListener("click", reset);
        goBtn.addEventListener("click", async () => {
            if (input.value.trim() !== FACTORY_CONFIRM) return;
            goBtn.disabled = true;
            cancelBtn.disabled = true;
            status.textContent = "Erasing…";
            try {
                const res = await apiFetch("/api/system/factory-reset", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ confirm: FACTORY_CONFIRM }),
                });
                if (!res.ok) {
                    let detail = `HTTP ${res.status}`;
                    try {
                        const b = await res.json();
                        if (b?.detail) detail = b.detail;
                    } catch { /* non-JSON body; keep the status code */ }
                    throw new Error(detail);
                }
                confirmBox.hidden = true;
                btn.hidden = true;
                status.textContent =
                    "Erasing and restarting — reconnect to the sign's setup network in a minute.";
            } catch (err) {
                status.textContent = `Factory reset failed: ${err.message}`;
                reset();
            }
        });
    }
    wireDeviceFactoryReset();

    // Perf-night r2 (2026-05-26): toggle for the corner perf overlay.
    // Settings owns the checkbox state + localStorage write; main.js
    // owns the overlay lifecycle. Decoupled via the
    // `openmarquee:perf-overlay-toggle` custom event so the overlay
    // survives Settings panel un-mount / re-mount cycles.
    function wirePerfOverlayToggle() {
        const card = container.querySelector(".settings-perf-overlay");
        if (!card) return;
        const checkbox = card.querySelector(".field-perf-overlay-toggle");
        if (!checkbox) return;
        // Initialize from localStorage so a page reload preserves the
        // operator's last choice.
        try {
            checkbox.checked = localStorage.getItem("om.perf.show") === "1";
        } catch {
            checkbox.checked = false;
        }
        checkbox.addEventListener("change", () => {
            try {
                if (checkbox.checked) {
                    localStorage.setItem("om.perf.show", "1");
                } else {
                    localStorage.removeItem("om.perf.show");
                }
            } catch { /* private-browsing — checkbox state still drives the event */ }
            document.dispatchEvent(
                new CustomEvent("openmarquee:perf-overlay-toggle", {
                    detail: { enabled: checkbox.checked },
                }),
            );
        });
    }
    wirePerfOverlayToggle();

    // Perf-night r8 (r2.5 follow-up): mount the histogram capture
    // control inside the Diagnostics card. Independent of the
    // perf-overlay toggle — operator can capture a histogram
    // without showing the corner overlay. Handle stored at
    // module-scope so a Settings panel un-mount destroys it
    // cleanly.
    let perfHistogramHandle = null;
    function wirePerfHistogramControl() {
        const slot = container.querySelector(".settings-perf-histogram-slot");
        if (!slot) return;
        perfHistogramHandle = mountPerfHistogramControl({
            container: slot,
        });
    }
    wirePerfHistogramControl();

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
            gammaEl.value = String(settings.gamma ?? 1.0);
            // Single-select radios (qarl 2026-07-14: "only one mode at a
            // time"). Legacy settings may have BOTH modes enabled (the old
            // concurrent AP+STA regime); collapse to one selection — prefer
            // Join when station is enabled, else Create (the fresh-device
            // default is AP-only). On the next save, collectPayload writes
            // the mutually-exclusive pair, migrating a concurrent device to
            // a single mode. Recovery still works: if STA drops, the network
            // supervisor turns the AP back on regardless of this flag.
            const wifiJoinSelected = Boolean(settings.wifi_station_enabled);
            apEnabledEl.checked = !wifiJoinSelected;
            ssidEl.value = settings.wifi_ssid ?? "";
            // Batch 20.4: GET returns "<set>" / null for the 3 secret
            // fields. The hidden inputs hold the wire value verbatim so
            // PUT can echo them back (the backend substitutes the
            // sentinel for the stored value, so a Save without a
            // Change-form rotation is a no-op for these fields).
            passwordEl.value = settings.wifi_password ?? "";
            updateSecretIndicator("wifi-ap-password", settings.wifi_password);
            stationEnabledEl.checked = wifiJoinSelected;
            stationSsidEl.value = settings.wifi_station_ssid ?? "";
            stationPasswordEl.value = settings.wifi_station_password ?? "";
            updateSecretIndicator("wifi-station-password", settings.wifi_station_password);
            ws281xOrderEl.value = settings.ws281x_pixel_order || "row_major";
            // Hydrate tailscale state BEFORE the sync calls — syncTailscale*
            // reads tsEnabledEl.checked to decide dim / disabled state, so
            // the wrong class would stick on first paint if this came after.
            tsEnabledEl.checked = Boolean(settings.tailscale_enabled);
            tsAuthKeyEl.value = settings.tailscale_auth_key ?? "";
            // r53: hydrate HTTPS toggle. Model default is True so a
            // legacy settings.json missing the key reads as true here
            // (matches the device's at-rest behavior; the boot path
            // already provisions HTTPS).
            tsHttpsEnabledEl.checked = settings.tailscale_https_enabled !== false;
            // Phase B2: hydrate the multi-network list. The backend
            // returns password as SECRET_SENTINEL "<set>" (or null) so
            // the plaintext PSK never leaves the device; we hold the
            // sentinel verbatim so a subsequent PUT round-trips it and
            // the server preserves the stored value.
            wifiNetworks.splice(0, wifiNetworks.length);
            for (const entry of settings.wifi_networks || []) {
                wifiNetworks.push({
                    ssid: entry.ssid,
                    password: entry.password ?? null,
                    autoconnect: entry.autoconnect !== false,
                    priority: Number(entry.priority ?? 0),
                });
            }
            renderWifiNetworksList();
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
    let tsPollStartedAt = null;
    // 5 minutes. Empirically, operators who haven't opened the auth URL
    // within this window are unlikely to do so soon — the poll keeps
    // hitting the backend forever otherwise. The retry path is just
    // clicking "Enable Tailscale" again, which restarts the timer.
    const TAILSCALE_AUTH_TIMEOUT_MS = 5 * 60 * 1000;
    function stopTsPoll() {
        if (tsPollTimer) clearInterval(tsPollTimer);
        tsPollTimer = null;
        tsPollStartedAt = null;
    }
    function setTsState(label, color) {
        if (!tsStateEl) return;
        tsStateEl.textContent = label;
        tsStateEl.style.color = color || "var(--om-text-dim)";
    }
    async function pollTailscaleStatus() {
        // Bound the poll lifetime — without this the interval ran
        // forever if the operator never opened the auth URL.
        if (
            tsPollStartedAt !== null
            && Date.now() - tsPollStartedAt > TAILSCALE_AUTH_TIMEOUT_MS
        ) {
            stopTsPoll();
            setTsState("Sign-in timed out");
            tsAuthPollEl.textContent =
                "Sign-in took too long — click \"Enable Tailscale\" again to retry.";
            return;
        }
        try {
            const res = await apiFetch("/api/system/tailscale/status");
            const data = await res.json();
            if (data.state === "authenticated") {
                const label = data.hostname
                    ? `Authenticated as ${data.hostname}`
                    : "Authenticated";
                setTsState(label, "#7dd87a");
                tsAuthBox.hidden = true;
                stopTsPoll();
            } else if (data.state === "error") {
                setTsState("Error", "#ff6b6b");
                tsAuthPollEl.textContent =
                    data.message || "Couldn't read Tailscale status.";
                // Stop polling — the error is unlikely to clear
                // without operator action; let them click Enable
                // again to restart.
                stopTsPoll();
            } else if (data.state === "disabled") {
                setTsState("Disabled");
                // Operator declined or `tailscale up` exited before
                // sign-in. Stop polling; the auth URL is stale anyway.
                stopTsPoll();
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
                stopTsPoll();
                tsPollStartedAt = Date.now();
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

    // 2026-07-03 (qarl handover Phase B2): local mirror of the
    // multi-network wifi list. Persisted via the same autosave PUT
    // that shipped display / hostname / etc., so no separate
    // sub-endpoint is needed. See collectPayload for the wire shape.
    // Sentinel-preserve pattern: existing entries carry password:
    // "<set>" verbatim, and the backend's PSK-redaction logic in
    // SettingsStorage recognises the sentinel + keeps the stored
    // PSK. New entries carry the operator-typed plaintext.
    const wifiNetworks = [];

    // Layer 3 (Option D, 2026-07-14, qarl's owner decision): per-network
    // on-demand PSK reveal. The reveal reads NetworkManager (which holds
    // the PSK) via the re-auth-gated POST endpoint, so it's offered for
    // EVERY saved network regardless of the masked indicator (which
    // reflects settings.json, not NM — an imported Layer-A network shows
    // "no password" there yet may still have a PSK in NM). Per-row state:
    // idle -> re-auth prompt -> shown (PSK + copy) OR not-stored OR
    // error, then back to idle. The reveal is transient; auto-hides so a
    // PSK doesn't linger on screen, and a list re-render resets it.
    const REVEAL_AUTO_HIDE_MS = 30_000;
    // Track pending auto-hide timers across ALL rows so a list re-render
    // (which detaches rows) can cancel them — otherwise a detached row's
    // timer would keep its (revealed-PSK) subtree alive in memory until
    // it fired. renderWifiNetworksList clears these on every rebuild.
    const revealHideTimers = new Set();
    function wireNetworkReveal(entry, revealBtn, revealArea, pwEl) {
        let hideTimer = null;
        function toIdle() {
            if (hideTimer) {
                clearTimeout(hideTimer);
                revealHideTimers.delete(hideTimer);
                hideTimer = null;
            }
            revealArea.hidden = true;
            revealArea.innerHTML = "";
            revealBtn.hidden = false;
            pwEl.hidden = false;
        }
        function showNotStored() {
            revealArea.innerHTML = "";
            const msg = document.createElement("div");
            msg.className = "field-wifi-networks-item-reveal-notstored";
            msg.style.cssText =
                "display:flex; gap:8px; align-items:center; margin-top:6px; "
                + "font-size:12px; color: var(--om-text-dim);";
            const text = document.createElement("span");
            text.textContent = "Password not stored on-device.";
            text.title =
                "The sign has this network saved but never captured its "
                + "password, so it can't be shown here.";
            const okBtn = document.createElement("button");
            okBtn.type = "button";
            okBtn.className = "om-btn sm field-wifi-networks-item-reveal-close";
            okBtn.textContent = "OK";
            okBtn.addEventListener("click", toIdle);
            msg.append(text, okBtn);
            revealArea.appendChild(msg);
        }
        function showRevealed(psk) {
            revealArea.innerHTML = "";
            const shown = document.createElement("div");
            shown.className = "field-wifi-networks-item-reveal-shown";
            shown.style.cssText =
                "display:flex; gap:8px; align-items:center; flex-wrap:wrap; "
                + "margin-top:6px;";
            const pskEl = document.createElement("span");
            pskEl.className = "field-wifi-networks-item-reveal-value";
            pskEl.style.cssText =
                "font-family: var(--om-mono); font-size:13px; word-break:break-all;";
            pskEl.textContent = psk;
            const copyBtn = document.createElement("button");
            copyBtn.type = "button";
            copyBtn.className = "om-btn sm field-wifi-networks-item-reveal-copy";
            copyBtn.textContent = "Copy";
            copyBtn.addEventListener("click", async () => {
                try {
                    if (!navigator.clipboard?.writeText) {
                        copyBtn.textContent = "Copy unavailable";
                        return;
                    }
                    await navigator.clipboard.writeText(psk);
                    copyBtn.textContent = "Copied";
                } catch {
                    copyBtn.textContent = "Copy failed";
                }
            });
            const hideBtn = document.createElement("button");
            hideBtn.type = "button";
            hideBtn.className = "om-btn sm field-wifi-networks-item-reveal-hide";
            hideBtn.textContent = "Hide";
            hideBtn.addEventListener("click", toIdle);
            shown.append(pskEl, copyBtn, hideBtn);
            revealArea.appendChild(shown);
            // Auto-hide so a revealed PSK doesn't linger on-screen. Track
            // the timer so a list re-render can cancel it (toIdle keeps the
            // registry in sync when it fires or is called manually).
            hideTimer = setTimeout(toIdle, REVEAL_AUTO_HIDE_MS);
            revealHideTimers.add(hideTimer);
        }
        function showPrompt() {
            revealBtn.hidden = true;
            pwEl.hidden = true;
            revealArea.hidden = false;
            revealArea.innerHTML = "";
            const promptEl = document.createElement("div");
            promptEl.className = "field-wifi-networks-item-reveal-form";
            promptEl.style.cssText =
                "display:flex; gap:6px; align-items:center; flex-wrap:wrap; "
                + "margin-top:6px;";
            const pwInput = document.createElement("input");
            pwInput.type = "password";
            pwInput.className = "om-input field-wifi-networks-item-reveal-pw";
            pwInput.placeholder = "Current login password";
            pwInput.autocomplete = "current-password";
            pwInput.style.cssText = "flex:1; min-width:160px;";
            const submitBtn = document.createElement("button");
            submitBtn.type = "button";
            submitBtn.className =
                "om-btn primary sm field-wifi-networks-item-reveal-submit";
            submitBtn.textContent = "Reveal";
            const cancelBtn = document.createElement("button");
            cancelBtn.type = "button";
            cancelBtn.className = "om-btn sm field-wifi-networks-item-reveal-cancel";
            cancelBtn.textContent = "Cancel";
            const errEl = document.createElement("span");
            errEl.className = "field-wifi-networks-item-reveal-error";
            errEl.setAttribute("role", "alert");
            errEl.setAttribute("aria-live", "polite");
            errEl.style.cssText = "color:#ff6b6b; font-size:12px; width:100%;";
            promptEl.append(pwInput, submitBtn, cancelBtn, errEl);
            revealArea.appendChild(promptEl);
            pwInput.focus();
            cancelBtn.addEventListener("click", toIdle);
            submitBtn.addEventListener("click", async () => {
                errEl.textContent = "";
                submitBtn.disabled = true;
                try {
                    const response = await apiFetch(
                        `/api/settings/network/${encodeURIComponent(entry.ssid)}`
                            + "/reveal-password",
                        {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({
                                current_password: pwInput.value,
                            }),
                            // 401 here = wrong LOGIN password (a re-auth
                            // signal), not a stale bearer -> surface inline
                            // rather than redirecting to /login.html.
                            skipAuth401Redirect: true,
                        },
                    );
                    if (response.status === 401) {
                        errEl.textContent = "Wrong password.";
                        submitBtn.disabled = false;
                        return;
                    }
                    if (!response.ok) {
                        errEl.textContent =
                            "Couldn't read the password from the sign.";
                        submitBtn.disabled = false;
                        return;
                    }
                    const data = await response.json();
                    if (data && data.stored) {
                        showRevealed(String(data.password ?? ""));
                    } else {
                        showNotStored();
                    }
                } catch (err) {
                    // apiFetch throws + redirects on a real 401 (stale
                    // bearer); otherwise surface the message inline.
                    errEl.textContent = err?.message || "Network error.";
                    submitBtn.disabled = false;
                }
            });
        }
        revealBtn.addEventListener("click", showPrompt);
    }

    function renderWifiNetworksList() {
        // Cancel any pending reveal auto-hide timers from rows we're about
        // to detach — otherwise a detached row's timer keeps its (possibly
        // revealed-PSK) subtree alive in memory until it fires.
        for (const t of revealHideTimers) clearTimeout(t);
        revealHideTimers.clear();
        // Rebuild the whole <ul> — the list is small (typically ≤5
        // entries on a device) so full re-render is simpler than
        // diffing.
        wifiNetworksListEl.innerHTML = "";
        if (wifiNetworks.length === 0) {
            wifiNetworksEmptyEl.hidden = false;
            return;
        }
        wifiNetworksEmptyEl.hidden = true;
        wifiNetworks.forEach((entry, index) => {
            const li = document.createElement("li");
            li.className = "field-wifi-networks-item";
            li.dataset.index = String(index);
            li.style.cssText =
                "display: flex; flex-direction: column; align-items: stretch; "
                + "padding: 8px 10px; border-radius: 6px; "
                + "background: var(--om-card-bg-2);";
            const rowEl = document.createElement("div");
            rowEl.className = "field-wifi-networks-item-row";
            rowEl.style.cssText = "display: flex; gap: 10px; align-items: center;";
            const ssidEl = document.createElement("span");
            ssidEl.className = "field-wifi-networks-item-ssid";
            ssidEl.style.cssText =
                "font-family: var(--om-mono); font-size: 13px; flex: 1;";
            ssidEl.textContent = entry.ssid;
            const pwEl = document.createElement("span");
            pwEl.className = "field-wifi-networks-item-password";
            pwEl.style.cssText =
                "font-family: var(--om-mono); font-size: 12px; color: var(--om-text-dim);";
            pwEl.textContent = entry.password ? "password: <set>" : "no password";
            // Layer 3 (Option D): per-network on-demand PSK reveal.
            const revealBtn = document.createElement("button");
            revealBtn.type = "button";
            revealBtn.className = "om-btn sm field-wifi-networks-item-reveal";
            revealBtn.textContent = "Show password";
            const removeBtn = document.createElement("button");
            removeBtn.type = "button";
            removeBtn.className = "om-btn sm field-wifi-networks-item-remove";
            removeBtn.textContent = "Remove";
            removeBtn.addEventListener("click", () => {
                wifiNetworks.splice(index, 1);
                renderWifiNetworksList();
                // Fire autosave: the form-level input event is the
                // trigger attachAutoSave listens for.
                form.dispatchEvent(new Event("input", { bubbles: true }));
            });
            rowEl.append(ssidEl, pwEl, revealBtn, removeBtn);
            const revealArea = document.createElement("div");
            revealArea.className = "field-wifi-networks-item-reveal-area";
            revealArea.hidden = true;
            li.append(rowEl, revealArea);
            wireNetworkReveal(entry, revealBtn, revealArea, pwEl);
            wifiNetworksListEl.appendChild(li);
        });
    }

    wifiNetworksAddBtn?.addEventListener("click", () => {
        const ssid = wifiNetworksAddSsidEl.value.trim();
        const password = wifiNetworksAddPasswordEl.value;
        wifiNetworksAddStatusEl.textContent = "";
        if (!ssid) {
            wifiNetworksAddStatusEl.textContent = "SSID required.";
            return;
        }
        if (ssid.length > 32) {
            wifiNetworksAddStatusEl.textContent = "SSID must be 32 chars or fewer.";
            return;
        }
        if (password && (password.length < 8 || password.length > 63)) {
            wifiNetworksAddStatusEl.textContent =
                "Password must be 8-63 chars.";
            return;
        }
        if (wifiNetworks.some((n) => n.ssid === ssid)) {
            wifiNetworksAddStatusEl.textContent = `"${ssid}" is already in the list.`;
            return;
        }
        wifiNetworks.push({
            ssid,
            password: password || null,
            autoconnect: true,
            priority: 0,
        });
        wifiNetworksAddSsidEl.value = "";
        wifiNetworksAddPasswordEl.value = "";
        renderWifiNetworksList();
        // Trigger autosave — attachAutoSave listens for input events
        // on any form-descendant field. dispatchEvent 'input' at the
        // form level is the canonical way to kick a save without
        // requiring the operator to touch another field.
        form.dispatchEvent(new Event("input", { bubbles: true }));
        wifiNetworksAddStatusEl.textContent = `Added "${ssid}".`;
    });

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
            // Phase B2: multi-network list. Pass through verbatim
            // (including sentinel `<set>` values for existing PSKs)
            // so a save that only touches display fields doesn't
            // rotate the stored passwords.
            wifi_networks: wifiNetworks.map((n) => ({
                ssid: n.ssid,
                password: n.password,
                autoconnect: n.autoconnect !== false,
                priority: Number(n.priority) || 0,
            })),
            ws281x_pixel_order: ws281xOrderEl.value || "row_major",
            timezone: tzEl.value || null,
            tailscale_enabled: tsEnabledEl.checked,
            tailscale_auth_key: tsAuthKeyEl.value || null,
            // r53: HTTPS toggle. The field is bool (not nullable);
            // checkbox.checked is the canonical source of truth.
            tailscale_https_enabled: tsHttpsEnabledEl.checked,
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

    // Helper for the two silent-fail paths in populateWifiScan. Same
    // shape as bg-picker's statusEl pattern (7aeec9a). Console log
    // preserved for devtools / journald correlation; operator gets a
    // visible breadcrumb so the "(type manually)" fallback doesn't
    // read as a missing feature.
    /**
     * Show the network wlan0 is CURRENTLY associated with, and seed the
     * Join box with it.
     *
     * Reads /api/settings/wifi-station-state. `connected_ssid` is the
     * LIVE association (active_wlan0_ssid) — NOT the applier's `ssid`
     * (that's the last submitted TARGET) and NOT the persisted
     * `wifi_station_ssid` (which nothing writes — the actual cause of
     * qarl's blank box).
     *
     * THREE distinct answers, never conflated:
     *   connected_probe_ok && ssid  -> that network
     *   connected_probe_ok && !ssid -> "Not connected" (a real answer)
     *   !connected_probe_ok         -> "Unknown" (we could not tell)
     * Rendering unknown as "Not connected" on a connected sign would
     * reproduce the bug this fixes.
     */
    async function refreshConnectedSsid() {
        const el = container.querySelector(".field-wifi-connected-ssid");
        if (!el) return;
        try {
            const res = await apiFetch("/api/settings/wifi-station-state");
            if (!res.ok) {
                // Don't leave a PRIOR success standing: on a rescan after
                // the sign drops off wifi, a stale "NEBULA" would assert a
                // state we just failed to verify.
                el.textContent = "Unknown";
                return;
            }
            const data = await res.json();
            const ssid = data?.connected_ssid;
            // Three states, and the split is the point: only a probe that
            // ANSWERED may produce "Not connected". An unreadable link is
            // "Unknown" — asserting not-connected on a sign we couldn't
            // read is the bug this row exists to kill.
            if (!data?.connected_probe_ok) {
                el.textContent = "Unknown";
                return;
            }
            if (typeof ssid === "string" && ssid) {
                el.textContent = ssid;
                // Prefill the Join box so the section stops showing an
                // empty field on a connected sign. Only when the operator
                // hasn't typed their own target — never clobber input.
                if (stationSsidEl && !stationSsidEl.value.trim()) {
                    stationSsidEl.value = ssid;
                }
            } else {
                el.textContent = "Not connected";
            }
        } catch (err) {
            // Same reasoning as the !res.ok path: never let a previous
            // answer stand in for one we couldn't get.
            el.textContent = "Unknown";
            console.debug("[settings] connected-ssid probe failed:", err);
        }
    }

    function surfaceWifiScanError(reason) {
        console.debug("[settings] wifi-scan failed:", reason);
        const status = container.querySelector(".settings-wifi-status");
        if (!status) return;
        status.textContent = `WiFi scan unavailable: ${reason}`;
        status.hidden = false;
    }

    async function populateWifiScan() {
        // Clear any prior failure breadcrumb at the start of each
        // attempt -- the rescan button is the operator's "try again"
        // affordance, and a stale message past a fresh success would
        // mislead.
        const status = container.querySelector(".settings-wifi-status");
        if (status) {
            status.textContent = "";
            status.hidden = true;
        }
        // qarl 2026-07-16: also refresh the LIVE connected SSID + seed
        // the Join box from it. The box binds to a persisted setting
        // nothing writes, so it rendered blank on a sign happily joined
        // to NEBULA.
        // AWAITED, not fire-and-forget: populateWifiScan reads
        // stationSsidEl.value into currentSsid below to sync the picker.
        // Racing the prefill against that read leaves the box showing
        // NEBULA while the picker says "(type manually)" whenever the
        // scan (a real nmcli rescan) happens to answer first.
        await refreshConnectedSsid();
        try {
            const res = await apiFetch("/api/system/wifi-scan");
            if (!res.ok) {
                surfaceWifiScanError(`HTTP ${res.status}`);
                return;
            }
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
            surfaceWifiScanError(err.message || String(err));
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
        // Replaces the pre-fix `if (!nowValueEl) return;` defensive
        // check, which was UNREACHABLE: nowValueEl is captured at
        // mount time (line ~367) and never reassigned, so once it's
        // non-null at mount, it stays non-null forever -- even after
        // the element is detached from the DOM. Optional-chained
        // .isConnected gives the check teeth: returns false when the
        // node is detached, AND tolerates the (impossible-today-but-
        // safe-anyway) nowValueEl === null path.
        if (!nowValueEl?.isConnected) return;
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
    // Tracked for teardown via the destroy() handle below. Sibling
    // contrast at settings.js:847 (pollTailscaleStatus, 3s) does the
    // same dance via tsPollTimer + clearInterval at :779 -- the
    // discipline was applied once in this module and missed here
    // pre-fix.
    let tickTimer = setInterval(tickNow, 1000);

    refresh();
    return {
        refresh,
        destroy() {
            // Idempotent: calling twice is safe (the second call
            // is a no-op because tickTimer is already null).
            if (tickTimer !== null) {
                clearInterval(tickTimer);
                tickTimer = null;
            }
            // Perf-night r8: tear down the histogram capture control
            // so any in-flight fetch is aborted + the DOM is cleaned.
            // The control's own destroy() is idempotent; clearing the
            // handle prevents a second destroy() call from re-entering.
            if (perfHistogramHandle !== null) {
                perfHistogramHandle.destroy();
                perfHistogramHandle = null;
            }
        },
    };
}

// Shared reveal-form lifecycle for the two secret-bearing forms
// (wireSecretFields per-row + wireChangePasswordCard). Each form has
// a display row (with the redacted indicator + a Change button) and
// a hidden form (with the current_password + new_value(+confirm)
// inputs + Cancel/Save buttons).
//
// open() and close() toggle the display/form visibility, clear all
// inputs, clear the inline error, and (in clear()) reset
// saveBtn.disabled. The shared `clear()` keeps the saveBtn reset
// invariant in one place — pre-extract, Site 1 (wireSecretFields)
// reset it only on close(), while Site 2 (wireChangePasswordCard)
// reset it on BOTH open() and close(). Unified to reset on both via
// clear(); Site 1's open() gains a redundant reset that's a no-op in
// practice (saveBtn would already be false at open-time because
// close() runs before any subsequent open()).
//
// The saveBtn-disabled gotcha: saveBtn gets .disabled=true on submit
// entry; the error paths re-enable but the success path calls close()
// before resetting it. Reset in clear() so a second submit in the
// same session isn't dead.
//
// Returns { open, close } so the caller's submit-success path can
// invoke close() directly.
function wireRevealForm({ display, formEl, changeBtn, cancelBtn, saveBtn, inputs, errorEl }) {
    function clear() {
        for (const el of inputs) el.value = "";
        errorEl.textContent = "";
        saveBtn.disabled = false;
    }
    function open() {
        display.hidden = true;
        formEl.hidden = false;
        clear();
        inputs[0].focus();
    }
    function close() {
        formEl.hidden = true;
        display.hidden = false;
        clear();
    }
    changeBtn.addEventListener("click", open);
    cancelBtn.addEventListener("click", close);
    return { open, close };
}

// Shared response-error handler for the two secret-bearing save flows
// (wireSecretFields + wireChangePasswordCard). Both endpoints use
// skipAuth401Redirect, so 401 here means "wrong current_password" not
// "session expired" — surface inline with a specific message.
//
// Returns true if the response was OK (caller proceeds); false if an
// error was surfaced inline (caller returns). The caller pattern is:
//   if (!(await handleSecretResponseError(response, errorEl, saveBtn))) return;
async function handleSecretResponseError(response, errorEl, saveBtn) {
    if (response.status === 401) {
        errorEl.textContent = "Incorrect current password.";
        saveBtn.disabled = false;
        return false;
    }
    if (!response.ok) {
        let detail = `HTTP ${response.status}`;
        try {
            const body = await response.json();
            if (typeof body.detail === "string") detail = body.detail;
        } catch { /* ignore */ }
        errorEl.textContent = detail;
        saveBtn.disabled = false;
        return false;
    }
    return true;
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
