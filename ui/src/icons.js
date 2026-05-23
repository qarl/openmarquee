// Inline SVG icons for the new om-* design.
//
// Each icon is a function that returns an SVG STRING (so the existing
// vanilla-JS templates can interpolate them into innerHTML). Single-line
// stroke style, 18-20px target size, currentcolor everywhere so they
// pick up text color from their context.
//
// Mirrors the JSX icons from the design bundle's icons.jsx but with
// string output so we don't need React to consume them.

function svg(path, opts = {}) {
    return ({ size = 18, strokeWidth = 1.6, className = "" } = {}) =>
        `<svg viewBox="${opts.vb || "0 0 24 24"}" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="${strokeWidth}" stroke-linecap="round" stroke-linejoin="round" class="${className}">${path}</svg>`;
}

export const Icons = {
    // Marquee mark — a 4×3 dot grid, lit. Used in the wordmark.
    Mark: ({ size = 16 } = {}) => {
        const dots = [];
        for (let c = 0; c < 4; c++) {
            for (let r = 0; r < 3; r++) {
                dots.push(
                    `<circle cx="${2 + c * 4}" cy="${2 + r * 4}" r="1.4" fill="currentColor"/>`,
                );
            }
        }
        return `<svg viewBox="0 0 16 12" width="${size}" height="${size * 0.75}">${dots.join("")}</svg>`;
    },
    Slides: svg('<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 9h18"/>'),
    Text: svg('<path d="M5 6h14M12 6v13M9 19h6"/>'),
    Image: svg(
        '<rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="11" r="1.5"/><path d="M3 17l5-5 4 4 3-3 6 5"/>',
    ),
    Video: svg(
        '<rect x="3" y="6" width="14" height="12" rx="2"/><path d="M17 10l4-2v8l-4-2"/>',
    ),
    Playlist: svg(
        '<path d="M4 7h12M4 12h12M4 17h7"/><circle cx="19" cy="16" r="2"/><path d="M21 16V9l-2 1"/>',
    ),
    Schedule: svg(
        '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
    ),
    Settings: svg(
        '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>',
    ),
    Flock: svg(
        '<circle cx="6" cy="9" r="2.5"/><circle cx="18" cy="9" r="2.5"/><circle cx="12" cy="17" r="2.5"/><path d="M8 10l3 5M16 10l-3 5"/>',
    ),
    // Live — broadcasting waves emanating from a center dot. Reads as
    // "live signal going out" without overlapping the Video icon's
    // camera-and-lens shape.
    Live: svg(
        '<circle cx="12" cy="12" r="2"/><path d="M8.5 8.5a5 5 0 000 7M15.5 8.5a5 5 0 010 7M5.5 5.5a9 9 0 000 13M18.5 5.5a9 9 0 010 13"/>',
    ),
    Plus: svg('<path d="M12 5v14M5 12h14"/>'),
    Menu: svg('<path d="M4 6h16M4 12h16M4 18h16"/>'),
    Close: svg('<path d="M6 6l12 12M18 6L6 18"/>'),
    Push: svg('<path d="M12 19V5M5 12l7-7 7 7"/>'),
    Sync: svg(
        '<path d="M4 12a8 8 0 0114-5l2-2v6h-6l2-2a5 5 0 00-9 3M20 12a8 8 0 01-14 5l-2 2v-6h6l-2 2a5 5 0 009-3"/>',
    ),
};
