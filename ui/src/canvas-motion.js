// JS port of compose_motion_frame for canvas-based previews (editor's
// main slide preview, eventually the playlist inline-preview). Mirrors
// the math in backend/openmarquee/motion.py so the editor preview
// shows operators what the device renderer will produce — visually
// approximate, not pixel-identical (per docs/text-layer-motion-spec.md
// Q3 lock).
//
// Six effects + static. Phase comes from a shared monotonic clock
// (the rAF loop's `elapsed_s`) plus the layer's `motion_phase`
// offset, so two layers in the same slide stay coherent.
//
// Parity Bug 3 (2026-05-19): displacement effects (shake, breathe/
// scale, bounce) are NOT box-clipped — the Rust device renderer
// never clips text to the layer box (only to the screen viewport),
// so displaced text spills past the box on glass; the editor
// preview now matches. The box clip survives for the TICKER effect
// only, where it is a rendering MECHANISM (not a containment
// policy): ticker draws the text twice and the clip creates the
// scroll-wrap illusion. blink/pulse don't displace text, so a clip
// would be a no-op for them — they run unclipped too.

const _DEFAULT_BOX = { x: 0.1, y: 0.1, w: 0.8, h: 0.8 };

// Cycle frequency in Hz for each effect. Mirrors motion.py's
// _effect_freq / _ticker_freq / _blink_freq.
export function effectFreq(motion, intensity) {
    const k = Number.isFinite(intensity) ? intensity : 50;
    if (motion === "ticker") {
        const cycleS = Math.max(0.5, 6.0 - 0.05 * k);
        return 1.0 / cycleS;
    }
    if (motion === "shake") return 10.0;
    if (motion === "blink") {
        return k <= 50 ? 0.5 + 0.01 * k : 1.0 + 0.06 * (k - 50);
    }
    // breathe / pulse / bounce — 1 Hz, intensity modulates amplitude.
    return 1.0;
}

// Cycle position in [0, 1) at `elapsed_s` on the shared global clock,
// shifted by the layer's motion_phase. Same shape as motion.py's
// compute_phase.
export function computePhase(elapsed_s, freq, motion_phase) {
    const v = elapsed_s * freq + (motion_phase || 0);
    return v - Math.floor(v);
}

// Deterministic 32-bit hash → uniform [0, 1). Shake's seed input.
// Matches motion.py's deterministic-RNG promise: same layer + same
// motion_phase + same step → same numeric draw across reloads.
//
// qarl 2026-07-16 (per-LETTER jitter): `glyphIndex` joins the seed so
// each letter draws independently. Structural identity (layer + index),
// mirroring the Rust side's `shake_glyph_offset_norm` seeding — the two
// engines were never sample-identical (FNV/Box-Muller here vs
// splitmix64 there; "visually approximate" per the spec Q3 lock), so
// parity is on SHAPE: same amp curve, same 10 Hz quantization, same
// per-letter independence.
function hashedUniform(layerKey, motionPhase, step, glyphIndex) {
    const s = `${layerKey}:${(motionPhase || 0).toFixed(6)}:${glyphIndex}:${step}`;
    // FNV-1a — small, stable, no library dep.
    let h = 2166136261;
    for (let i = 0; i < s.length; i++) {
        h ^= s.charCodeAt(i);
        h = Math.imul(h, 16777619);
    }
    return ((h >>> 0) % 100000) / 100000;
}

// Box-Muller: two uniforms → one Gaussian draw. Matches motion.py's
// numpy default_rng .normal() shape (mean 0, std 1) for shake offsets.
function gaussian(layerKey, motionPhase, step, glyphIndex) {
    const u1 = Math.max(1e-9, hashedUniform(layerKey, motionPhase, step * 2, glyphIndex));
    const u2 = hashedUniform(layerKey, motionPhase, step * 2 + 1, glyphIndex);
    return Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2);
}

// Round 21: shake's step = floor(phase * 10) quantizes to 10 distinct
// values per cycle. At 10 Hz shake / 60 Hz rAF, each step lasts ~6
// frames — 5 of every 6 frames re-derived the same 4 hashedUniform
// strings + FNV walks. Memoize the 10-entry (dxG, dyG) gaussian table
// per (layerKey, motionPhase) tuple so per-frame work is a single
// Map.get + array index. ampPx multiplication stays at use-site
// (intensity / bh can change between renders without invalidating
// the cache).
//
// Bounded LRU. qarl 2026-07-16 (per-LETTER jitter): the key gained a
// glyphIndex, so a slide now builds N layers × M phases × G glyphs
// tables instead of N×M. The old cap of 32 would thrash on a single
// 16-letter line; sized to 512 so the FYS reel's heaviest multi-shake
// "Panic" slide (~6 layers × ~20 letters = ~120) and the 3-layer
// UNCAGE / YOUR / SIGN!! slide (~16 letters ≈ 48) both fit with room
// to spare. Each table is 10 × {dxG, dyG} — 512 entries is ~10k
// floats, trivial. Oldest insertion-order key evicts on overflow —
// Map iteration is insertion order, so .keys().next().value gives
// the oldest.
const _shakeTables = new Map();
const _SHAKE_TABLE_MAX = 512;

