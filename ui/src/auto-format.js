// Client-side mirror of openmarquee.auto_render.render_auto_text —
// formats a JS Date for the preview's ticking overlay on auto-mode
// text slides. Uses the browser's local timezone (the backend renders
// with the device's configured IANA tz on actual hardware, so preview
// and device can drift by up to a zone; documented trade-off).
//
// Extracted into its own module so both the editor's live preview
// and the Playlists-panel inline preview can import it without a
// circular dep.

const MONTHS_LONG = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
];
const MONTHS_SHORT = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];
const DAYS_LONG = [
    "Sunday", "Monday", "Tuesday", "Wednesday",
    "Thursday", "Friday", "Saturday",
];
const DAYS_SHORT = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

export function formatAutoText(mode, format, now) {
    const fmt = format || defaultFormatFor(mode);
    if (mode === "time") {
        if (fmt === "time_hms") {
            return `${pad2(now.getHours())}:${pad2(now.getMinutes())}:${pad2(now.getSeconds())}`;
        }
        return `${pad2(now.getHours())}:${pad2(now.getMinutes())}`;
    }
    if (mode === "date") {
        if (fmt === "date_iso") {
            return `${now.getFullYear()}-${pad2(now.getMonth() + 1)}-${pad2(now.getDate())}`;
        }
        if (fmt === "date_medium") {
            return `${MONTHS_SHORT[now.getMonth()]} ${now.getDate()}`;
        }
        return `${MONTHS_LONG[now.getMonth()]} ${now.getDate()}, ${now.getFullYear()}`;
    }
    if (mode === "day") {
        if (fmt === "day_short") return DAYS_SHORT[now.getDay()];
        return DAYS_LONG[now.getDay()];
    }
    return "";
}

function pad2(n) {
    return String(n).padStart(2, "0");
}

function defaultFormatFor(mode) {
    if (mode === "time") return "time_hm";
    if (mode === "date") return "date_iso";
    if (mode === "day") return "day_long";
    return null;
}
