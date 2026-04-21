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

        <label class="field">
            <span>Default playlist (when no rule matches)</span>
            <input type="text" class="field-default-playlist" maxlength="64" pattern="[a-z0-9_-]+">
        </label>

        <ul class="schedule-rules" role="list"></ul>

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
 * @returns {{ refresh: () => Promise<void> }}
 */
export function mountSchedule(container, { fetchSchedule, onSave }) {
    container.innerHTML = SECTION_TEMPLATE;
    const defaultEl = container.querySelector(".field-default-playlist");
    const rulesEl = container.querySelector(".schedule-rules");
    const addBtn = container.querySelector(".schedule-add");
    const saveBtn = container.querySelector(".schedule-save");
    const statusEl = container.querySelector(".schedule-status");

    async function refresh() {
        statusEl.textContent = "";
        try {
            const schedule = await fetchSchedule();
            defaultEl.value = schedule.default_playlist_name || "default";
            rulesEl.innerHTML = "";
            for (const rule of schedule.rules || []) {
                rulesEl.appendChild(renderRule(rule));
            }
        } catch (err) {
            statusEl.textContent = `Could not load schedule: ${err.message}`;
        }
    }

    addBtn.addEventListener("click", () => {
        rulesEl.appendChild(
            renderRule({
                name: "New rule",
                days: ["mon", "tue", "wed", "thu", "fri"],
                start_time: "08:00",
                end_time: "17:00",
                playlist_name: "default",
                enabled: true,
            }),
        );
    });

    saveBtn.addEventListener("click", async () => {
        saveBtn.disabled = true;
        statusEl.textContent = "Saving…";
        try {
            const payload = collectSchedule(defaultEl, rulesEl);
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

function renderRule(rule) {
    const li = document.createElement("li");
    li.className = "schedule-rule";
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
                <input type="text" class="rule-playlist" value="${escapeHtml(rule.playlist_name || "default")}"
                       maxlength="64" pattern="[a-z0-9_-]+">
            </label>
        </div>
    `;
    li.querySelector(".rule-remove").addEventListener("click", () => li.remove());
    return li;
}

function collectSchedule(defaultEl, rulesEl) {
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
    };
}

function escapeHtml(value) {
    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}
