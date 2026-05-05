// Procedural background pattern system — editor-side helpers.
//
// 11 patterns from the qarl 2026-05-03 designer handoff. Each is a
// pure function `(color_a, color_b, density 0..1) → CSS background
// value`. Backend mirror lives at
// backend/openmarquee/auto_render.py: render_pattern (one numpy
// implementation per pattern). The JS side renders for the editor
// canvas + thumbnails; the backend side rasterizes the same pattern
// into the slide's bg cache at slide entry. WYSIWYG.
//
// Palette / mood / shuffle features from the original design bundle
// are intentionally NOT ported — operator picks colors directly.

const lerp = (a, b, t) =>
    a + (b - a) * Math.max(0, Math.min(1, t));

// Numerical Recipes LCG -- deterministic 0..1 stream from a seed.
// Used by patterns that need stable per-render randomness (e.g.
// confetti scatter, B5). Not cryptographic; just needs to be
// reproducible across calls with the same seed.
function lcgRandom(seed) {
    let state = (seed | 0) || 1;
    return () => {
        state = (Math.imul(state, 1664525) + 1013904223) | 0;
        return ((state >>> 0) / 0x100000000);
    };
}

// 12-name list in display order (matches the picker grid order).
export const PATTERN_NAMES = [
    "solid", "gradient", "dots", "halftone", "stripes",
    "scanlines", "checker", "grid", "rings", "rays", "confetti", "bricks",
];

// Friendly label per pattern (for the picker tile + readout).
export const PATTERN_LABELS = {
    solid: "Solid",
    gradient: "Gradient",
    dots: "Dots",
    halftone: "Halftone",
    stripes: "Stripes",
    scanlines: "Scanlines",
    checker: "Checker",
    grid: "Grid",
    rings: "Rings",
    rays: "Rays",
    confetti: "Confetti",
    bricks: "Bricks",
};

