// JS port of compose_motion_frame for canvas-based previews (editor's
// main slide preview, eventually the playlist inline-preview). Mirrors
// the math in backend/openmarquee/motion.py so the editor preview
// shows operators what the device renderer will produce — visually
// approximate, not pixel-identical (per docs/text-layer-motion-spec.md
// Q3 lock).
//
// Six effects + static. All effects are box-bounded — the layer's
// `box` rect clips the motion via ctx.clip(). Phase comes from a
// shared monotonic clock (the rAF loop's `elapsed_s`) plus the layer's
// `motion_phase` offset, so two layers in the same slide stay coherent.

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
function hashedUniform(layerKey, motionPhase, step) {
    const s = `${layerKey}:${(motionPhase || 0).toFixed(6)}:${step}`;
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
function gaussian(layerKey, motionPhase, step) {
    const u1 = Math.max(1e-9, hashedUniform(layerKey, motionPhase, step * 2));
    const u2 = hashedUniform(layerKey, motionPhase, step * 2 + 1);
    return Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2);
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

// Wrap a paint call with motion transforms + box clip. `paintFn` is
// the actual layer-rendering closure (typically a paintLayer call
// bound to this layer's data); the wrapper sets up ctx.save / clip /
// transform / alpha, invokes paintFn one or two times depending on
// the motion (twice for ticker so the wrap shows BOTH copies as the
// shift transitions), and ctx.restores. paintFn must NOT call
// ctx.save/ctx.restore externally — this wrapper owns the save stack.
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
        // Box-bounded clip — every effect respects the layer's box.
        ctx.beginPath();
        ctx.rect(bx, by, bw, bh);
        ctx.clip();

        if (motion === "blink") {
            // Square wave 50% duty.
            if (phase < 0.5) paintFn();
            return;
        }
        if (motion === "ticker") {
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
            const amp = (intensity / 100.0) * 0.20;
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
            const minA = 1.0 - intensity / 100.0;
            const sin01 = (Math.sin(2 * Math.PI * phase) + 1) / 2;
            const a = minA + (1.0 - minA) * sin01;
            ctx.globalAlpha *= a;
            paintFn();
            return;
        }
        if (motion === "bounce") {
            const amp = (intensity / 100.0) * 0.10;
            const offsetY = amp * bh * Math.sin(2 * Math.PI * phase);
            ctx.translate(0, offsetY);
            paintFn();
            return;
        }
        if (motion === "shake") {
            if (intensity > 0) {
                const step = Math.floor(phase * 10);
                const ampPx = (intensity / 100.0) * 0.04 * bh;
                const dx = Math.round(gaussian(layerKey, motionPhase, step) * ampPx / 2);
                const dy = Math.round(
                    gaussian(layerKey, motionPhase, step + 100000) * ampPx / 2,
                );
                ctx.translate(dx, dy);
            }
            paintFn();
            return;
        }
        // Unknown motion — render as static (forward-compat with
        // future spec values the renderer doesn't know about yet).
        paintFn();
    } finally {
        ctx.restore();
    }
}
