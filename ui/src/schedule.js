import { attachAutoSave } from "./auto-save.js";
import { DEFAULT_PLAYLIST_ID } from "./constants.js";

// Schedule editor: time-of-day rules binding (days × HH:MM windows) →
// playlist UUID. The backend's schedule evaluator picks the active
// playlist based on these rules; renaming a playlist doesn't break a
// rule because rules carry the immutable id.
//
// Timezone for rule evaluation comes from System Settings — no panel-local
// override here, so operators only set the zone in one place.

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
                <select class="om-select field-default-playlist"></select>
            </label>
        </div>

        <p class="schedule-empty-hint" hidden>
            Schedule needs at least two playlists to switch between — right
            now only the seeded <strong>default</strong> exists. Create a
            named playlist on the <a href="#/playlists">Playlists</a> page,
            then come back to schedule it.
        </p>

        <ul class="schedule-rules" role="list" style="list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 10px;"></ul>

        <div class="om-row" style="gap: 8px; flex-wrap: wrap; margin-top: 14px;">
            <button type="button" class="om-btn sm schedule-enable-all">Enable all</button>
            <button type="button" class="om-btn sm schedule-disable-all">Disable all</button>
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
 * @param {() => Promise<Array<{id: string, name: string}>>} options.fetchPlaylistChoices
 *   — yields the available playlists. The UI shows display names but
 *   submits stable UUIDs.
 * @returns {{ refresh: () => Promise<void>, flushAutoSave: () => Promise<void> }}
 */
export function mountSchedule(
    container,
    { fetchSchedule, onSave, fetchPlaylistChoices, fetchSettings },
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
    const emptyHintEl = container.querySelector(".schedule-empty-hint");

    // Available playlists for the dropdowns. Each entry: {id, name}.
    let availableChoices = [];
    let persistedTz = null;
    let deviceTz = null;
    // 15.2: keep the full loaded schedule envelope so collectSchedule
    // can round-trip unknown fields (backend Schedule has extra="allow"
    // -- a future zoned-evaluator field or downstream consumer add-on
    // would otherwise be silently dropped on the next save).
    let loadedSchedule = null;

    async function refresh() {
        statusEl.textContent = "";
        try {
            const [schedule, choices, settings] = await Promise.all([
                fetchSchedule(),
                fetchPlaylistChoices ? fetchPlaylistChoices() : Promise.resolve([]),
                fetchSettings ? fetchSettings().catch(() => null) : Promise.resolve(null),
            ]);
            availableChoices = choices || [];
            // Show the "create another playlist first" hint when only the
            // seeded default exists — otherwise every rule the operator
            // adds resolves to the same playlist as the no-rule fallback,
            // i.e. a no-op. Stays hidden once a second playlist appears.
            if (emptyHintEl) {
                const onlyDefault =
                    availableChoices.length <= 1 &&
                    (availableChoices[0]?.id || DEFAULT_PLAYLIST_ID) === DEFAULT_PLAYLIST_ID;
                emptyHintEl.hidden = !onlyDefault;
            }
            fillPlaylistOptions(
                defaultEl,
                availableChoices,
                schedule.default_playlist_id || DEFAULT_PLAYLIST_ID,
            );
            persistedTz = schedule.tz || null;
            deviceTz = settings?.timezone || null;
            loadedSchedule = schedule;
            rulesEl.innerHTML = "";
            for (const rule of schedule.rules || []) {
                rulesEl.appendChild(renderRule(rule, availableChoices));
            }
            tickNow();
        } catch (err) {
            statusEl.textContent = `Could not load schedule: ${err.message}`;
        }
    }

    // Current-time display, ticks every second.
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
            delete options.timeZone;
            nowValueEl.textContent = new Intl.DateTimeFormat(
                undefined,
                options,
            ).format(now);
        }
    }
    const nowInterval = setInterval(tickNow, 1000);
    void nowInterval;

    function setAllEnabled(enabled) {
        const boxes = rulesEl.querySelectorAll(".rule-enabled");
        if (boxes.length === 0) {
            statusEl.textContent = "No rules to toggle.";
            return;
        }
        boxes.forEach((cb) => {
            cb.checked = enabled;
        });
        autoSave.kick();
    }

    enableAllBtn.addEventListener("click", () => setAllEnabled(true));
    disableAllBtn.addEventListener("click", () => setAllEnabled(false));

    addBtn.addEventListener("click", async () => {
        // Re-fetch playlist choices so a newly-created playlist shows
        // up in the dropdown without the user having to refresh the page.
        if (fetchPlaylistChoices) {
            try {
                availableChoices = await fetchPlaylistChoices();
            } catch {
                /* fall back to cached list */
            }
        }
        const firstChoiceId = availableChoices[0]?.id || DEFAULT_PLAYLIST_ID;
        rulesEl.appendChild(
            renderRule(
                {
                    name: "New rule",
                    days: ["mon", "tue", "wed", "thu", "fri"],
                    start_time: "08:00",
                    end_time: "17:00",
                    playlist_id: firstChoiceId,
                    enabled: true,
                },
                availableChoices,
            ),
        );
        autoSave.kick();
    });

    async function performSave() {
        const payload = collectSchedule(defaultEl, rulesEl, persistedTz, loadedSchedule);
        await onSave(payload);
    }

    // Walk every rule and refresh per-card error styling, returning a
    // human-facing message describing the *first* invalid rule (or null
    // when the form is fine). The autosave gate uses the message; CSS
    // uses the .rule-invalid class to outline the offending card so the
    // operator can see which one needs attention without re-reading the
    // status pill.
    function refreshValidation() {
        let firstError = null;
        for (const li of rulesEl.querySelectorAll(".schedule-rule")) {
            const ruleName = () =>
                li.querySelector(".rule-name").value.trim() || "rule";

            // Days fieldset — at least one checked. The server enforces
            // min_length=1 on ScheduleRule.days, so an empty array is a
            // hard 422 we never want to send.
            const dayCount = li.querySelectorAll(".rule-day-input:checked").length;
            const daysWrap = li.querySelector(".rule-days-wrap");
            const noDays = dayCount === 0;
            if (daysWrap) daysWrap.classList.toggle("invalid", noDays);

            // HH:MM inputs — pattern= already paints :invalid via the
            // browser's constraint validation, but the autosave path
            // doesn't go through form-submit, so the bad value would
            // otherwise round-trip to a 422. We mirror the same check
            // here and add an explicit class so styling shows up even on
            // browsers that suppress :invalid for un-touched inputs (they
            // hide it until first interaction).
            const startInput = li.querySelector(".rule-start");
            const endInput = li.querySelector(".rule-end");
            const startBad = !startInput.checkValidity();
            const endBad = !endInput.checkValidity();
            startInput.classList.toggle("input-error", startBad);
            endInput.classList.toggle("input-error", endBad);

            const ruleInvalid = noDays || startBad || endBad;
            li.classList.toggle("rule-invalid", ruleInvalid);

            if (firstError) continue;
            if (noDays) {
                firstError = `Pick at least one day for "${ruleName()}".`;
            } else if (startBad) {
                firstError = `Start time for "${ruleName()}" needs to be HH:MM (e.g. 08:00).`;
            } else if (endBad) {
                firstError = `End time for "${ruleName()}" needs to be HH:MM, or 24:00 for end-of-day.`;
            }
        }
        return firstError;
    }

    const autoSave = attachAutoSave(sectionEl, {
        save: performSave,
        status: statusEl,
        debounceMs: 500,
        validate: refreshValidation,
    });

    // Keep the per-card error styling fresh on every input so the operator
    // sees the outline clear the moment they re-check a day, not only once
    // the debounced autosave runs.
    sectionEl.addEventListener("input", refreshValidation);
    sectionEl.addEventListener("change", refreshValidation);

    sectionEl.addEventListener("schedule-rule-removed", () => autoSave.kick());

    refresh();
    return { refresh, flushAutoSave: () => autoSave.flush() };
}

