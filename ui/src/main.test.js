import { describe, it, expect } from "vitest";
import { greeting } from "./main.js";

describe("greeting", () => {
    it("includes the name it was given", () => {
        expect(greeting("world")).toContain("world");
    });

    it("names OpenMarquee", () => {
        expect(greeting("anyone")).toContain("OpenMarquee");
    });
});