function getShakeTable(layerKey, motionPhase, glyphIndex) {
    const tableKey = `${layerKey}:${motionPhase || 0}:${glyphIndex}`;
    let table = _shakeTables.get(tableKey);
    if (table) return table;
    if (_shakeTables.size >= _SHAKE_TABLE_MAX) {
        // LRU-ish: drop the oldest insertion. Map preserves insertion
        // order; first key returned by .keys() is the oldest.
        const oldest = _shakeTables.keys().next().value;
        _shakeTables.delete(oldest);
    }
    table = new Array(10);
    for (let i = 0; i < 10; i++) {
        table[i] = {
            dxG: gaussian(layerKey, motionPhase, i, glyphIndex),
            // The +100000 offset on the dy seed matches the pre-memo
            // call shape in the shake branch — keeps dx and dy
            // independent draws (without the offset, both would seed
            // off step ∈ [0, 9] and dy would equal dx).
            dyG: gaussian(layerKey, motionPhase, i + 100000, glyphIndex),
        };
    }
    _shakeTables.set(tableKey, table);
    return table;
}

// Test seam: per-glyph re-keying multiplies table count, so the LRU
// cap matters more than it did. Exposed so canvas-motion.test.js can
// assert bounding without reaching into module state.
export function _shakeTableCountForTest() {
    return _shakeTables.size;
}

// True if at least one visible layer in `layers` has motion != static.
// The editor's rAF loop kicks ONLY when this is true and pauses when
// the slide goes back to fully static — no idle CPU burn for static
// slides, matches the spec's perf rationale.
export function anyLayerAnimated(layers) {
    if (!Array.isArray(layers)) return false;
    for (const layer of layers) {
        if (!layer || layer.visible === false) continue;
        const m = layer.motion;
        if (m && m !== "static") return true;
    }
    return false;
}