const BUILDERS = {
    solid: (a /*, b, d */) => a,

    gradient: (a, b, d = 0.5) => {
        // Density 0..1 → angle 0..270deg. CSS-like: 0deg = top→bot,
        // 90deg = left→right, 180deg = bot→top, 270deg = right→left.
        const angle = Math.round(lerp(0, 270, d));
        return `linear-gradient(${angle}deg, ${a}, ${b})`;
    },

    dots: (a, b, d = 0.5) => {
        // Widened lerp(48, 4) -- prior lerp(28, 8) was visually narrow
        // at both extremes (B12, 2026-05-05).
        const tile = Math.round(lerp(48, 4, d));
        const r = Math.max(2, Math.round(tile * 0.22));
        return [
            `radial-gradient(circle at center, ${b} ${r}px, transparent ${r + 1}px) 0 0 / ${tile}px ${tile}px`,
            a,
        ].join(", ");
    },

    halftone: (a, b, d = 0.5) => {
        // Widened lerp(60, 6) for more striking sparse/dense extremes
        // (B12, 2026-05-05).
        const tile = Math.round(lerp(60, 6, d));
        const r = Math.round(tile * 0.34);
        const half = tile / 2;
        return [
            `radial-gradient(circle at center, ${b} ${r}px, transparent ${r + 1}px) 0 0 / ${tile}px ${tile}px`,
            `radial-gradient(circle at center, ${b} ${r}px, transparent ${r + 1}px) ${half}px ${half}px / ${tile}px ${tile}px`,
            a,
        ].join(", ");
    },

    stripes: (a, b, d = 0.5) => {
        // Widened lerp(80, 4) -- B12, 2026-05-05.
        const tile = Math.round(lerp(80, 4, d));
        const half = tile / 2;
        return `repeating-linear-gradient(45deg, ${a} 0 ${half}px, ${b} ${half}px ${tile}px)`;
    },

    scanlines: (a, b, d = 0.5) => {
        // Widened lerp(16, 2) -- B12, 2026-05-05.
        const tile = Math.round(lerp(16, 2, d));
        return [
            `repeating-linear-gradient(0deg, ${b} 0 1px, transparent 1px ${tile}px)`,
            a,
        ].join(", ");
    },

    checker: (a, b, d = 0.5) => {
        // Widened lerp(60, 4) -- B12, 2026-05-05.
        const tile = Math.round(lerp(60, 4, d));
        return `conic-gradient(${b} 0 25%, ${a} 0 50%, ${b} 0 75%, ${a} 0) 0 0 / ${tile}px ${tile}px`;
    },

    grid: (a, b, d = 0.5) => {
        // Graph-paper layout. Color A = grid line, Color B = paper.
        // Cell size shrinks with density (lerp(120, 4)). 1px lines.
        // (B17 + widened by B12, 2026-05-05.)
        const tile = Math.round(lerp(120, 4, d));
        return [
            `repeating-linear-gradient(0deg, ${a} 0 1px, transparent 1px ${tile}px)`,
            `repeating-linear-gradient(90deg, ${a} 0 1px, transparent 1px ${tile}px)`,
            b,
        ].join(", ");
    },

    rings: (a, b, d = 0.5) => {
        // Widened lerp(120, 6) -- B12, 2026-05-05.
        const tile = Math.round(lerp(120, 6, d));
        const half = tile / 2;
        return `repeating-radial-gradient(circle at 50% 50%, ${a} 0 ${half - 2}px, ${b} ${half - 2}px ${half}px)`;
    },

    rays: (a, b, d = 0.5) => {
        // Slice count is always even -- with an odd count the slice at
        // angle wrap-around (0 / 2pi) joins two same-colored slices,
        // visibly doubling the top ray (B15, 2026-05-05).
        // Widened to 2*round(lerp(2, 24)) so range is 4..48 -- B12,
        // 2026-05-05.
        const slices = 2 * Math.round(lerp(2, 24, d));
        const stops = [];
        const step = 100 / slices;
        for (let i = 0; i < slices; i++) {
            const c = i % 2 === 0 ? a : b;
            stops.push(`${c} ${(i * step).toFixed(2)}% ${((i + 1) * step).toFixed(2)}%`);
        }
        return `conic-gradient(from 0deg at 50% 50%, ${stops.join(", ")})`;
    },

    confetti: (a, b, d = 0.5) => {
        // Widened lerp(100, 14) -- B12, 2026-05-05.
        const t1 = Math.round(lerp(100, 14, d));
        const t2 = Math.round(t1 * 0.7);
        const t3 = Math.round(t1 * 1.3);
        const r = Math.max(2, Math.round(t1 * 0.08));
        return [
            `radial-gradient(circle at 18% 22%, ${b} ${r}px, transparent ${r + 1}px) 0 0 / ${t1}px ${t1}px`,
            `radial-gradient(circle at 72% 38%, ${b} ${r}px, transparent ${r + 1}px) ${t2 / 2}px ${t2 / 3}px / ${t2}px ${t2}px`,
            `radial-gradient(circle at 44% 80%, ${b} ${r + 1}px, transparent ${r + 2}px) ${t3 / 4}px ${t3 / 2}px / ${t3}px ${t3}px`,
            `radial-gradient(circle at 90% 70%, ${b} ${r}px, transparent ${r + 1}px) 0 ${t1 / 3}px / ${t1}px ${t1}px`,
            a,
        ].join(", ");
    },

    bricks: (a, b, d = 0.5) => {
        // Widened lerp(140, 16) -- B12, 2026-05-05.
        const w = Math.round(lerp(140, 16, d));
        const h = Math.round(w / 2);
        const half = w / 2;
        // 2px mortar -- 1px reads as a uniform grid at thumbnail scale
        // (B16, 2026-05-05); 2px lets the offset alternating rows
        // actually register as bricks.
        return [
            `repeating-linear-gradient(0deg, ${b} 0 2px, transparent 2px ${h}px)`,
            `repeating-linear-gradient(90deg, ${b} 0 2px, transparent 2px ${w}px) 0 0 / ${w}px ${h * 2}px`,
            `repeating-linear-gradient(90deg, ${b} 0 2px, transparent 2px ${w}px) ${half}px ${h}px / ${w}px ${h * 2}px`,
            a,
        ].join(", ");
    },
};

// Build a CSS background value for the given pattern + colors.
// Falls back to a solid color_a fill on an unknown pattern name.
export function buildBg(pattern, color_a, color_b, density) {
    const fn = BUILDERS[pattern] || BUILDERS.solid;
    return fn(color_a, color_b, density);
}

