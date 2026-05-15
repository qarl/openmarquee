# Cross-renderer parity harness — current state 2026-05-14

Findings deliverable for the QA pixel-comparison v1 slice + the
Boot-eyeball follow-up. Pairs with the renderer parity audit
(qa/captures/renderer-parity-audit-2026-05-14.md, amendment 46e30d4).

**TL;DR**: The harness already exists in tree and hard-gates. Running
against HEAD: **39 / 39 fixtures FAIL** the existing tolerance bands.
The dominant divergence is **text vertical positioning + per-engine
glyph rasterization** — NOT kerning, as qarl correctly suspected.
Background math itself is within 1-2 LSB where it can be isolated.

## §1 The harness is built

Slice 1 of the dispatch (Canvas2D-vs-Rust automated gate) is mostly
already in tree:

- `scripts/parity_tests.sh` — entry-point wrapper
- `scripts/parity/run.py` — 309-line Playwright + scikit-image driver
- `scripts/parity/fixtures.json` — 39 fixtures across text, patterns,
  fonts, blend modes, motion, transitions
- `ui/parity-harness.html` — module-loaded Canvas2D capture page
  (drives `__parityCapture(itemJson, tickSeconds)`)
- `renderer/tests/golden/*.png` — 39 Rust goldens (last re-blessed
  at `fb3f6a3 renderer: re-bless goldens after layout fix (c56314f)`)
- `renderer/tests/parity/captures/` — output directory
- Hard-gate-by-default (qarl-direct 2026-05-13 per run.py:292-297);
  exit non-zero on threshold miss
- Existing thresholds: `ssim_min: 0.95`, `max_delta_max: 50`

**Net for the v1 dispatch**: NO new harness needs to be built. Slice
1's deliverables (Canvas2D path + Rust path + comparison metric + test
runner) are all present.

