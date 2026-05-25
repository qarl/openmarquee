// Tests for the byte-bounded LRU cache extracted in r27.
//
// Extracted as a separable testable unit because the original
// in-place cache lived in wasm-renderer.js, which vitest.config.js's
// resolve.alias redirects to a stub for jsdom (no .wasm loader). This
// module has no wasm dependency → testable directly. The byte-
// arithmetic + LRU eviction loop is exactly the kind of thing that
// subtly regresses (forget to subtract on evict, off-by-one in the
// while-loop, clear() not resetting), so a real unit test was worth
// the extraction.

import { describe, expect, it } from "vitest";
import { LruByteCache } from "./lru-byte-cache.js";

describe("LruByteCache — construction", () => {
    it("throws on a non-positive byteBudget", () => {
        expect(() => new LruByteCache({ byteBudget: 0 })).toThrow(/positive finite/);
        expect(() => new LruByteCache({ byteBudget: -1 })).toThrow(/positive finite/);
        expect(() => new LruByteCache({ byteBudget: NaN })).toThrow(/positive finite/);
        expect(() => new LruByteCache({ byteBudget: Infinity })).toThrow(/positive finite/);
    });

    it("starts empty with bytes=0 and size=0", () => {
        const c = new LruByteCache({ byteBudget: 1024 });
        expect(c.size).toBe(0);
        expect(c.bytes).toBe(0);
    });

    it("entryByteCap defaults to Infinity when omitted", () => {
        const c = new LruByteCache({ byteBudget: 1024 });
        // A 512-byte entry well over a non-existent cap — should be cached.
        expect(c.set("a", "value", 512)).toBe(true);
        expect(c.size).toBe(1);
    });
});

describe("LruByteCache — basic operations", () => {
    it("set + get round-trips the value", () => {
        const c = new LruByteCache({ byteBudget: 1024 });
        c.set("a", { foo: 42 }, 100);
        expect(c.get("a")).toEqual({ foo: 42 });
    });

    it("get returns undefined for absent keys", () => {
        const c = new LruByteCache({ byteBudget: 1024 });
        expect(c.get("missing")).toBeUndefined();
    });

    it("bytes counter tracks cumulative size", () => {
        const c = new LruByteCache({ byteBudget: 1024 });
        c.set("a", "value-a", 100);
        expect(c.bytes).toBe(100);
        c.set("b", "value-b", 250);
        expect(c.bytes).toBe(350);
        expect(c.size).toBe(2);
    });

    it("set throws on negative or non-finite bytes", () => {
        const c = new LruByteCache({ byteBudget: 1024 });
        expect(() => c.set("a", "v", -1)).toThrow(/non-negative finite/);
        expect(() => c.set("a", "v", NaN)).toThrow(/non-negative finite/);
        expect(() => c.set("a", "v", Infinity)).toThrow(/non-negative finite/);
    });
});

describe("LruByteCache — eviction", () => {
    it("entries totaling under budget are all retained", () => {
        const c = new LruByteCache({ byteBudget: 1000 });
        c.set("a", "A", 100);
        c.set("b", "B", 200);
        c.set("c", "C", 300);
        expect(c.size).toBe(3);
        expect(c.bytes).toBe(600);
        expect(c.get("a")).toBe("A");
        expect(c.get("b")).toBe("B");
        expect(c.get("c")).toBe("C");
    });

    it("entries totaling over budget evict oldest until under", () => {
        const c = new LruByteCache({ byteBudget: 500 });
        c.set("a", "A", 200);
        c.set("b", "B", 200);
        // Now at 400 bytes / 500 budget. Adding c (200) pushes to 600,
        // triggers eviction of a (oldest). Final: b + c = 400 bytes.
        c.set("c", "C", 200);
        expect(c.bytes).toBe(400);
        expect(c.size).toBe(2);
        expect(c.get("a")).toBeUndefined();   // evicted
        expect(c.get("b")).toBe("B");
        expect(c.get("c")).toBe("C");
    });

    it("multi-evict in one set when one new entry displaces several oldest", () => {
        const c = new LruByteCache({ byteBudget: 500 });
        c.set("a", "A", 100);
        c.set("b", "B", 100);
        c.set("c", "C", 100);
        c.set("d", "D", 100);
        c.set("e", "E", 100);
        // Now 500 bytes / 500 budget exactly — no eviction yet.
        expect(c.size).toBe(5);
        // Add a 300-byte entry. Total would be 800; need to evict
        // 300 bytes of oldest → a, b, c evicted (each 100). Final:
        // d, e, f = 100 + 100 + 300 = 500.
        c.set("f", "F", 300);
        expect(c.bytes).toBe(500);
        expect(c.size).toBe(3);
        expect(c.get("a")).toBeUndefined();
        expect(c.get("b")).toBeUndefined();
        expect(c.get("c")).toBeUndefined();
        expect(c.get("d")).toBe("D");
        expect(c.get("e")).toBe("E");
        expect(c.get("f")).toBe("F");
    });

    it("touch-on-get moves entry to newest position; prevents its eviction", () => {
        const c = new LruByteCache({ byteBudget: 300 });
        c.set("a", "A", 100);
        c.set("b", "B", 100);
        c.set("c", "C", 100);
        // All three at budget. Touch "a" — moves to newest.
        // Insertion order now: b, c, a.
        c.get("a");
        // Add "d" — needs to evict 100 bytes. Oldest is b, NOT a.
        c.set("d", "D", 100);
        expect(c.size).toBe(3);
        expect(c.get("a")).toBe("A");  // survived
        expect(c.get("b")).toBeUndefined();  // evicted
        expect(c.get("c")).toBe("C");
        expect(c.get("d")).toBe("D");
    });
});