// Whether the pattern uses color_b. Used by the editor UI to grey
// out the Color B input for `solid`.
export function patternUsesColorB(pattern) {
    return pattern !== "solid";
}

// Whether the pattern uses the density slider. Solid is the only
// pattern that ignores it; gradient repurposes it as angle, but the
// slider is still active.
export function patternUsesDensity(pattern) {
    return pattern !== "solid";
}

// Density label shown next to the slider — relabeled "Angle" for
// gradient (where density 0..1 maps to angle 0..270deg).
export function densityLabelFor(pattern) {
    return pattern === "gradient" ? "Angle" : "Density";
}

// Canvas-side renderer — paints the chosen pattern directly onto the
// passed 2D context, covering [0..width, 0..height]. Mirrors the
// device-side render_pattern() in backend/openmarquee/auto_render.py
// so the editor canvas (which becomes the saved slide PNG) and the
// device renderer agree visually. Pixel-perfect parity isn't
// required; matching tile sizes / dot radii / band spacing is.
//
// Used by editor.drawCanvas for bgSource === "pattern". The CSS
// version (buildBg) is used for the small thumbnail tiles in the
// pattern picker — that's where CSS gradient stacks are cheap and
// canvas painting would be wasteful.
export function paintPatternOnCanvas(
    ctx, width, height, pattern, a, b, density,
) {
    ctx.save();
    try {
        // Solid color_a base — every pattern (except solid itself,
        // which short-circuits) starts here.
        ctx.fillStyle = a;
        ctx.fillRect(0, 0, width, height);
        if (pattern === "solid") return;

        if (pattern === "gradient") {
            const angleDeg = Math.round(lerp(0, 270, density));
            const rad = (angleDeg * Math.PI) / 180;
            const dx = Math.sin(rad);
            const dy = Math.cos(rad);
            const cx = width / 2;
            const cy = height / 2;
            const half = Math.abs(dx) * (width / 2)
                + Math.abs(dy) * (height / 2);
            const g = ctx.createLinearGradient(
                cx - dx * half, cy - dy * half,
                cx + dx * half, cy + dy * half,
            );
            g.addColorStop(0, a);
            g.addColorStop(1, b);
            ctx.fillStyle = g;
            ctx.fillRect(0, 0, width, height);
            return;
        }

        if (pattern === "dots") {
            const tile = Math.round(lerp(48, 4, density));
            const r = Math.max(2, Math.round(tile * 0.22));
            ctx.fillStyle = b;
            for (let y = tile / 2; y < height; y += tile) {
                for (let x = tile / 2; x < width; x += tile) {
                    ctx.beginPath();
                    ctx.arc(x, y, r, 0, Math.PI * 2);
                    ctx.fill();
                }
            }
            return;
        }

        if (pattern === "halftone") {
            const tile = Math.round(lerp(60, 6, density));
            const r = Math.round(tile * 0.34);
            ctx.fillStyle = b;
            for (let layer = 0; layer < 2; layer++) {
                const ox = layer === 0 ? tile / 2 : tile;
                const oy = layer === 0 ? tile / 2 : tile;
                // Start one tile before the canvas origin so layer 1's
                // dot centered at (0, 0) renders -- otherwise the top
                // and left edges clip while right and bottom bleed
                // (B18, 2026-05-05).
                for (let y = oy - tile; y < height + tile; y += tile) {
                    for (let x = ox - tile; x < width + tile; x += tile) {
                        ctx.beginPath();
                        ctx.arc(x, y, r, 0, Math.PI * 2);
                        ctx.fill();
                    }
                }
            }
            return;
        }

        if (pattern === "stripes") {
            // 45deg diagonal alternating bands. Use repeating linear
            // gradient via CanvasPattern.
            const tile = Math.round(lerp(80, 4, density));
            const half = tile / 2;
            // Build an off-canvas tile aligned to the diagonal.
            // Simpler: rotate context, draw vertical bands, restore.
            ctx.save();
            ctx.rotate(Math.PI / 4);
            const diag = Math.ceil(Math.sqrt(width * width + height * height));
            ctx.fillStyle = b;
            for (let x = -diag; x < diag; x += tile) {
                ctx.fillRect(x + half, -diag, half, diag * 2);
            }
            ctx.restore();
            return;
        }

        if (pattern === "scanlines") {
            const tile = Math.max(2, Math.round(lerp(16, 2, density)));
            ctx.fillStyle = b;
            for (let y = 0; y < height; y += tile) {
                ctx.fillRect(0, y, width, 1);
            }
            return;
        }

        if (pattern === "checker") {
            const tile = Math.round(lerp(60, 4, density));
            ctx.fillStyle = b;
            const cols = Math.ceil(width / tile);
            const rows = Math.ceil(height / tile);
            for (let row = 0; row < rows; row++) {
                for (let col = 0; col < cols; col++) {
                    if ((col + row) % 2 === 1) {
                        ctx.fillRect(col * tile, row * tile, tile, tile);
                    }
                }
            }
            return;
        }

        if (pattern === "grid") {
            // Color A = grid line, Color B = paper. Repaint base from
            // color_a (set above) to color_b first, then 1px lines in
            // color_a. (B17, 2026-05-05.)
            ctx.fillStyle = b;
            ctx.fillRect(0, 0, width, height);
            const tile = Math.max(4, Math.round(lerp(120, 4, density)));
            ctx.fillStyle = a;
            for (let y = 0; y < height; y += tile) {
                ctx.fillRect(0, y, width, 1);
            }
            for (let x = 0; x < width; x += tile) {
                ctx.fillRect(x, 0, 1, height);
            }
            return;
        }

        if (pattern === "rings") {
            const tile = Math.max(4, Math.round(lerp(120, 6, density)));
            const half = tile / 2;
            const cx = width / 2;
            const cy = height / 2;
            const maxDist = Math.ceil(Math.sqrt(cx * cx + cy * cy));
            ctx.strokeStyle = b;
            ctx.lineWidth = 2;
            for (let r = half; r < maxDist; r += tile) {
                ctx.beginPath();
                ctx.arc(cx, cy, r, 0, Math.PI * 2);
                ctx.stroke();
            }
            return;
        }

        if (pattern === "rays") {
            // Always even -- see B15 note in the rays builder above.
            const slices = Math.max(2, 2 * Math.round(lerp(2, 24, density)));
            const cx = width / 2;
            const cy = height / 2;
            const maxR = Math.ceil(Math.sqrt(cx * cx + cy * cy)) + 1;
            ctx.fillStyle = b;
            const step = (Math.PI * 2) / slices;
            for (let i = 1; i < slices; i += 2) {
                const a0 = i * step;
                const a1 = (i + 1) * step;
                ctx.beginPath();
                ctx.moveTo(cx, cy);
                ctx.arc(cx, cy, maxR, a0, a1);
                ctx.closePath();
                ctx.fill();
            }
            return;
        }

        if (pattern === "confetti") {
            // Deterministic full-canvas particle scatter -- replaces the
            // tiled 4-layer dot grid that had visible repeat seams at
            // 1080p (B5, 2026-05-05). Particle count grows with density.
            // Fixed PRNG seed means a given (density) produces the same
            // scatter every render -- live edits feel stable, not random.
            // NB: editor preview and device renderer use different RNG
            // families with the same seed -- both deterministic per-surface
            // but they will not pixel-match. Visual character is the same.
            const count = Math.round(lerp(80, 2000, density));
            const rng = lcgRandom(0xC0FFE71);
            ctx.fillStyle = b;
            for (let i = 0; i < count; i++) {
                const x = rng() * width;
                const y = rng() * height;
                const r = 2 + Math.floor(rng() * 4);
                ctx.beginPath();
                ctx.arc(x, y, r, 0, Math.PI * 2);
                ctx.fill();
            }
            return;
        }

        if (pattern === "bricks") {
            const w = Math.max(8, Math.round(lerp(140, 16, density)));
            const h = Math.max(4, Math.round(w / 2));
            const half = w / 2;
            ctx.fillStyle = b;
            // 2px mortar -- see B16 note in bricks builder.
            for (let y = 0; y < height; y += h) {
                ctx.fillRect(0, y, width, 2);
            }
            for (let row = 0, y = 0; y < height; row++, y += h) {
                const offset = row % 2 === 0 ? 0 : half;
                for (let x = offset; x < width; x += w) {
                    ctx.fillRect(x, y, 2, h);
                }
            }
            return;
        }
    } finally {
        ctx.restore();
    }
}
