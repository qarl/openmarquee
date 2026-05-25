// Generic LRU cache bounded by total byte volume.
//
// Each entry carries a caller-supplied byte cost (caller knows the
// shape of its values; the cache doesn't introspect). On set(), if
// total bytes exceed `byteBudget`, the cache evicts oldest entries
// (by insertion-or-touch order via Map's iteration semantics) until
// it's back under budget.
//
// Optional `entryByteCap` rejects single entries larger than the cap
// outright — without this, a single huge entry would force eviction
// of EVERY smaller entry without bringing total bytes under budget,
// then sit alone above budget until cleared. The caller's miss path
// re-computes the value; not caching a too-big entry is cheaper than
// re-evicting the entire cache on every hit.
//
// Extracted from wasm-renderer.js in r27 so the byte-arithmetic + LRU
// eviction loop is testable without the .wasm import that pins the
// production module behind vitest.config.js's resolve.alias stub.
// Generic shape — no wasm-renderer-specific assumptions; suitable for
// any future byte-volume-bounded cache (e.g., decoded image bitmaps,
// pre-rasterized font glyph atlases).

export class LruByteCache {
    /**
     * @param {object} opts
     * @param {number} opts.byteBudget — max total bytes across all entries.
     *   Eviction triggers when sum-of-entry-bytes > budget.
     * @param {number} [opts.entryByteCap=Infinity] — refuse to cache any
     *   single entry whose bytes exceed this. Default: no per-entry cap.
     */
    constructor({ byteBudget, entryByteCap } = {}) {
        if (!Number.isFinite(byteBudget) || byteBudget <= 0) {
            throw new Error("LruByteCache: byteBudget must be a positive finite number");
        }
        this._budget = byteBudget;
        this._entryCap = Number.isFinite(entryByteCap) ? entryByteCap : Infinity;
        // Map preserves insertion order; iteration via .keys() yields
        // oldest first. get() does delete-then-reinsert to bump an
        // accessed entry to the "newest" position.
        this._entries = new Map();
        this._bytes = 0;
    }

    /** Current entry count. */
    get size() { return this._entries.size; }

    /** Current total bytes across all entries. */
    get bytes() { return this._bytes; }

    /**
     * Look up by key. Returns the value (whatever was passed to set())
     * or undefined if not present. Touching an entry moves it to the
     * "newest" position so it survives the next eviction pass.
     */
    get(key) {
        const entry = this._entries.get(key);
        if (entry === undefined) return undefined;
        // Touch: delete + reinsert to bump to end of insertion order.
        this._entries.delete(key);
        this._entries.set(key, entry);
        return entry.value;
    }

    /**
     * Store `value` under `key` with the given byte cost. Returns true
     * if cached, false if rejected (over entryByteCap).
     *
     * If `key` already exists, the prior entry's bytes are subtracted
     * before the new entry's are added. Eviction loop runs after add
     * until total bytes <= byteBudget OR only one entry remains
     * (single-entry blowout is a defensive guard — the entryByteCap
     * check above already covers this case, but `size > 1` belts the
     * loop against an infinite eviction).
     */
    set(key, value, bytes) {
        if (!Number.isFinite(bytes) || bytes < 0) {
            throw new Error("LruByteCache: bytes must be a non-negative finite number");
        }
        if (bytes > this._entryCap) {
            // Reject: caching this would force eviction of everything
            // smaller without bringing total under budget. Caller's
            // miss path re-computes.
            return false;
        }
        // If key already exists, subtract old bytes + delete (reinsert
        // below to update insertion-order position too).
        const existing = this._entries.get(key);
        if (existing !== undefined) {
            this._bytes -= existing.bytes;
            this._entries.delete(key);
        }
        this._bytes += bytes;
        this._entries.set(key, { value, bytes });
        // Evict oldest until under budget. `size > 1` guard belts
        // against the single-huge-entry edge case (the entryByteCap
        // check above already prevents it, but defense-in-depth).
        while (this._bytes > this._budget && this._entries.size > 1) {
            const oldestKey = this._entries.keys().next().value;
            const oldEntry = this._entries.get(oldestKey);
            this._bytes -= oldEntry.bytes;
            this._entries.delete(oldestKey);
        }
        return true;
    }

    /** Drop all entries. Counter resets to 0. */
    clear() {
        this._entries.clear();
        this._bytes = 0;
    }
}
