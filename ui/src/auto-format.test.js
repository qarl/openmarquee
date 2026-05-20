// Pure-logic tests for formatAutoText (UI-side mirror of
// openmarquee.auto_render.render_auto_text). Per qarl-canon
// "tests alongside live fire" + QA Batch 2 dispatch: parity
// against backend test_auto_render.py is the load-bearing
// guarantee since editor preview + device renderer must agree.
//
// All tests construct a JS Date via the (year, monthIndex, day,
// hour, minute, second) constructor which produces a LOCAL-TZ
// Date. formatAutoText reads now.getHours() / .getMonth() etc.,
// which are also local-TZ. So the inputs and outputs are in the
// machine's local zone -- matching the documented "preview uses
// browser TZ" trade-off in the module header.

import { describe, expect, it } from "vitest";
import { formatAutoText, setSignTimezone } from "./auto-format.js";

describe("formatAutoText", () => {
    describe("mode=time", () => {
        it("default time_hm at 14:30:45 -> '14:30'", () => {
            // Parity with test_auto_render.py::test_time_hm_default
            const now = new Date(2026, 3, 21, 14, 30, 45);
            expect(formatAutoText("time", null, now)).toBe("14:30");
        });

        it("time_hms at 14:30:45 -> '14:30:45'", () => {
            // Parity with test_auto_render.py::test_time_hms
            const now = new Date(2026, 3, 21, 14, 30, 45);
            expect(formatAutoText("time", "time_hms", now)).toBe("14:30:45");
        });

        it("zero-pads single-digit hour and minute", () => {
            const now = new Date(2026, 3, 21, 4, 5, 6);
            expect(formatAutoText("time", "time_hm", now)).toBe("04:05");
            expect(formatAutoText("time", "time_hms", now)).toBe("04:05:06");
        });

        it("midnight is 00:00 (24-hour, NOT 12:00)", () => {
            // Edge case: midnight in 24h is 00:00. Backend uses %H so
            // does the same. Parity.
            const now = new Date(2026, 3, 21, 0, 0, 0);
            expect(formatAutoText("time", "time_hm", now)).toBe("00:00");
            expect(formatAutoText("time", "time_hms", now)).toBe("00:00:00");
        });

        it("noon is 12:00 (24-hour)", () => {
            const now = new Date(2026, 3, 21, 12, 0, 0);
            expect(formatAutoText("time", "time_hm", now)).toBe("12:00");
        });

        it("23:59:59 (just-before-midnight)", () => {
            const now = new Date(2026, 3, 21, 23, 59, 59);
            expect(formatAutoText("time", "time_hms", now)).toBe("23:59:59");
        });
    });

    describe("mode=date", () => {
        it("default date_iso (the JS-side default) -> 'YYYY-MM-DD'", () => {
            // JS module's defaultFormatFor returns "date_iso" for date.
            // Parity-NOTE: backend's DEFAULT_FORMATS map says "date_iso"
            // too (auto_render.py:46). Matched.
            const now = new Date(2026, 3, 21);
            expect(formatAutoText("date", null, now)).toBe("2026-04-21");
        });

        it("date_iso explicitly -> '2026-04-21'", () => {
            // Parity with test_auto_render.py::test_date_iso
            const now = new Date(2026, 3, 21);
            expect(formatAutoText("date", "date_iso", now)).toBe("2026-04-21");
        });

        it("date_medium -> 'Apr 7' (no zero-pad on day)", () => {
            // Parity with test_auto_render.py::test_date_medium
            const now = new Date(2026, 3, 7);
            expect(formatAutoText("date", "date_medium", now)).toBe("Apr 7");
        });

        it("date_long -> 'April 7, 2026' (no leading zero on day)", () => {
            // Parity with test_auto_render.py::test_date_long_default_
            // drops_leading_zero
            const now = new Date(2026, 3, 7);
            expect(formatAutoText("date", "date_long", now)).toBe("April 7, 2026");
        });

        it("date_iso zero-pads single-digit month and day", () => {
            const now = new Date(2026, 0, 5);  // Jan 5
            expect(formatAutoText("date", "date_iso", now)).toBe("2026-01-05");
        });

        it("leap day: 2024-02-29", () => {
            // Edge case: leap-year day formatting works for all 3
            // formats.
            const now = new Date(2024, 1, 29);
            expect(formatAutoText("date", "date_iso", now)).toBe("2024-02-29");
            expect(formatAutoText("date", "date_medium", now)).toBe("Feb 29");
            expect(formatAutoText("date", "date_long", now)).toBe("February 29, 2024");
        });
    });

    describe("mode=day", () => {
        it("day_long for Tuesday (2026-04-21)", () => {
            // Parity with test_auto_render.py::test_day_long
            const now = new Date(2026, 3, 21);
            expect(formatAutoText("day", "day_long", now)).toBe("Tuesday");
        });

        it("day_short for Tuesday (2026-04-21)", () => {
            // Parity with test_auto_render.py::test_day_short
            const now = new Date(2026, 3, 21);
            expect(formatAutoText("day", "day_short", now)).toBe("Tue");
        });

        it("default day_long for null format", () => {
            const now = new Date(2026, 3, 21);
            expect(formatAutoText("day", null, now)).toBe("Tuesday");
        });

        it("day_short cycles through all 7 days", () => {
            // 2026-04-19 = Sunday, then Mon, Tue, ..., Sat
            const expected = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
            for (let i = 0; i < 7; i++) {
                const now = new Date(2026, 3, 19 + i);
                expect(formatAutoText("day", "day_short", now)).toBe(expected[i]);
            }
        });

        it("day_long cycles through all 7 days", () => {
            const expected = [
                "Sunday", "Monday", "Tuesday", "Wednesday",
                "Thursday", "Friday", "Saturday",
            ];
            for (let i = 0; i < 7; i++) {
                const now = new Date(2026, 3, 19 + i);
                expect(formatAutoText("day", "day_long", now)).toBe(expected[i]);
            }
        });
    });

    describe("unknown / fallback shape", () => {
        it("unknown mode returns empty string", () => {
            // The function's final return "" matches the fall-through
            // for any mode not in {time, date, day}. Defensive shape.
            const now = new Date(2026, 3, 21);
            expect(formatAutoText("garbage", null, now)).toBe("");
            expect(formatAutoText("", null, now)).toBe("");
        });

        it("unknown format string falls to mode default", () => {
            // The match-arms inside each mode block fall through to
            // the default (time_hm / date_long / day_long). So an
            // unknown format string for a known mode produces the
            // default-format output.
            const now = new Date(2026, 3, 21, 14, 30, 45);
            // time: known formats are time_hm (default) + time_hms.
            // anything else falls to time_hm.
            expect(formatAutoText("time", "garbage_format", now)).toBe("14:30");
            // date: known formats are date_iso, date_medium, date_long.
            // anything else falls to date_long.
            const dateNow = new Date(2026, 3, 7);
            expect(formatAutoText("date", "garbage_format", dateNow))
                .toBe("April 7, 2026");
            // day: only day_short branches off; everything else =
            // day_long.
            expect(formatAutoText("day", "garbage_format", now)).toBe("Tuesday");
        });
    });

    // Parity follow-up (2026-05-20): the auto_mode preview resolves
    // in the SIGN's configured timezone, not the operator's browser
    // zone. These tests pin `now` to an explicit UTC INSTANT (via
    // Date.UTC) so the assertions are deterministic regardless of
    // the machine running the suite — the `timeZone` arg does the
    // conversion.
    describe("explicit timezone (sign-tz preview parity)", () => {
        it("resolves time in the given IANA zone", () => {
            // 2026-04-21 23:30:15 UTC. Tokyo is UTC+9, no DST.
            const now = new Date(Date.UTC(2026, 3, 21, 23, 30, 15));
            expect(formatAutoText("time", "time_hms", now, "Asia/Tokyo"))
                .toBe("08:30:15");
            expect(formatAutoText("time", "time_hms", now, "UTC"))
                .toBe("23:30:15");
        });

        it("date rolls over at the sign zone's midnight, not UTC's", () => {
            // 2026-04-21 23:30 UTC: in Tokyo (+9) it is already the
            // 22nd; in Los_Angeles (-7 in April, PDT) it is still
            // the 21st. The previewed calendar day must follow the
            // sign zone — same bug class as a wrong clock.
            const now = new Date(Date.UTC(2026, 3, 21, 23, 30, 0));
            expect(formatAutoText("date", "date_iso", now, "Asia/Tokyo"))
                .toBe("2026-04-22");
            expect(formatAutoText("date", "date_iso", now, "UTC"))
                .toBe("2026-04-21");
            expect(formatAutoText("date", "date_iso", now, "America/Los_Angeles"))
                .toBe("2026-04-21");
        });

        it("day-of-week follows the sign zone across a date boundary", () => {
            // 2026-04-21 is a Tuesday (UTC). At 23:30 UTC, Tokyo has
            // rolled to Wednesday the 22nd.
            const now = new Date(Date.UTC(2026, 3, 21, 23, 30, 0));
            expect(formatAutoText("day", "day_long", now, "UTC")).toBe("Tuesday");
            expect(formatAutoText("day", "day_long", now, "Asia/Tokyo"))
                .toBe("Wednesday");
        });

        it("honors DST — London is BST (+1) in July, GMT (+0) in January", () => {
            const july = new Date(Date.UTC(2026, 6, 15, 12, 0, 0));
            expect(formatAutoText("time", "time_hm", july, "Europe/London"))
                .toBe("13:00"); // BST = UTC+1
            const january = new Date(Date.UTC(2026, 0, 15, 12, 0, 0));
            expect(formatAutoText("time", "time_hm", january, "Europe/London"))
                .toBe("12:00"); // GMT = UTC+0
        });

        it("null / omitted timezone falls back to browser-local", () => {
            // "Device local" — the browser can't know the sign's
            // zone. Explicit-null and the 3-arg call must agree, and
            // both equal the browser-local getters' result.
            const now = new Date(2026, 3, 21, 14, 30, 45);
            const threeArg = formatAutoText("time", "time_hms", now);
            const explicitNull = formatAutoText("time", "time_hms", now, null);
            expect(explicitNull).toBe(threeArg);
            expect(explicitNull).toBe(
                `${String(now.getHours()).padStart(2, "0")}:`
                + `${String(now.getMinutes()).padStart(2, "0")}:`
                + `${String(now.getSeconds()).padStart(2, "0")}`,
            );
        });

        it("an invalid timezone string falls back to browser-local, no throw", () => {
            const now = new Date(2026, 3, 21, 14, 30, 45);
            // Must not throw out of the preview render loop.
            expect(
                formatAutoText("time", "time_hms", now, "Not/A_Zone"),
            ).toBe(formatAutoText("time", "time_hms", now, null));
        });
    });

    describe("setSignTimezone (app-wide default)", () => {
        it("setSignTimezone changes the default zone used by 3-arg calls", () => {
            const now = new Date(Date.UTC(2026, 3, 21, 23, 30, 15));
            setSignTimezone("Asia/Tokyo");
            // 3-arg call now uses the module default = Asia/Tokyo.
            expect(formatAutoText("time", "time_hms", now)).toBe("08:30:15");
            setSignTimezone("UTC");
            expect(formatAutoText("time", "time_hms", now)).toBe("23:30:15");
            // Reset so other test files / cases see the default.
            setSignTimezone(null);
            // null default -> browser-local fallback (no throw).
            expect(typeof formatAutoText("time", "time_hms", now)).toBe("string");
        });
    });
});