describe("LruByteCache — set on existing key", () => {
    it("re-set on existing key subtracts old bytes + adds new", () => {
        const c = new LruByteCache({ byteBudget: 1000 });
        c.set("a", "A-small", 100);
        expect(c.bytes).toBe(100);
        // Re-set with larger value.
        c.set("a", "A-large", 500);
        expect(c.bytes).toBe(500);   // 100 was subtracted, 500 added
        expect(c.size).toBe(1);
        expect(c.get("a")).toBe("A-large");
    });

    it("re-set on existing key bumps it to newest (LRU-wise)", () => {
        const c = new LruByteCache({ byteBudget: 300 });
        c.set("a", "A", 100);
        c.set("b", "B", 100);
        c.set("c", "C", 100);
        // Re-set "a" — should move to newest.
        c.set("a", "A-updated", 100);
        // Now adding "d" should evict "b" (now oldest), not "a".
        c.set("d", "D", 100);
        expect(c.get("a")).toBe("A-updated");  // survived
        expect(c.get("b")).toBeUndefined();    // evicted
        expect(c.get("c")).toBe("C");
        expect(c.get("d")).toBe("D");
    });
});

describe("LruByteCache — per-entry cap", () => {
    it("rejects entries over entryByteCap; bytes counter unchanged", () => {
        const c = new LruByteCache({ byteBudget: 1000, entryByteCap: 200 });
        c.set("small", "S", 100);
        expect(c.bytes).toBe(100);
        // Big entry over cap — rejected.
        const accepted = c.set("big", "B", 500);
        expect(accepted).toBe(false);
        expect(c.bytes).toBe(100);   // unchanged
        expect(c.size).toBe(1);
        expect(c.get("big")).toBeUndefined();
    });

    it("accepts entries exactly at the cap (cap is inclusive)", () => {
        const c = new LruByteCache({ byteBudget: 1000, entryByteCap: 200 });
        expect(c.set("at-cap", "X", 200)).toBe(true);
        expect(c.get("at-cap")).toBe("X");
    });
});

describe("LruByteCache — clear", () => {
    it("clear() removes all entries and resets bytes to 0", () => {
        const c = new LruByteCache({ byteBudget: 1000 });
        c.set("a", "A", 100);
        c.set("b", "B", 200);
        c.set("c", "C", 300);
        expect(c.size).toBe(3);
        expect(c.bytes).toBe(600);

        c.clear();
        expect(c.size).toBe(0);
        expect(c.bytes).toBe(0);
        expect(c.get("a")).toBeUndefined();
        expect(c.get("b")).toBeUndefined();
        expect(c.get("c")).toBeUndefined();
    });

    it("operations after clear() work correctly (counter not poisoned)", () => {
        const c = new LruByteCache({ byteBudget: 1000 });
        c.set("a", "A", 500);
        c.clear();
        c.set("b", "B", 300);
        expect(c.bytes).toBe(300);   // not 800
        expect(c.size).toBe(1);
    });
});