What's NOT in tree:
- CI wire-up (the script runs locally but isn't gating any commit yet)
- The dispatch's tighter `max_delta=2` threshold (current is 50)
- A "strict mode" CLI flag to override fixtures.json thresholds

## §2 Current state — 39 / 39 FAIL

Verbatim from `bash scripts/parity_tests.sh` (exit=1):

| Fixture (representative) | SSIM (≥0.95) | max_delta (≤50) | mean_delta | pct_over_10 |
|---|---|---|---|---|
| parity_bg_pattern_solid | **0.92** | **229** | 19.5 | 9.3% |
| parity_text_static | **0.93** | **229** | 11.5 | 5.4% |
| parity_motion_ticker | **0.62** | **251** | 56.3 | 33.8% |
| parity_transition_fade | **0.60** | **250** | 41.2 | 49.3% |
| parity_bg_pattern_rings | **0.67** | **229** | 68.6 | 54.1% |
| parity_bg_pattern_gradient | **0.93** | **197** | 12.8 | 8.5% |
| parity_font_inter | **0.92** | **231** | 17.8 | 7.9% |

Full table in `renderer/tests/parity/captures/metrics.json`.

All 39 fixtures fail BOTH thresholds simultaneously. This is a P0
finding that **the audit's code-level "high confidence" missed**.

## §3 Where the drift actually lives

Per-fixture analysis (top-strip y=0..80 = background-only band):

```
fixture                          bg-strip max_d   bg-strip mean_d
parity_bg_pattern_solid                0          0.00      ← clean
parity_blend_*                         0          0.00      ← clean
parity_font_inter                      0          0.00      ← clean (top is bg)
parity_bg_pattern_gradient             1          0.21      ← LSB
...
parity_bg_pattern_rings              229         62.20      ← bg math drift
parity_bg_pattern_stripes            229         31.17      ← bg math drift
parity_bg_pattern_scanlines          229          9.22      ← bg math drift
parity_bg_pattern_halftone           229          7.69      ← mild bg drift
```

**Two classes of divergence** are visible:

1. **Text-heavy fixtures (most)**: bg strips clean within 1 LSB →
   divergence concentrates in text regions. Confirmed for fonts,
   blends, solid, dots — the body of the canvas has text, the top
   strip doesn't. Drift here is the **font-engine + line-height +
   glyph-AA** stack (P1 #7 + P2 #11 in the audit).

2. **Pattern-heavy fixtures (rings, stripes, scanlines)**: bg strips
   show real divergence even WITHOUT text. The pattern shader (Rust
   FS_PATTERN_*) and Canvas2D `paintPatternOnCanvas` produce
   visibly-different output. **Not flagged in the original audit** —
   the audit said patterns were in lockstep per the `hdmi_logic.rs:
   2489-2493` comment ("Both sides must stay in lockstep for WYSIWYG
   parity"). The comment turns out to be aspirational, not actual.

## §4 Boot-slide eyeball test (per qarl's follow-up dispatch)

Setup: `renderer/tests/fixtures/f0000000-0000-4000-8000-000000000023/
item.json` (Boot fixture, slide "15 · Boot" from the FREE YOUR SIGN
reel), rendered at `tick = 0.6 s` (mid-slide, mid-breathe-cycle of
the PANEL-0 OK badge).

Capture command: `scripts/parity/boot_sxs.py` (new this commit).

Artifacts (all in `qa/captures/`):
- `boot-canvas2d.png` — Canvas2D render (this commit, t=0.6 s)
- `boot-rust.png` — Rust render (pre-existing, from prior capture)
- `boot-sxs.png` — side-by-side, 4 px gray gutter
- `boot-diff.png` — per-pixel `abs(delta) * 8`, RGB

Metrics:
- **max_delta**: 250  (saturated — single channel goes 0→250)
- **mean_delta**: 5.665  (low overall — divergence is concentrated)
- **pct_over_10**: 4.42 %  (4 % of pixels visibly differ)
- **hottest row**: y=318, mean delta 36.48 across the row

### Where the delta concentrates

Per-band breakdown (mean delta across 108-row bands):

```
y=   0-108   mean=  4.15   BADGE region (motion=breathe)
y= 108-216   mean=  4.86   BADGE region
y= 216-324   mean= 11.26   LOG region (boot text, line 1-2)
y= 324-432   mean=  7.63   LOG region
y= 432-540   mean=  9.24   LOG region
y= 540-648   mean=  8.66   LOG region
y= 648-756   mean=  8.59   LOG region
y= 756-864   mean=  2.27   LOG region (after last line)
y= 864-972   mean=  0.00   empty
y= 972-1080  mean=  0.00   empty
```

Drift is **dominant in the boot-log text region**, modest in the
badge area, zero in empty bg.

### Pixel-class decomposition (canvas vs rust by brightness)

```
Pixels where canvas2d has text, rust has bg:  41,624
Pixels where rust     has text, canvas2d bg:  36,027
Pixels where both have text (text-on-text):   22,314

Both-text mean delta:           31.37   ← per-engine glyph AA
Canvas-only mean delta:        147.24   ← text vs bg = vertical shift
Rust-only mean delta:          134.83   ← text vs bg = vertical shift
```

The fact that more pixels are "text in one, bg in the other"
(77,651 mismatched) than "text in both" (22,314 aligned) is the
**signature of a vertical line offset**, not just kerning. Lines of
text are landing at different y-positions in the two renderers.

### Sample row 318 (verbatim):

```
Canvas2D y=318  px(500)= (5, 6, 8)        ← bg
                 px(960)= (5, 6, 8)        ← bg
                 px(1400)=(5, 6, 8)        ← bg
Rust     y=318  px(500)= (255, 180, 60)   ← TEXT (amber)
                 px(960)= (255, 180, 60)   ← TEXT
                 px(1400)=(5, 6, 8)        ← bg
```

Rust paints the second log line on y=318. Canvas2D paints it on a
DIFFERENT row. That's the line-height / baseline math diverging —
the same audit-P1 #7 issue, but visibly bigger than the audit
characterized it.

### Judgment (qarl asked this specifically)

**The divergence is NOT just kerning.** Three contributors visible
in the data:

1. **Vertical line positioning** (DOMINANT): the multi-line VT323
   block lands on different y-coordinates in Canvas2D vs Rust.
   Line-height math diverges — Pillow `textbbox` vs Canvas-native
   metrics vs Rust fontdue `line_metrics()`. Per-glyph kerning is
   a subset of this but not the headline contributor.
2. **Per-engine glyph rasterization** (SECONDARY): even on the
   pixels where BOTH paint text, mean delta is 31 — that's anti-
   aliasing curve differences in the same glyph rendered at the
   same position. Different rasterizer libraries produce different
   coverage values at glyph edges.
3. **Motion-modulated scale subtly different** (MINOR, badge only):
   the breathe scale at t=0.6 s is calculated identically (formula
   is canonical-pinned post-ed7162a etc.), but the resulting scaled-
   glyph rasterization differs because of (2) above.

NOT contributors:
- Background fill math (matches within 1-2 LSB on bg-only strips)
- Color math (text colors match where pixels align)
- Brightness/gamma post-pass (Canvas2D parity-harness uses identity
  opts, so 8ef2e7f + 07ad96b's encoding is NOT applied — kept the
  comparison apples-to-apples vs pre-gamma Rust goldens)
- Motion math itself (formula is pinned, the visible delta comes
  from downstream glyph AA)

## §5 Recommended next slices

In priority order:

1. **Fix line-height parity (audit P1 #7, now upgraded from P1 to
   P0)**. The audit said "Canvas2D + Rust both apply 1.1× multiplier"
   — that's a NECESSARY but not SUFFICIENT condition. The full
   line-height stack includes (a) the multiplier, (b) the font's
   intrinsic line metrics, (c) baseline offset, (d) glyph bbox
   inflation. Need to walk all four for VT323 (and probably for the
   other fonts that fail) and align.

2. **Investigate pattern shader divergence (rings, stripes,
   scanlines)**. The audit's "patterns are in lockstep" claim is
   wrong for these three. Likely cause: density-to-radius math
   differs at the sub-pixel level, or tile-alignment-to-screen-edge
   differs (Canvas2D paints from top-left, Rust shader from NDC
   center — anchor offset).

3. **Add the dispatch's strict `--max-delta` CLI flag** to run.py.
   Today thresholds are hardcoded in fixtures.json. A `--max-delta N`
   override lets QA tighten gates per-arc without editing the fixture
   file. Not blocking; ergonomic.

4. **Wire harness into CI** as a per-commit gate. Today nothing
   blocks a renderer-math regression from landing. The harness is
   the right gate; just need CI plumbing.

5. **Re-bless goldens after each fix lands** (per existing pattern
   in `fb3f6a3 renderer: re-bless goldens after layout fix
   (c56314f)`). Current goldens are c56314f-vintage; subsequent
   renderer changes (a49505c BT.709, ed7162a bounce abs(sin),
   etc.) may have shifted Rust output. Re-bless = capture fresh
   Rust output, commit.

## §6 Confidence (per surface, post-this-analysis)

| Surface | Original audit | Post-eyeball |
|---|---|---|
| Text vertical positioning | "P1 line-height, PIL drift only" | **P0 — Canvas2D vs Rust drift dominates** |
| Text per-glyph AA | "P2 per-engine kerning" | **P1 — visible at 31 LSB even on aligned text** |
| Pattern shader (rings/stripes/scanlines) | "Patterns in lockstep" | **P1 — pattern math drift, audit was aspirational** |
| Pattern shader (others) | "Patterns in lockstep" | **OK within tolerance** (dots/gradient/halftone pass bg-strip at <10 LSB) |
| Motion math | "Audit P0 closed by ed7162a etc." | OK — motion FORMULA correct; downstream AA is the visible delta |
| Background fills (no pattern) | "OK" | **OK within 1-2 LSB** |
| Brightness/gamma | "Canvas2D fix landed (8ef2e7f + 07ad96b)" | OK — but parity-harness uses identity opts so this isn't tested here yet |

## §7 What's NOT in this commit

Per dispatch out-of-scope:
- CI wire-up (next slice)
- Re-blessing goldens (would paper over the divergence; qarl said no)
- Fixing the divergences themselves (next dispatch arc)
- Pattern-shader fix (different slice)
- Line-height fix (different slice — this is the P0 surfaced above)

The mandatory-reply data goes back to QA + qarl via the Jimmy ping.

## §8 Subagent LGTM

Eyeball analysis on row 318 + the canvas-only/rust-only/both-text
pixel classes is the smoking gun for vertical line offset. The
bg-strip-only metric isolates non-text divergence cleanly. The
boot_sxs.py script reuses run.py's Playwright + http.server harness
pattern.

LGTM for the findings + the side-by-side capture path.
