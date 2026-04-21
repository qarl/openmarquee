// Auto slides: dynamic content that updates at play time without operator
// intervention — time-of-day, date, day-of-week, weather, countdowns.
//
// This is a placeholder UI for the upcoming feature. The actual variant
// (`AutoSlide` / server-side rendering at playback time) lands when we
// decide on the templating shape. Today this panel renders a preview of
// what a time-of-day slide would look like + notes the feature is
// pending, so the subpage feels real rather than empty.

const TEMPLATE = `
    <section class="auto-slide">
        <h2 class="auto-slide-heading">Auto slides</h2>
        <p class="auto-slide-hint">
            Dynamic content rendered on the device at playback time: current
            time, today's date, day of week, weather, countdowns. No
            operator-typed text — the device fills the slide itself.
        </p>

        <div class="preview-wrap">
            <canvas class="auto-slide-canvas" aria-label="auto-slide preview"></canvas>
        </div>

        <div class="row">
            <label class="field">
                <span>Type</span>
                <select class="auto-slide-type">
                    <option value="time">Current time</option>
                    <option value="date">Today's date</option>
                    <option value="day">Day of week</option>
                </select>
            </label>
        </div>

        <p class="field-hint auto-slide-todo">
            <strong>Preview only.</strong> Server-side Auto slide variant
            + playback-time rendering lands in a follow-up — saving
            isn't wired up yet.
        </p>
    </section>
`;

/**
 * Mount the Auto slides placeholder.
 *
 * @param {HTMLElement} container
 * @param {object} options
 * @param {number} options.width  — panel width (preview canvas pins aspect)
 * @param {number} options.height — panel height
 */
export function mountAutoSlide(container, { width, height }) {
    container.innerHTML = TEMPLATE;
    const canvas = container.querySelector(".auto-slide-canvas");
    canvas.width = width;
    canvas.height = height;
    canvas.style.aspectRatio = `${width} / ${height}`;

    const typeEl = container.querySelector(".auto-slide-type");

    // Live ticker for the preview — redraws every second so the time-of-day
    // option doesn't look frozen. Stopping + restarting is unneeded today
    // because the panel stays mounted (nav.js just toggles `hidden`).
    function redraw() {
        const ctx = canvas.getContext("2d");
        if (!ctx) return;
        const value = render(typeEl.value);
        ctx.save();
        try {
            ctx.fillStyle = "#000000";
            ctx.fillRect(0, 0, canvas.width, canvas.height);
            ctx.fillStyle = "#FFFFFF";
            const fontSize = Math.max(12, Math.floor(canvas.height * 0.3));
            ctx.font = `bold ${fontSize}px sans-serif`;
            ctx.textAlign = "center";
            ctx.textBaseline = "middle";
            ctx.fillText(value, canvas.width / 2, canvas.height / 2, canvas.width - 4);
        } finally {
            ctx.restore();
        }
    }

    typeEl.addEventListener("change", redraw);
    redraw();
    setInterval(redraw, 1000);
}

function render(kind) {
    const now = new Date();
    switch (kind) {
        case "date":
            return now.toLocaleDateString(undefined, {
                month: "short",
                day: "numeric",
            });
        case "day":
            return now.toLocaleDateString(undefined, { weekday: "long" });
        case "time":
        default:
            return now.toLocaleTimeString(undefined, {
                hour: "numeric",
                minute: "2-digit",
            });
    }
}