function renderRule(rule, availableChoices) {
    const li = document.createElement("li");
    li.className = "schedule-rule";
    const playlistId = rule.playlist_id || DEFAULT_PLAYLIST_ID;
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
                <input type="text" class="rule-start" value="${escapeHtml(rule.start_time || "08:00")}" pattern="([01][0-9]|2[0-3]):[0-5][0-9]" placeholder="08:00" inputmode="numeric">
            </label>
            <label class="field">
                <span>End (HH:MM, 24:00 = end-of-day)</span>
                <input type="text" class="rule-end" value="${escapeHtml(rule.end_time || "17:00")}" pattern="(([01][0-9]|2[0-3]):[0-5][0-9]|24:00)" placeholder="17:00" inputmode="numeric">
            </label>
            <label class="field">
                <span>Playlist</span>
                <select class="om-select rule-playlist"></select>
            </label>
        </div>
    `;
    fillPlaylistOptions(li.querySelector(".rule-playlist"), availableChoices, playlistId);
    li.querySelector(".rule-remove").addEventListener("click", () => {
        const section = li.closest("section.schedule");
        li.remove();
        if (section) section.dispatchEvent(new CustomEvent("schedule-rule-removed"));
    });
    return li;
}

/**
 * Populate a <select> with one <option> per choice. Each option's
 * value is the playlist UUID; its label is the display name. If
 * `currentValue` (a UUID) isn't present in `choices` (deleted or
 * unknown), append a "(missing)" option so the round-trip preserves
 * the value rather than silently switching to the first choice.
 */
function fillPlaylistOptions(selectEl, choices, currentValue) {
    selectEl.innerHTML = "";
    const seen = new Set();
    for (const { id, name } of choices) {
        if (seen.has(id)) continue;
        seen.add(id);
        const opt = document.createElement("option");
        opt.value = id;
        opt.textContent = name || "(unnamed)";
        selectEl.appendChild(opt);
    }
    if (currentValue && !seen.has(currentValue)) {
        const opt = document.createElement("option");
        opt.value = currentValue;
        opt.textContent = `${currentValue.slice(0, 8)}… (missing)`;
        selectEl.appendChild(opt);
    }
    selectEl.value = currentValue || "";
}

// Exported for unit tests; not part of the public mountSchedule API.
export function collectSchedule(defaultEl, rulesEl, persistedTz, loadedSchedule) {
    const rules = Array.from(rulesEl.querySelectorAll(".schedule-rule")).map((li) => ({
        name: li.querySelector(".rule-name").value,
        days: Array.from(li.querySelectorAll(".rule-day-input"))
            .filter((cb) => cb.checked)
            .map((cb) => cb.value),
        start_time: li.querySelector(".rule-start").value,
        end_time: li.querySelector(".rule-end").value,
        playlist_id: li.querySelector(".rule-playlist").value,
        enabled: li.querySelector(".rule-enabled").checked,
    }));
    // 15.2: start from the loaded schedule envelope so unknown forward-
    // compat fields round-trip (backend Schedule has extra="allow").
    // Spread BEFORE the explicit overrides so operator-edited values
    // win over the loaded snapshot's stale rules / default / tz.
    return {
        ...(loadedSchedule || {}),
        rules,
        default_playlist_id: defaultEl.value || DEFAULT_PLAYLIST_ID,
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
