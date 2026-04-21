// Canonical IANA timezone list for the UI's <select> dropdowns.
//
// Browsers ship this via `Intl.supportedValuesOf('timeZone')` — Baseline
// Widely Available since 2022 (Chrome 99+, Safari 15.4+, Firefox 93+).
// The captive portal is a phone's browser, so the baseline is comfortably
// met and we don't ship a bundled TZDB.
//
// Tiny fallback for the edge case of a weird browser that can't produce
// the list. "UTC" alone is useless for schedule authoring but at least
// the dropdown isn't empty.

const FALLBACK = [
    "UTC",
    "America/Los_Angeles",
    "America/Denver",
    "America/Chicago",
    "America/New_York",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Asia/Tokyo",
    "Australia/Sydney",
];

// Common U.S. zones surfaced at the top of the dropdown so US operators
// (our primary F&F demo audience) find their zone without scrolling
// through 400+ IANA entries. Order matches the mainland-west-to-east +
// Alaska / Hawaii / territories the list includes.
export const US_COMMON_TIMEZONES = Object.freeze([
    "America/Los_Angeles",
    "America/Denver",
    "America/Phoenix",
    "America/Chicago",
    "America/New_York",
    "America/Anchorage",
    "Pacific/Honolulu",
    "UTC",
]);

export function listTimezones() {
    try {
        const supportedValuesOf = Intl?.supportedValuesOf;
        if (typeof supportedValuesOf === "function") {
            const values = supportedValuesOf.call(Intl, "timeZone");
            if (Array.isArray(values) && values.length > 0) {
                return withUsCommonFirst(values);
            }
        }
    } catch {
        // Fall through to FALLBACK.
    }
    return FALLBACK;
}

// Browsers' Intl.supportedValuesOf('timeZone') deliberately omits "UTC"
// (it's an alias, not a zoneinfo entry). We also front-load the common
// U.S. zones so the dropdown's first click covers 90% of operators.
// De-dupe so the remaining IANA list doesn't contain repeats.
function withUsCommonFirst(values) {
    const seen = new Set();
    const out = [];
    for (const z of US_COMMON_TIMEZONES) {
        if (!seen.has(z)) {
            out.push(z);
            seen.add(z);
        }
    }
    for (const z of values) {
        if (!seen.has(z)) {
            out.push(z);
            seen.add(z);
        }
    }
    return out;
}
