import { attachAutoSave } from "./auto-save.js";

// Schedule editor: a form for the schedule rules backend (Phase 5 (d)).
// The backend persists time-of-day rules but doesn't yet act on them
// (multi-playlist refactor pending), so this UI is half "dry-run" — users can
// build the schedule they want and it survives across restarts, ready for
// the day playback actually honors it.
//
// Timezone for rule evaluation comes from System Settings — no panel-local
// override here, so operators only set the zone in one place. The IANA
// timezone dropdown helpers are gone from this file with that UI; they
// still live in iana-timezones.js for the Settings panel's use.

const DAYS = [
    { value: "mon", label: "Mon" },
    { value: "tue", label: "Tue" },
    { value: "wed", label: "Wed" },
    { value: "thu", label: "Thu" },
    { value: "fri", label: "Fri" },
    { value: "sat", label: "Sat" },
    { value: "sun", label: "Sun" },
];

const SECTION_TEMPLATE = `
    <section class="schedule">
        <div class="om-page-head">
            <div>
                <span class="om-eyebrow" data-field="now"><span data-field="now-value">—</span></span>
                <h1>Schedule</h1>
                <p>Rules pick which playlist plays when. First matching rule wins; otherwise the default below plays. Timezone comes from Settings.</p>
            </div>
            <div class="om-page-head-actions">
                <button type="button" class="om-btn primary schedule-add">+ New rule</button>
            </div>
        </div>

        <div class="om-card" style="margin-bottom: 12px;">
            <label class="om-field">
                <span>Default playlist (when no rule matches)</span>
                <input type="text" class="om-input field-default-playlist" maxlength="64" pattern="[a-z0-9_-]+">
            </label>
        </div>

        <ul class="schedule-rules" role="list" style="list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 10px;"></ul>

        <div class="om-row" style="gap: 8px; flex-wrap: wrap; margin-top: 14px;">
            <button type="button" class="om-btn ghost sm schedule-enable-all">Enable all</button>
            <button type="button" class="om-btn ghost sm schedule-disable-all">Disable all</button>
        </div>

        <p class="om-save-status schedule-status" role="status" aria-live="polite" data-state="idle"></p>
    </section>
`;

/**
 * Mount the schedule editor into `container`.
 *
 * @param {HTMLElement} container — parent (emptied + replaced).
 * @param {object} options
 * @param {() => Promise<object>} options.fetchSchedule
 * @param {(schedule: object) => Promise<void>} options.onSave
 * @param {() => Promise<string[]>} [options.fetchPlaylistNames] — optional;
 *     when provided, playlist_name fields become <select>s populated from
 *     this list (plus any existing values that aren't in the list, so
 *     round-tripping never silently drops a name).
 * @returns {{ refresh: () => Promise<void> }}
 */
