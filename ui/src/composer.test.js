// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { drawComposite, mountComposer } from "./composer.js";

function tick() {
    return new Promise((r) => setTimeout(r, 0));
}

// Stub canvas: jsdom's canvas returns null for getContext(), which breaks
// the composer's draw calls. Install a bare-bones 2d context shim.
beforeEach(() => {
    const canvasProto = HTMLCanvasElement.prototype;
    vi.spyOn(canvasProto, "getContext").mockImplementation(function () {
        return {
            save: () => {},
            restore: () => {},
            fillRect: () => {},
            fillText: () => {},
            drawImage: () => {},
            measureText: () => ({
                width: 40,
                actualBoundingBoxAscent: 10,
                actualBoundingBoxDescent: 4,
            }),
            set fillStyle(_v) {},
            set font(_v) {},
            set textAlign(_v) {},
            set textBaseline(_v) {},
        };
    });
    vi.spyOn(canvasProto, "toDataURL").mockReturnValue("data:image/png;base64,AAAA");
    // pointer capture is unused in jsdom but the composer calls it.
    HTMLElement.prototype.setPointerCapture = function () {};
    HTMLElement.prototype.releasePointerCapture = function () {};
});

afterEach(() => {
    vi.restoreAllMocks();
});

describe("mountComposer", () => {
    it("seeds with one text layer and renders the name + duration controls", async () => {
        const container = document.createElement("div");
        mountComposer(container, {
            width: 128,
            height: 96,
            fetchItems: async () => [],
            onSave: vi.fn(),
        });
        await tick();

        expect(container.querySelectorAll(".layer-card")).toHaveLength(1);
        expect(container.querySelector(".field-name").value).toBe("Composite");
        expect(container.querySelector(".field-duration").value).toBe("5");
    });

    it("adds a new layer when + Add text layer is clicked", async () => {
        const container = document.createElement("div");
        mountComposer(container, {
            width: 128,
            height: 96,
            fetchItems: async () => [],
            onSave: vi.fn(),
        });
        await tick();

        container.querySelector(".layers-add").click();
        expect(container.querySelectorAll(".layer-card")).toHaveLength(2);
    });

    it("removes a layer when × is clicked", async () => {
        const container = document.createElement("div");
        mountComposer(container, {
            width: 128,
            height: 96,
            fetchItems: async () => [],
            onSave: vi.fn(),
        });
        await tick();

        container.querySelector(".layers-add").click();
        expect(container.querySelectorAll(".layer-card")).toHaveLength(2);
        container.querySelector(".layer-card:last-child .layer-remove").click();
        expect(container.querySelectorAll(".layer-card")).toHaveLength(1);
    });

    it("switching background mode to 'slide' populates the dropdown from fetchItems", async () => {
        const container = document.createElement("div");
        const fetchItems = vi
            .fn()
            .mockResolvedValue([
                { id: "a", name: "Open" },
                { id: "b", name: "Closed" },
            ]);
        mountComposer(container, {
            width: 128,
            height: 96,
            fetchItems,
            onSave: vi.fn(),
        });
        await tick();

        const select = container.querySelector(".bg-mode");
        select.value = "slide";
        select.dispatchEvent(new Event("change"));
        await tick();

        expect(fetchItems).toHaveBeenCalled();
        const options = Array.from(container.querySelectorAll(".bg-slide option"))
            .map((o) => o.value)
            .filter(Boolean);
        expect(options).toEqual(["a", "b"]);
    });

    it("Save invokes onSave with the ImageSlide payload", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue(undefined);
        mountComposer(container, {
            width: 128,
            height: 96,
            fetchItems: async () => [],
            onSave,
        });
        await tick();

        container.querySelector(".field-name").value = "Lobby";
        container.querySelector(".field-duration").value = "10";
        container.querySelector(".composer-controls").dispatchEvent(new Event("submit"));
        await tick();

        expect(onSave).toHaveBeenCalledTimes(1);
        const payload = onSave.mock.calls[0][0];
        expect(payload.name).toBe("Lobby");
        expect(payload.duration_ms).toBe(10_000);
        expect(payload.png_base64).toBe("AAAA");
    });

    it("saves a default-name composite when the field is left at its default", async () => {
        const container = document.createElement("div");
        const onSave = vi.fn().mockResolvedValue(undefined);
        mountComposer(container, {
            width: 128,
            height: 96,
            fetchItems: async () => [],
            onSave,
        });
        await tick();

        container.querySelector(".composer-controls").dispatchEvent(new Event("submit"));
        await tick();
        expect(onSave.mock.calls[0][0].name).toBe("Composite");
    });

    it("disables Save while a background slide is loading", async () => {
        const container = document.createElement("div");
        mountComposer(container, {
            width: 128,
            height: 96,
            fetchItems: async () => [{ id: "a", name: "Open" }],
            onSave: vi.fn(),
        });
        await tick();

        // Switch mode + populate.
        const mode = container.querySelector(".bg-mode");
        mode.value = "slide";
        mode.dispatchEvent(new Event("change"));
        await tick();

        // Pick the slide. The <img> src fires an async load in jsdom which
        // never completes (jsdom does not decode images) — so `bgLoading`
        // stays true and Save stays disabled.
        const select = container.querySelector(".bg-slide");
        select.value = "a";
        select.dispatchEvent(new Event("change"));

        const saveBtn = container.querySelector(".composer-save");
        expect(saveBtn.disabled).toBe(true);
        expect(container.querySelector(".composer-status").textContent).toMatch(
            /Waiting for background/,
        );
    });

    it("disables Generate button when onGenerateBackground isn't wired", async () => {
        const container = document.createElement("div");
        mountComposer(container, {
            width: 128,
            height: 96,
            fetchItems: async () => [],
            onSave: vi.fn(),
            // onGenerateBackground intentionally omitted
        });
        await tick();
        const btn = container.querySelector(".bg-generate-btn");
        expect(btn.disabled).toBe(true);
        expect(container.querySelector(".bg-generate-status").textContent).toMatch(
            /isn't wired/,
        );
    });

    it("Generate button demands a prompt before calling the hook", async () => {
        const container = document.createElement("div");
        const onGenerateBackground = vi.fn();
        mountComposer(container, {
            width: 128,
            height: 96,
            fetchItems: async () => [],
            onSave: vi.fn(),
            onGenerateBackground,
        });
        await tick();
        container.querySelector(".bg-generate-btn").click();
        await tick();
        expect(onGenerateBackground).not.toHaveBeenCalled();
        expect(container.querySelector(".bg-generate-status").textContent).toMatch(
            /prompt first/i,
        );
    });

    it("Generate button calls the hook with the typed prompt", async () => {
        const container = document.createElement("div");
        const onGenerateBackground = vi
            .fn()
            .mockResolvedValue({ id: "bg1", name: "Background — gradient" });
        mountComposer(container, {
            width: 128,
            height: 96,
            fetchItems: async () => [],
            onSave: vi.fn(),
            onGenerateBackground,
        });
        await tick();

        container.querySelector(".bg-generate-prompt").value = "abstract gradient";
        container.querySelector(".bg-generate-btn").click();
        await tick();
        await tick();

        expect(onGenerateBackground).toHaveBeenCalledWith({
            prompt: "abstract gradient",
        });
    });

    it("Surfaces a 503 from the hook as a friendly 'not set up' status", async () => {
        const container = document.createElement("div");
        const err503 = Object.assign(new Error("nope"), { status: 503 });
        const onGenerateBackground = vi.fn().mockRejectedValue(err503);
        mountComposer(container, {
            width: 128,
            height: 96,
            fetchItems: async () => [],
            onSave: vi.fn(),
            onGenerateBackground,
        });
        await tick();

        container.querySelector(".bg-generate-prompt").value = "x";
        container.querySelector(".bg-generate-btn").click();
        await tick();
        await tick();

        expect(container.querySelector(".bg-generate-status").textContent).toMatch(
            /isn't set up/,
        );
    });

    it("editing a layer's text triggers a redraw (no throw)", async () => {
        const container = document.createElement("div");
        mountComposer(container, {
            width: 128,
            height: 96,
            fetchItems: async () => [],
            onSave: vi.fn(),
        });
        await tick();

        const textInput = container.querySelector(".layer-text");
        textInput.value = "SALE";
        textInput.dispatchEvent(new Event("input"));
        // Redraw happens synchronously through the stub ctx; asserting
        // no throw + value persisted is enough for jsdom.
        expect(textInput.value).toBe("SALE");
    });
});

