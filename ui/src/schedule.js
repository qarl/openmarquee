// Schedule editor: a form for the schedule rules backend (Phase 5 (d)).
// The backend persists time-of-day rules but doesn't yet act on them
// (multi-playlist refactor pending), so this UI is half "dry-run" — users can
// build the schedule they want and it survives across restarts, ready for
// the day playback actually honors it.

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
        <h2 class="schedule-heading">Schedule</h2>
        <p class="schedule-hint">
            Rules pick which playlist plays when. First matching rule wins;
            otherwise the default below plays.
            <em>(Backend persists rules; multi-playlist switching lands later.)</em>
        </p>

        <div class="row">
            <label class="field">
                <span>Default playlist (when no rule matches)</span>
                <input type="text" class="field-default-playlist" maxlength="64" pattern="[a-z0-9_-]+">
            </label>
            <label class="field">
                <span>Timezone (IANA, optional — reserved for future zoned eval)</span>
                <input type="text" class="field-tz" maxlength="64" placeholder="e.g. America/Los_Angeles">
            </label>
        </div>

        <ul class="schedule-rules" role="list"></ul>

        <div class="schedule-bulk">
            <button type="button" class="schedule-enable-all">Enable all</button>
            <button type="button" class="schedule-disable-all">Disable all</button>
        </div>

        <button type="button" class="schedule-add">+ Add rule</button>
        <button type="button" class="primary schedule-save">Save schedule</button>
        <p class="schedule-status" role="status" aria-live="polite"></p>
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
export function mountSchedule(container, { fetchSchedule, onSave, fetchPlaylistNames }) {
    container.innerHTML = SECTION_TEMPLATE;
    const defaultEl = container.querySelector(".field-default-playlist");
    const tzEl = container.querySelector(".field-tz");
    const rulesEl = container.querySelector(".schedule-rules");
    const addBtn = container.querySelector(".schedule-add");
    const saveBtn = container.querySelector(".schedule-save");
    const enableAllBtn = container.querySelector(".schedule-enable-all");
    const disableAllBtn = container.querySelector(".schedule-disable-all");
    const statusEl = container.querySelector(".schedule-status");

    let availableNames = null; // null = no dropdown; array = use <select>

    async function refresh() {
        statusEl.textContent = "";
        try {
            const [schedule, names] = await Promise.all([
                fetchSchedule(),
                fetchPlaylistNames ? fetchPlaylistNames() : Promise.resolve(null),
            ]);
            availableNames = names;
            if (availableNames && defaultEl.tagName !== "SELECT") {
                replaceDefaultWithSelect(defaultEl.parentElement, schedule.default_playlist_name);
            }
            setDefaultValue(container, schedule.default_playlist_name || "default");
            tzEl.value = schedule.tz || "";
            rulesEl.innerHTML = "";
            for (const rule of schedule.rules || []) {
                rulesEl.appendChild(renderRule(rule, availableNames));
            }
        } catch (err) {
            statusEl.textContent = `Could not load schedule: ${err.message}`;
        }
    }

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
        // Mutate DOM only — persistence still flows through the Save button,
        // consistent with how all the other inline edits work. Save schedule
        // turns yellow-accent when there are dirty changes (Phase 6+ polish;
        // deferred for now).
        const boxes = rulesEl.querySelectorAll(".rule-enabled");
        if (boxes.length === 0) {
            statusEl.textContent = "No rules to toggle.";
            return;
        }
        boxes.forEach((cb) => {
            cb.checked = enabled;
        });
        statusEl.textContent = `${enabled ? "Enabled" : "Disabled"} ${boxes.length} rule${
            boxes.length === 1 ? "" : "s"
        } — click Save to persist.`;
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
    });

    saveBtn.addEventListener("click", async () => {
        saveBtn.disabled = true;
        statusEl.textContent = "Saving…";
        try {
            const payload = collectSchedule(defaultEl, rulesEl, tzEl);
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
    li.querySelector(".rule-remove").addEventListener("click", () => li.remove());
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

function collectSchedule(defaultEl, rulesEl, tzEl) {
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
    const tz = tzEl?.value.trim() || null;
    return {
        rules,
        default_playlist_name: defaultEl.value || "default",
        tz,
    };
}

function escapeHtml(value) {
    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}