export function mountSchedule(
    container,
    { fetchSchedule, onSave, fetchPlaylistNames, fetchSettings },
) {
    container.innerHTML = SECTION_TEMPLATE;
    const sectionEl = container.querySelector("section.schedule");
    const defaultEl = container.querySelector(".field-default-playlist");
    const rulesEl = container.querySelector(".schedule-rules");
    const addBtn = container.querySelector(".schedule-add");
    const enableAllBtn = container.querySelector(".schedule-enable-all");
    const disableAllBtn = container.querySelector(".schedule-disable-all");
    const statusEl = container.querySelector(".schedule-status");
    const nowValueEl = container.querySelector('[data-field="now-value"]');

    let availableNames = null; // null = no dropdown; array = use <select>
    // TZ carried through saves unchanged — the schedule payload still
    // round-trips any tz the schedule.json has, even though this UI
    // doesn't edit it (authoritative tz lives in System Settings).
    let persistedTz = null;
    // Device tz, pulled once at mount from /api/settings for the
    // ticking current-time display. Falls back to browser local if
    // settings is unreachable.
    let deviceTz = null;

    async function refresh() {
        statusEl.textContent = "";
        try {
            const [schedule, names, settings] = await Promise.all([
                fetchSchedule(),
                fetchPlaylistNames ? fetchPlaylistNames() : Promise.resolve(null),
                fetchSettings ? fetchSettings().catch(() => null) : Promise.resolve(null),
            ]);
            availableNames = names;
            if (availableNames && defaultEl.tagName !== "SELECT") {
                replaceDefaultWithSelect(defaultEl.parentElement, schedule.default_playlist_name);
            }
            setDefaultValue(container, schedule.default_playlist_name || "default");
            persistedTz = schedule.tz || null;
            deviceTz = settings?.timezone || null;
            rulesEl.innerHTML = "";
            for (const rule of schedule.rules || []) {
                rulesEl.appendChild(renderRule(rule, availableNames));
            }
            tickNow();
        } catch (err) {
            statusEl.textContent = `Could not load schedule: ${err.message}`;
        }
    }

    // Current-time display, ticks every second. Uses the device's
    // configured tz so the operator can eyeball whether their "runs
    // weekdays 9–5" rule is about to fire.
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
        if (deviceTz) options.timeZone = deviceTz;
        try {
            nowValueEl.textContent = new Intl.DateTimeFormat(
                undefined,
                options,
            ).format(now);
        } catch {
            // Invalid tz: fall back without a tz option.
            delete options.timeZone;
            nowValueEl.textContent = new Intl.DateTimeFormat(
                undefined,
                options,
            ).format(now);
        }
    }
    const nowInterval = setInterval(tickNow, 1000);
    // Suppress unused-var lint; interval is intentional.
    void nowInterval;

    function replaceDefaultWithSelect(labelEl, currentValue) {
        // Swap the <input class="field-default-playlist"> for a <select>.
        labelEl.querySelector(".field-default-playlist")?.remove();
        const select = document.createElement("select");
        select.className = "field-default-playlist";
        fillPlaylistOptions(select, availableNames, currentValue);
        labelEl.appendChild(select);
    }

    function setDefaultValue(root, value) {
        const el = root.querySelector(".field-default-playlist");
        if (!el) return;
        if (el.tagName === "SELECT") {
            ensureOption(el, value);
            el.value = value;
        } else {
            el.value = value;
        }
    }

    function setAllEnabled(enabled) {
        const boxes = rulesEl.querySelectorAll(".rule-enabled");
        if (boxes.length === 0) {
            statusEl.textContent = "No rules to toggle.";
            return;
        }
        boxes.forEach((cb) => {
            cb.checked = enabled;
        });
        // Programmatic mutation doesn't fire input events automatically —
        // kick auto-save manually so the bulk toggle persists.
        autoSave.kick();
    }

    enableAllBtn.addEventListener("click", () => setAllEnabled(true));
    disableAllBtn.addEventListener("click", () => setAllEnabled(false));

    addBtn.addEventListener("click", async () => {
        // Re-fetch playlist names so a newly-created playlist (via the
        // manager above) shows up in the dropdown without the user having
        // to refresh the page.
        if (fetchPlaylistNames) {
            try {
                availableNames = await fetchPlaylistNames();
            } catch {
                /* fall back to cached list */
            }
        }
        rulesEl.appendChild(
            renderRule(
                {
                    name: "New rule",
                    days: ["mon", "tue", "wed", "thu", "fri"],
                    start_time: "08:00",
                    end_time: "17:00",
                    playlist_name: availableNames?.[0] || "default",
                    enabled: true,
                },
                availableNames,
            ),
        );
        // Programmatic append doesn't fire input events; persist the
        // new rule through auto-save explicitly.
        autoSave.kick();
    });

    async function performSave() {
        const payload = collectSchedule(defaultEl, rulesEl, persistedTz);
        await onSave(payload);
    }

    const autoSave = attachAutoSave(sectionEl, {
        save: performSave,
        status: statusEl,
        debounceMs: 500,
    });

    // Removing a rule is wired in renderRule's listener, but it lives at
    // the row level so we listen at the section level for the synthetic
    // "schedule-rule-removed" event the row dispatches on remove.
    sectionEl.addEventListener("schedule-rule-removed", () => autoSave.kick());

    refresh();
    return { refresh, flushAutoSave: () => autoSave.flush() };
}

