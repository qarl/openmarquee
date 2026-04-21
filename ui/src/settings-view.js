// Settings panel — read-only view of the current device config.
//
// The settings backend (model + storage + API) landed with commit
// 5ba120a; the editable form lives in a follow-up commit. Today this
// panel just *displays* the current settings so operators can confirm
// what's stored without dropping into the API. Every field is covered
// because the goal is an audit view: "here is everything my device
// believes about itself right now."
//
// Passwords are displayed as a masked placeholder (dots) — the field
// is available via GET /api/settings for anyone already on the captive
// portal, so the masking is a UX courtesy, not a security boundary.

const SECTION_TEMPLATE = `
    <section class="settings-view">
        <h2 class="settings-heading">System settings</h2>
        <p class="settings-hint">
            Read-only view of the current device configuration. An editable
            form lands in a follow-up.
        </p>
        <dl class="settings-dl"></dl>
        <p class="settings-status" role="status" aria-live="polite"></p>
    </section>
`;

// [key on the API payload, human label, renderer]. Order matters — it's the
// display order in the <dl>.
const FIELDS = [
    ["sign_name", "Sign name", (v) => v],
    ["output_mode", "Output mode", (v) => v],
    ["display_width", "Display width (px)", (v) => String(v)],
    ["display_height", "Display height (px)", (v) => String(v)],
    ["brightness", "Brightness (0-100)", (v) => String(v)],
    ["gamma", "Gamma", (v) => String(v)],
    ["wifi_ssid", "WiFi SSID", (v) => v],
    ["wifi_password", "WiFi password", () => "••••••••"],
    ["timezone", "Timezone", (v) => v || "(device local)"],
    ["schema_version", "Schema version", (v) => String(v)],
];

/**
 * Mount the settings view into `container`.
 *
 * @param {HTMLElement} container — parent (emptied + replaced).
 * @param {object} options
 * @param {() => Promise<object>} options.fetchSettings
 * @returns {{ refresh: () => Promise<void> }}
 */
export function mountSettingsView(container, { fetchSettings }) {
    container.innerHTML = SECTION_TEMPLATE;
    const dl = container.querySelector(".settings-dl");
    const statusEl = container.querySelector(".settings-status");

    async function refresh() {
        statusEl.textContent = "";
        try {
            const settings = await fetchSettings();
            dl.innerHTML = "";
            for (const [key, label, render] of FIELDS) {
                const dt = document.createElement("dt");
                dt.textContent = label;
                const dd = document.createElement("dd");
                dd.textContent = render(settings[key]);
                dd.dataset.key = key;
                dl.append(dt, dd);
            }
        } catch (err) {
            statusEl.textContent = `Could not load settings: ${err.message}`;
        }
    }

    refresh();
    return { refresh };
}
