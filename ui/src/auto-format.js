// Client-side mirror of openmarquee.auto_render.render_auto_text /
// the renderer's hdmi_logic::format_auto_text — formats a JS Date
// for the preview's ticking overlay on auto-mode text slides.
//
// TIMEZONE (parity follow-up, 2026-05-20): the auto_mode preview
// resolves in the SIGN's configured timezone — `settings.timezone`
// — so the editor preview clock/date matches what the device
// renders on glass. The device renderer resolves local time via
// libc localtime_r with TZ=settings.timezone (commit 4a59d4f);
// this module mirrors that with `Intl.DateTimeFormat`'s
// `timeZone` option. The app calls `setSignTimezone` at boot +
// on a Settings save.
//
// When the sign's timezone is null ("Device local"), the browser
// genuinely cannot know the device's zone (it can't read the Pi's
// /etc/localtime) — the preview falls back to the operator's
// browser-local time. That is an accepted, documented limitation:
// a null timezone means "device local" and the previewer can't
// replicate it without the device reporting its zone (no IPC
// round-trip — deliberately out of scope).
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

// Module-level "sign timezone" — the single app-wide fact of which
// IANA zone the device clock runs in. There is exactly one sign,
// one configured zone, and every auto_mode preview anywhere (the
// editor canvas, slide-browser tiles, the playlist inline preview)
// must resolve against the same value — so a module singleton is
// the correct model, not per-call data threaded through a dozen
// drawCanvas call sites. `null` = "Device local" → browser-local
// fallback. Set via `setSignTimezone` from app boot + Settings save.
let signTimeZone = null;

/// Set the app-wide sign timezone. `tz` is an IANA string (e.g.
/// "Europe/London") or null/"" for "Device local". Called by
/// main.js after loading settings + after a Settings save.
export function setSignTimezone(tz) {
    signTimeZone = tz || null;
}

/// Decompose `now` into calendar fields in `timeZone`. When
/// `timeZone` is null/empty, uses the browser-local Date getters
/// (the documented "Device local" fallback). Returns month0 /
/// weekday0 as 0-based indices so they index the MONTHS_* / DAYS_*
/// arrays directly.
function breakdown(now, timeZone) {
    if (!timeZone) {
        return {
            year: now.getFullYear(),
            month0: now.getMonth(),
            day: now.getDate(),
            hour: now.getHours(),
            minute: now.getMinutes(),
            second: now.getSeconds(),
            weekday0: now.getDay(),
        };
    }
    let parts;
    try {
        parts = new Intl.DateTimeFormat("en-US", {
            timeZone,
            year: "numeric", month: "2-digit", day: "2-digit",
            hour: "2-digit", minute: "2-digit", second: "2-digit",
            hourCycle: "h23",
        }).formatToParts(now);
    } catch {
        // Invalid IANA tz string — fall back to browser-local
        // rather than throwing out of the preview render loop.
        return breakdown(now, null);
    }
    const val = (type) => {
        const p = parts.find((x) => x.type === type);
        return p ? parseInt(p.value, 10) : 0;
    };
    const year = val("year");
    const month1 = val("month");
    const day = val("day");
    let hour = val("hour");
    if (hour === 24) hour = 0; // guard the legacy h24-at-midnight quirk
    return {
        year,
        month0: month1 - 1,
        day,
        hour,
        minute: val("minute"),
        second: val("second"),
        // Weekday computed numerically (Sakamoto) — locale-independent,
        // avoids parsing a localized weekday string back to an index.
        weekday0: dowFromYmd(year, month1, day),
    };
}

// Day-of-week from Y/M/D via Sakamoto's algorithm. `m` is 1-12;
// returns 0=Sunday .. 6=Saturday (matching JS Date.getDay()).
function dowFromYmd(y, m, d) {
    const t = [0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4];
    const yy = m < 3 ? y - 1 : y;
    return (
        (yy + Math.floor(yy / 4) - Math.floor(yy / 100)
            + Math.floor(yy / 400) + t[m - 1] + d) % 7
    );
}

/// Format `now` for an auto_mode layer. `timeZone` defaults to the
/// app-wide `signTimeZone` (set via `setSignTimezone`); tests pass
/// an explicit zone for determinism. Every auto_format — time AND
/// date AND day — resolves in the chosen zone, so a date near
/// midnight shows the sign's calendar day, not the browser's.
export function formatAutoText(mode, format, now, timeZone = signTimeZone) {
    const fmt = format || defaultFormatFor(mode);
    const t = breakdown(now, timeZone);
    if (mode === "time") {
        if (fmt === "time_hms") {
            return `${pad2(t.hour)}:${pad2(t.minute)}:${pad2(t.second)}`;
        }
        return `${pad2(t.hour)}:${pad2(t.minute)}`;
    }
    if (mode === "date") {
        if (fmt === "date_iso") {
            return `${t.year}-${pad2(t.month0 + 1)}-${pad2(t.day)}`;
        }
        if (fmt === "date_medium") {
            return `${MONTHS_SHORT[t.month0]} ${t.day}`;
        }
        return `${MONTHS_LONG[t.month0]} ${t.day}, ${t.year}`;
    }
    if (mode === "day") {
        if (fmt === "day_short") return DAYS_SHORT[t.weekday0];
        return DAYS_LONG[t.weekday0];
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
