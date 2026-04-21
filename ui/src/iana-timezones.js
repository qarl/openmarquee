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

export function listTimezones() {
    try {
        const supportedValuesOf = Intl?.supportedValuesOf;
        if (typeof supportedValuesOf === "function") {
            const values = supportedValuesOf.call(Intl, "timeZone");
            if (Array.isArray(values) && values.length > 0) {
                return withUtcFirst(values);
            }
        }
    } catch {
        // Fall through to FALLBACK.
    }
    return FALLBACK;
}

// Browsers' Intl.supportedValuesOf('timeZone') deliberately omits "UTC"
// (it's an alias, not a zoneinfo entry). For schedulers who don't care
// about DST, UTC is still the most useful single choice, so we prepend it
// to the list.
function withUtcFirst(values) {
    if (values.includes("UTC")) return values;
    return ["UTC", ...values];
}