function renderRule(rule, availableNames) {
    const li = document.createElement("li");
    li.className = "schedule-rule";
    const playlistValue = rule.playlist_name || "default";
    const playlistControl = availableNames
        ? `<select class="rule-playlist"></select>`
        : `<input type="text" class="rule-playlist" value="${escapeHtml(playlistValue)}"
                  maxlength="64" pattern="[a-z0-9_-]+">`;
    li.innerHTML = `
        <div class="schedule-rule-row">
            <label class="field schedule-rule-name">
                <span>Rule name</span>
                <input type="text" class="rule-name" value="${escapeHtml(rule.name || "")}" maxlength="200">
            </label>
            <label class="field schedule-rule-enabled">
                <span>Enabled</span>
                <input type="checkbox" class="rule-enabled" ${rule.enabled === false ? "" : "checked"}>
            </label>
            <button type="button" class="danger rule-remove" aria-label="Remove rule">Remove</button>
        </div>
        <div class="schedule-rule-row">
            <fieldset class="rule-days-wrap">
                <legend>Days</legend>
                ${DAYS.map(
                    (d) => `
                    <label class="rule-day">
                        <input type="checkbox" class="rule-day-input" value="${d.value}"
                               ${(rule.days || []).includes(d.value) ? "checked" : ""}>
                        <span>${d.label}</span>
                    </label>
                `,
                ).join("")}
            </fieldset>
        </div>
        <div class="schedule-rule-row">
            <label class="field">
                <span>Start (HH:MM)</span>
                <input type="text" class="rule-start" value="${escapeHtml(rule.start_time || "08:00")}" pattern="[0-2][0-9]:[0-5][0-9]">
            </label>
            <label class="field">
                <span>End (HH:MM, 24:00 = end-of-day)</span>
                <input type="text" class="rule-end" value="${escapeHtml(rule.end_time || "17:00")}" pattern="([0-2][0-9]:[0-5][0-9]|24:00)">
            </label>
            <label class="field">
                <span>Playlist</span>
                ${playlistControl}
            </label>
        </div>
    `;
    if (availableNames) {
        const select = li.querySelector(".rule-playlist");
        fillPlaylistOptions(select, availableNames, playlistValue);
    }
    li.querySelector(".rule-remove").addEventListener("click", () => {
        const section = li.closest("section.schedule");
        li.remove();
        // Notify the section so its autoSave hook fires. closest() walks
        // up from the row's pre-removal parent to the right section, so
        // multiple Schedule mounts in the DOM (test harnesses) wouldn't
        // cross-fire.
        if (section) section.dispatchEvent(new CustomEvent("schedule-rule-removed"));
    });
    return li;
}

function fillPlaylistOptions(selectEl, names, currentValue) {
    selectEl.innerHTML = "";
    const seen = new Set();
    for (const name of names) {
        if (seen.has(name)) continue;
        seen.add(name);
        const opt = document.createElement("option");
        opt.value = name;
        opt.textContent = name;
        selectEl.appendChild(opt);
    }
    ensureOption(selectEl, currentValue);
    selectEl.value = currentValue;
}

function ensureOption(selectEl, value) {
    if (!value) return;
    const exists = Array.from(selectEl.options).some((opt) => opt.value === value);
    if (!exists) {
        const opt = document.createElement("option");
        opt.value = value;
        opt.textContent = `${value} (missing)`;
        selectEl.appendChild(opt);
    }
}

function collectSchedule(defaultEl, rulesEl, persistedTz) {
    const rules = Array.from(rulesEl.querySelectorAll(".schedule-rule")).map((li) => ({
        name: li.querySelector(".rule-name").value,
        days: Array.from(li.querySelectorAll(".rule-day-input"))
            .filter((cb) => cb.checked)
            .map((cb) => cb.value),
        start_time: li.querySelector(".rule-start").value,
        end_time: li.querySelector(".rule-end").value,
        playlist_name: li.querySelector(".rule-playlist").value,
        enabled: li.querySelector(".rule-enabled").checked,
    }));
    return {
        rules,
        default_playlist_name: defaultEl.value || "default",
        // Round-trip whatever tz was on disk — the UI doesn't edit it,
        // but persisting untouched keeps backend-side scheduler happy.
        tz: persistedTz,
    };
}

function escapeHtml(value) {
    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}