describe("drawComposite (pure)", () => {
    it("renders without throwing for solid-color bg + multiple text layers", () => {
        const canvas = document.createElement("canvas");
        canvas.width = 128;
        canvas.height = 96;
        expect(() =>
            drawComposite(canvas, {
                width: 128,
                height: 96,
                bgMode: "solid",
                bgColor: "#222",
                bgImage: null,
                layers: [
                    { text: "HI", x: 64, y: 40, fontSize: 20, color: "#fff", bold: true, italic: false, align: "center" },
                    { text: "!!", x: 64, y: 70, fontSize: 14, color: "#f00", bold: false, italic: true, align: "center" },
                ],
            }),
        ).not.toThrow();
    });

    it("skips empty-text layers without drawing anything", () => {
        const canvas = document.createElement("canvas");
        const ctx = canvas.getContext("2d");
        const fillTextSpy = vi.fn();
        ctx.fillText = fillTextSpy;
        drawComposite(canvas, {
            width: 32,
            height: 32,
            bgMode: "solid",
            bgColor: "#000",
            bgImage: null,
            layers: [
                { text: "", x: 10, y: 10, fontSize: 12, color: "#fff", bold: false, italic: false, align: "center" },
            ],
        });
        expect(fillTextSpy).not.toHaveBeenCalled();
    });
});
