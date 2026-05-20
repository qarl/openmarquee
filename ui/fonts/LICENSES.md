# Bundled fonts — licenses

All redistributable. The TTF files in this directory are shipped with
openMarquee so a captive-portal operator can pick from a curated display
set without needing internet at runtime.

| File | Family | License | Source |
|---|---|---|---|
| `inter.ttf` | Inter | SIL OFL-1.1 | https://fonts.google.com/specimen/Inter |
| `oswald.ttf` | Oswald | SIL OFL-1.1 | https://fonts.google.com/specimen/Oswald |
| `bebas-neue.ttf` | Bebas Neue | SIL OFL-1.1 | https://fonts.google.com/specimen/Bebas+Neue |
| `roboto-slab.ttf` | Roboto Slab | Apache-2.0 | https://fonts.google.com/specimen/Roboto+Slab |
| `caveat-brush.ttf` | Caveat Brush | SIL OFL-1.1 | https://fonts.google.com/specimen/Caveat+Brush |
| `permanent-marker.ttf` | Permanent Marker | Apache-2.0 | https://fonts.google.com/specimen/Permanent+Marker |
| `cinzel.ttf` | Cinzel | SIL OFL-1.1 | https://fonts.google.com/specimen/Cinzel |
| `unifrakturcook.ttf` | UnifrakturCook | SIL OFL-1.1 | https://fonts.google.com/specimen/UnifrakturCook |
| `rye.ttf` | Rye | SIL OFL-1.1 | https://fonts.google.com/specimen/Rye |
| `pacifico.ttf` | Pacifico | SIL OFL-1.1 | https://fonts.google.com/specimen/Pacifico |
| `sedgwick-ave-display.ttf` | Sedgwick Ave Display | SIL OFL-1.1 | https://fonts.google.com/specimen/Sedgwick+Ave+Display |
| `bowlby-one-sc.ttf` | Bowlby One SC | SIL OFL-1.1 | https://fonts.google.com/specimen/Bowlby+One+SC |
| `anton.ttf` | Anton | SIL OFL-1.1 | https://fonts.google.com/specimen/Anton |
| `archivo-black.ttf` | Archivo Black | SIL OFL-1.1 | https://fonts.google.com/specimen/Archivo+Black |
| `alfa-slab-one.ttf` | Alfa Slab One | SIL OFL-1.1 | https://fonts.google.com/specimen/Alfa+Slab+One |
| `playfair-display.ttf` | Playfair Display | SIL OFL-1.1 | https://fonts.google.com/specimen/Playfair+Display |
| `dm-serif-display.ttf` | DM Serif Display | SIL OFL-1.1 | https://fonts.google.com/specimen/DM+Serif+Display |
| `vt323.ttf` | VT323 | SIL OFL-1.1 | https://fonts.google.com/specimen/VT323 |
| `jetbrains-mono.ttf` | JetBrains Mono | SIL OFL-1.1 | https://fonts.google.com/specimen/JetBrains+Mono |
| `space-mono.ttf` | Space Mono | SIL OFL-1.1 | https://fonts.google.com/specimen/Space+Mono |
| `caveat.ttf` | Caveat | SIL OFL-1.1 | https://fonts.google.com/specimen/Caveat |
| `reenie-beanie.ttf` | Reenie Beanie | SIL OFL-1.1 | https://fonts.google.com/specimen/Reenie+Beanie |
| `shadows-into-light.ttf` | Shadows Into Light | SIL OFL-1.1 | https://fonts.google.com/specimen/Shadows+Into+Light |
| `dejavu-sans.ttf` | DejaVu Sans | Bitstream Vera + Public Domain | https://github.com/dejavu-fonts/dejavu-fonts |
| `noto-color-emoji-colrv1.ttf` | Noto Color Emoji (COLRv1, SVG-stripped) | SIL OFL-1.1 | https://github.com/google/fonts/tree/main/ofl/notocoloremoji |

SIL OFL-1.1: https://openfontlicense.org/
Apache-2.0: http://www.apache.org/licenses/LICENSE-2.0
Bitstream Vera license: see the DejaVu fonts repo for the upstream
LICENSE file — permits redistribution + modification (with rename)
without royalty. DejaVu's modifications are public domain.

`dejavu-sans.ttf` is the fallback font for the runtime glyph cache
(Bug 3 Slice 2D). Codepoints absent from the primary font (e.g. ●
U+25CF on VT323) fall through to DejaVu Sans, which covers Geometric
Shapes, Mathematical Operators, Box Drawing, Block Elements, and
Arrows. Picked over Noto Sans because Noto Sans Regular ships
without Geometric Shapes; DejaVu Sans covers 5918 codepoints in one
~750 KB TTF.

`noto-color-emoji-colrv1.ttf` is THE emoji font on the device
post-Slice-3D. Bug 3 Slice 3A.rev added the COLRv1 rasterizer
module (skrifa + tiny-skia); 3B wired runtime dispatch + a
dedicated dynamic atlas page; 3D retired the build-time CBDT
bake of the older `noto-color-emoji.ttf` so emoji rasterize on
demand at the cell size (96 px) and sample with bilinear filter,
giving crisp edges at any on-screen size. The upstream tarball
is 24.3 MB because it bundles an SVG table for browser fallback
(80% of the file); `scripts/download-emoji-font-colrv1.sh`
strips SVG via fontTools before bundling because skrifa (the
renderer's COLRv1 paint-tree reader) needs only COLR/CPAL/glyf,
dropping the file to 4.8 MB. The download+strip step is wired
into `scripts/setup.sh`.