// Wrap a paint call with motion transforms. `paintFn` is the actual
// layer-rendering closure (typically a paintLayer call bound to this
// layer's data); the wrapper sets up ctx.save / transform / alpha,
// invokes paintFn one or two times depending on the motion (twice
// for ticker so the wrap shows BOTH copies as the shift transitions),
// and ctx.restores. Only the ticker branch installs a box clip (its
// two-copy wrap needs it); displacement effects run unclipped to
// match the Rust device renderer (parity Bug 3). paintFn must NOT
// call ctx.save/ctx.restore externally — this wrapper owns the
// save stack.
export function paintLayerWithMotion(ctx, canvas, layer, paintFn, opts) {
    const motion = (layer && layer.motion) || "static";
    const elapsed = opts && opts.elapsed_s;
    if (motion === "static" || elapsed === undefined || elapsed === null) {
        paintFn();
        return;
    }
    const intensity = layer.motion_intensity ?? layer.motionIntensity ?? 50;
    const motionPhase = layer.motion_phase ?? layer.motionPhase ?? 0;
    // motion_speed multiplies the effect's frequency (B2, 2026-05-05).
    // 1.0 = original speed, 0.0 = frozen, 2.0 = double speed. Schema
    // range is [0, 2]; clamp defensively in case a hand-edited slide
    // ships an out-of-range value.
    const speedRaw = layer.motion_speed ?? layer.motionSpeed ?? 1.0;
    const speed = Math.max(0, Math.min(2, Number(speedRaw) || 0));
    const layerKey = (opts && opts.layerKey) || "0";

    const box = layer.box || _DEFAULT_BOX;
    const bx = box.x * canvas.width;
    const by = box.y * canvas.height;
    const bw = Math.max(1, box.w * canvas.width);
    const bh = Math.max(1, box.h * canvas.height);

    const freq = effectFreq(motion, intensity) * speed;
    const phase = computePhase(elapsed, freq, motionPhase);

    ctx.save();
    try {
        if (motion === "blink") {
            // Square wave 50% duty. No displacement — unclipped.
            if (phase < 0.5) paintFn();
            return;
        }
        if (motion === "ticker") {
            // Box-bounded clip — REQUIRED by the ticker mechanism:
            // the text is drawn TWICE (one copy exiting the left
            // edge while the second enters from the right) and the
            // clip is what makes that read as a single scrolling
            // wrap instead of two visible copies. This is the only
            // effect that clips (parity Bug 3) — it's a rendering
            // mechanism, not a containment policy.
            ctx.beginPath();
            ctx.rect(bx, by, bw, bh);
            ctx.clip();
            // Linear horizontal travel inside box, drawn TWICE so the
            // wrap is visible (text exits the left edge while the
            // second copy is entering from the right). This is the
            // canvas analogue of motion.py's np.roll wrap.
            const shift = -phase * bw;
            ctx.translate(shift, 0);
            paintFn();
            ctx.translate(bw, 0);
            paintFn();
            return;
        }
        if (motion === "breathe") {
            // Spec range: ±2% → ±20% (docs/text-layer-motion-spec.md:229).
            // amp = 0.02 + 0.18 * intensity_norm matches Rust
            // motion_breathe exactly. Previous Canvas2D formula
            // (intensity/100)*0.20 zeroed amp at intensity=0, missing
            // the ±2% baseline the spec calls for.
            const intensityNorm = intensity / 100.0;
            const amp = 0.02 + 0.18 * intensityNorm;
            const s = 1.0 + amp * Math.sin(2 * Math.PI * phase);
            const cx = bx + bw / 2;
            const cy = by + bh / 2;
            ctx.translate(cx, cy);
            ctx.scale(s, s);
            ctx.translate(-cx, -cy);
            paintFn();
            return;
        }
        if (motion === "pulse") {
            // Spec range: 70-100% shallow → 0-100% deep (docs/text-
            // layer-motion-spec.md:230). min_alpha = 0.70*(1-intensity_norm)
            // matches Rust motion_pulse exactly. Previous Canvas2D
            // formula (minA = 1 - intensity/100) gave alpha=1 constant
            // at intensity=0 (no pulse) instead of the spec's 70-100%
            // shallow sweep.
            const intensityNorm = intensity / 100.0;
            const minA = 0.70 * (1.0 - intensityNorm);
            const sin01 = (Math.sin(2 * Math.PI * phase) + 1) / 2;
            const a = minA + (1.0 - minA) * sin01;
            ctx.globalAlpha *= a;
            paintFn();
            return;
        }
        if (motion === "bounce") {
            // Spec range: ±1% → ±10% (docs/text-layer-motion-spec.md:231).
            // amp = 0.01 + 0.09 * intensity_norm matches Rust
            // motion_bounce. Previous Canvas2D formula (intensity/100)
            // *0.10 zeroed amp at intensity=0 (no bounce), missing the
            // ±1% baseline the spec specifies.
            //
            // Shape stays as symmetric sin() here in the editor preview;
            // Rust device path uses abs(sin) for true ball-on-floor
            // (qarl decision, see motion_bounce in hdmi_logic.rs). Per
            // the editor-vs-device approximate-preview spec lock, the
            // two are not required to be pixel-identical on shape.
            const intensityNorm = intensity / 100.0;
            const amp = 0.01 + 0.09 * intensityNorm;
            const offsetY = amp * bh * Math.sin(2 * Math.PI * phase);
            ctx.translate(0, offsetY);
            paintFn();
            return;
        }
        if (motion === "shake") {
            // qarl 2026-07-16 — per-LETTER jitter. Previously this
            // ctx.translate'd the WHOLE layer, so a line moved as a
            // rigid unit. Now we hand the paint closure a per-glyph
            // offset provider and let it displace each letter around
            // its own base position (mirrors the Rust device path,
            // which offsets each glyph quad instead of the layer).
            //
            // paintFn(glyphOffset) — the text painter applies the
            // offset per character; a painter that ignores the
            // argument simply renders unshaken (forward-compatible).
            if (intensity > 0) {
                const step = Math.floor(phase * 10);
                const ampPx = (intensity / 100.0) * 0.04 * bh;
                // Round 21 memo, now per (layerKey, motionPhase,
                // glyphIndex): per-frame per-letter work is one
                // Map.get + one array index; no string-build, no FNV
                // walk. Same amp curve + 10 Hz step as before — only
                // the granularity changed.
                const glyphOffset = (glyphIndex) => {
                    const { dxG, dyG } =
                        getShakeTable(layerKey, motionPhase, glyphIndex | 0)[step];
                    return [
                        Math.round((dxG * ampPx) / 2),
                        Math.round((dyG * ampPx) / 2),
                    ];
                };
                paintFn(glyphOffset);
            } else {
                paintFn();
            }
            return;
        }
        // Unknown motion — render as static (forward-compat with
        // future spec values the renderer doesn't know about yet).
        paintFn();
    } finally {
        ctx.restore();
    }
}
