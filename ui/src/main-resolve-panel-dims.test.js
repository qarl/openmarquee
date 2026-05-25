// Tests for resolvePanelDims (main.js:98-144). The function is the
// boot()-friendly settings + effectiveDisplayDims accessor: fetches
// settings, computes display dims, coerces brightness into [0, 100]
// with rounding, defaults outputMode + rotation, and falls back to
// a hardcoded {128x96, hdmi, rotation=0, brightness=80} shape if
// anything throws or returns null dims.
//
// Lives in a separate file from main.js's (nonexistent) main.test.js
// because vitest treats `// @vitest-environment jsdom` directives in
// other parts of the codebase carefully -- see
// feedback_vitest_directive_pickup_from_comments. This file
// deliberately runs in the default node environment (the literal
// env-directive token is intentionally absent from every comment in
// this file -- vitest 1.6's parser picks it up from inside `//`
// comments + backtick spans + would trigger jsdom loading, which is
// not needed here since resolvePanelDims touches no DOM).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Stub the api.js imports resolvePanelDims uses. Other api.js exports
// stay live via importOriginal so main.js's top-level imports don't
// blow up at load time.
vi.mock("./api.js", async (importOriginal) => {
    const actual = await importOriginal();
    return {
        ...actual,
        getSettings: vi.fn(),
        effectiveDisplayDims: vi.fn(),
    };
});

// auto-format.js's setSignTimezone runs as a side effect inside
// resolvePanelDims's try block. Stub it to a no-op so the call
// doesn't blow up on a missing timezone or perturb global state.
vi.mock("./auto-format.js", async (importOriginal) => {
    const actual = await importOriginal();
    return {
        ...actual,
        setSignTimezone: vi.fn(),
    };
});

// jsdom is virtiofs-wedged on this dev machine; main.js's other
// imports (mountEditor / mountSettings / etc) touch the DOM inside
// their function bodies but their TOP-LEVEL code is import-safe
// in pure node env. Confirmed empirically: this test file resolves
// + runs without window/document defined.

const SAMPLE_SETTINGS = {
    timezone: "America/New_York",
    output_mode: "hub75",
    display_rotation: 90,
    brightness: 65,
};

beforeEach(() => {
    vi.clearAllMocks();
});

afterEach(() => {
    vi.restoreAllMocks();
});

describe("resolvePanelDims", () => {
    it("returns the resolved shape when getSettings + effectiveDisplayDims both succeed", async () => {
        const { getSettings, effectiveDisplayDims } = await import("./api.js");
        getSettings.mockResolvedValue(SAMPLE_SETTINGS);
        effectiveDisplayDims.mockReturnValue({ width: 64, height: 32 });
        const { resolvePanelDims } = await import("./main.js");
        const result = await resolvePanelDims();
        expect(result).toEqual({
            width: 64,
            height: 32,
            rotation: 90,
            outputMode: "hub75",
            brightness: 65,
        });
    });

    it("clamps + rounds brightness into [0, 100] (50.7 -> 51, -10 -> 0, 150 -> 100)", async () => {
        const { getSettings, effectiveDisplayDims } = await import("./api.js");
        effectiveDisplayDims.mockReturnValue({ width: 64, height: 32 });

        getSettings.mockResolvedValue({ ...SAMPLE_SETTINGS, brightness: 50.7 });
        expect((await resolvePanelDimsImported()).brightness).toBe(51);

        getSettings.mockResolvedValue({ ...SAMPLE_SETTINGS, brightness: -10 });
        expect((await resolvePanelDimsImported()).brightness).toBe(0);

        getSettings.mockResolvedValue({ ...SAMPLE_SETTINGS, brightness: 150 });
        expect((await resolvePanelDimsImported()).brightness).toBe(100);
    });

    it("falls back to brightness=80 when settings.brightness is non-finite", async () => {
        const { getSettings, effectiveDisplayDims } = await import("./api.js");
        effectiveDisplayDims.mockReturnValue({ width: 64, height: 32 });

        getSettings.mockResolvedValue({ ...SAMPLE_SETTINGS, brightness: "abc" });
        expect((await resolvePanelDimsImported()).brightness).toBe(80);

        getSettings.mockResolvedValue({ ...SAMPLE_SETTINGS, brightness: NaN });
        expect((await resolvePanelDimsImported()).brightness).toBe(80);

        getSettings.mockResolvedValue({ ...SAMPLE_SETTINGS, brightness: undefined });
        expect((await resolvePanelDimsImported()).brightness).toBe(80);
    });

    it("defaults outputMode to 'hdmi' when settings.output_mode is missing or empty", async () => {
        const { getSettings, effectiveDisplayDims } = await import("./api.js");
        effectiveDisplayDims.mockReturnValue({ width: 64, height: 32 });

        getSettings.mockResolvedValue({ ...SAMPLE_SETTINGS, output_mode: undefined });
        expect((await resolvePanelDimsImported()).outputMode).toBe("hdmi");

        getSettings.mockResolvedValue({ ...SAMPLE_SETTINGS, output_mode: "" });
        expect((await resolvePanelDimsImported()).outputMode).toBe("hdmi");

        getSettings.mockResolvedValue({ ...SAMPLE_SETTINGS, output_mode: null });
        expect((await resolvePanelDimsImported()).outputMode).toBe("hdmi");
    });

    it("defaults rotation to 0 when settings.display_rotation is missing", async () => {
        const { getSettings, effectiveDisplayDims } = await import("./api.js");
        effectiveDisplayDims.mockReturnValue({ width: 64, height: 32 });

        getSettings.mockResolvedValue({ ...SAMPLE_SETTINGS, display_rotation: undefined });
        expect((await resolvePanelDimsImported()).rotation).toBe(0);

        getSettings.mockResolvedValue({ ...SAMPLE_SETTINGS, display_rotation: 0 });
        expect((await resolvePanelDimsImported()).rotation).toBe(0);
    });

    it("falls back to the FALLBACK_* shape when getSettings throws", async () => {
        const { getSettings } = await import("./api.js");
        getSettings.mockRejectedValue(new Error("network glitch"));
        const result = await resolvePanelDimsImported();
        expect(result).toEqual({
            width: 128,
            height: 96,
            rotation: 0,
            outputMode: "hdmi",
            brightness: 80,
        });
    });

    it("falls back to the FALLBACK_* shape when effectiveDisplayDims returns null", async () => {
        const { getSettings, effectiveDisplayDims } = await import("./api.js");
        getSettings.mockResolvedValue(SAMPLE_SETTINGS);
        effectiveDisplayDims.mockReturnValue(null);
        const result = await resolvePanelDimsImported();
        expect(result).toEqual({
            width: 128,
            height: 96,
            rotation: 0,
            outputMode: "hdmi",
            brightness: 80,
        });
    });
});

// Helper: re-import main.js to pick up the current mock state.
// vi.mock hoists so the stubs are in place, but the per-test mock
// resolves/rejects need to be set before the resolvePanelDims call.
async function resolvePanelDimsImported() {
    const { resolvePanelDims } = await import("./main.js");
    return resolvePanelDims();
}
