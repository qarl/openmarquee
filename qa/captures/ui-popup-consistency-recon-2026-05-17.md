# UI popup/dropdown style consistency — recon (2026-05-17)

## Intent

Bug 9 recon. qarl morning: "in the UI - i see a lot of pop-up menus that
have custom font/layout. like for the transition selection. they are
nice. i see other pop-ups that are generic. i'd like to make all the
generic pop-ups match the nice style, please."

Goal: inventory every popup-shaped UI surface in `ui/src/`, identify
which pattern qarl considers "nice", classify each as styled / generic
/ mixed, and propose a refactor plan.

This is RECON ONLY. No code touched. Fix-dispatches follow if qarl
greenlights an approach.

## Method

Greppped `ui/src/` and `ui/styles.css` for popup-shaped patterns:
`<select`, `role="listbox"`, `aria-haspopup`, `popover`, `popup`,
`dropdown`, `*-picker`. Read each call site's HTML structure + the
CSS class hierarchy that styles it.

Mobile hamburger sheet nav (main.js:711-742 + styles.css:3652+) is
out of scope — it's a full-screen sheet, not a per-control dropdown
the way qarl's request frames the problem.

## Four distinct popup patterns surfaced

Before the inventory: there are **FOUR** distinct visual patterns
in the codebase, each implemented differently. Identifying which
one qarl thinks of as "nice" is the critical fork.

### Pattern A — chip-pill styled native `<select>`

**Reference site**: `.track-block-transition` (playlist-track.js:597,
styled at styles.css:619-648).

Wire: native HTML `<select>` with `appearance: none` and custom CSS.
Open dropdown state uses **browser-default** chrome (system font, OS
list rendering). Closed state is fully branded:

```css
.track-block-transition {
    font-size: 10.5px;
    font-family: var(--om-mono);
    background: var(--om-surface-2);
    color: var(--om-text-dim);
    border: 1px solid var(--om-line);
    border-radius: 999px;          /* full pill */
    padding: 1px 18px 1px 8px;
    text-transform: lowercase;
    letter-spacing: 0.04em;
    appearance: none;
    /* inline SVG chevron via background-image */
}
.track-block-transition:hover  { border-color: var(--om-accent); color: var(--om-text); }
.track-block-transition:focus-visible { outline: 2px solid var(--om-accent); outline-offset: 1px; }
```

Distinctive: pill chip (999px radius), monospace font, lowercase
letter-spacing, dim text → bright on hover/focus, custom SVG chevron.

### Pattern B — form-field styled native `<select>` (`.om-select`)

**Reference site**: every `.om-select` in editor.js / settings.js /
schedule.js (styled at styles.css:3050-3072).

Wire: native HTML `<select>` with `appearance: none` and DIFFERENT CSS.
Same browser-default open-state chrome as Pattern A.

```css
.om-select {
    appearance: none;
    background: var(--om-surface-2);
    color: var(--om-text);
    border: 1px solid var(--om-line);
    border-radius: 9px;            /* rounded rectangle */
    padding: 10px 12px 10px 12px;
    padding-right: 32px;
    font-family: var(--om-sans);
    font-size: inherit;
    width: 100%;
    /* inline SVG chevron via background-image */
}
.om-select:focus { border-color: var(--om-accent); background: var(--om-bg); }
```

Distinctive: rounded rectangle (9px radius), sans font, normal case,
full-width, form-field padding (10/12px). Visually consistent within
its own family (all 12+ sites match) but **different from Pattern A**.

### Pattern D — unstyled native `<select>`

**Reference site**: `.rule-playlist` (schedule.js:318) — the schedule
rule's "playlist for this rule" picker.

Wire: `<select class="rule-playlist"></select>`. No `.om-select`
class, no custom CSS in `styles.css` (`grep "rule-playlist"
ui/styles.css` returns zero hits). Browser-default chrome both
closed AND open. This is the only truly-unstyled `<select>` in
the codebase and the strongest candidate for "generic" in the
strictest reading of qarl's request.

### Pattern C — fully-styled custom listbox popover

**Reference sites**: font-picker (font-picker.js:73-159, CSS
styles.css:3692-3784) + color-picker popover (color-picker.js:213-285,
CSS styles.css:1182-1245).

Wire: a hidden native `<select>` for forms-value-tracking + a
visible button trigger + a custom `role="listbox"` popover element.
Both **closed and open states** are fully branded. The popover
floats absolutely-positioned with z-index, custom shadow, custom
scroll, custom item layout (grid of tiles for fonts, swatch row for
colors).

Distinctive: nothing falls back to browser-default chrome. Operator
sees the same visual language whether the picker is open or closed.

## Inventory of every popup site

| # | File:line | Class / Selector | Pattern | Classification |
|---|-----------|------------------|---------|----------------|
| 1 | playlist-track.js:597, styles.css:619 | `.track-block-transition` | A | **REFERENCE-A** |
| 2 | font-picker.js:73, styles.css:3692 | `.font-picker-popover` | C | **REFERENCE-C** |
| 3 | color-picker.js:213, styles.css:1182 | `.om-color-picker-popover` | C | **REFERENCE-C** |
| 4 | editor.js:162 | `.om-select.field-bg-slide` | B | G (generic relative to A) |
| 5 | editor.js:170 | `.om-select.field-bg-video` | B | G (generic relative to A) |
| 6 | editor.js:229 | `.om-select.field-blend` | B | G |
| 7 | editor.js:270 | `.om-select.field-auto-format` | B | G |
| 8 | editor.js:284 | `.om-select.field-font-family` (HIDDEN, driven by C) | C-driven | S (Pattern-C trigger renders) |
| 9 | editor.js:299 | `.om-select.field-motion` | B | G |
| 10 | schedule.js:38 | `.om-select.field-default-playlist` | B | G |
| 11 | settings.js:81 | `.om-select.field-output-mode` | B | G |
| 12 | settings.js:94 | `.om-select.field-display-rotation` | B | G |
| 13 | settings.js:115 | `.om-select.field-ws281x-pixel-order` | B | G |
| 14 | settings.js:171 | `.om-select.field-wifi-station-ssid-picker` | B | G |
| 15 | settings.js:263 | `.om-select.field-timezone` | B | G |
| 16 | (color picker triggers, scattered) | `.om-color-picker-more` etc. | C | S |
| 17 | schedule.js:318 | `.rule-playlist` (no other class) | D | **G (most generic — unstyled native)** |

**Tally**: 17 popup surfaces. 3 references (Pattern A + 2× Pattern C),
1 site that uses C indirectly (#8 font hidden-select), 12 generic-
relative-to-A sites (all Pattern B), 1 absolutely-unstyled site
(Pattern D, #17 `.rule-playlist`).

## The ambiguity — which pattern is "nice"?

qarl's wording: "like for the transition selection. they are nice."
The transition selection picker is unambiguously
`.track-block-transition` → **Pattern A** (chip-pill mono lowercase).

But the dispatch's first criterion was "(S) Already styled — matches
the transition picker's pattern. No work needed." Strict reading: all
12 om-selects (Pattern B) ARE styled — `appearance: none`, custom
chevron, custom border/background, custom focus. They are **not
generic** in the absolute sense; they have less custom-font-and-
chrome than Pattern A, and they use a different visual model (form-
field rectangle vs chip pill).

**Two interpretations of qarl's request:**

### Interpretation X — unify on Pattern A (chip-pill aesthetic)

All 12 Pattern-B sites should adopt Pattern A's chip-pill styling
(pill 999px, mono lowercase 10.5px, dim text → accent on hover).
The visual outcome: every dropdown across the UI matches the
transition picker's chip aesthetic.

**Visual trade-off**: chip pill works for inline-content-density
contexts (the playlist track row); a settings form full of pill chips
may feel less form-like and more chip-spam. Some controls
(`field-timezone` with hundreds of options; `field-default-playlist`
with N choices; `field-wifi-station-ssid-picker` dynamic) may want
the wider rectangle for readability.

### Interpretation Y — unify on Pattern C (fully-styled custom popover)

All Pattern-A AND Pattern-B sites adopt the custom-listbox-popover
shape (hidden `<select>` + button trigger + branded popover with
items rendered by JS).

**Visual outcome**: open state stops falling back to browser-default
chrome. The transition picker's open dropdown gets the same Pattern-C
treatment as font-picker. All controls feel like part of the same
design system both closed and open.

**Code trade-off**: this is the bigger refactor (~10 sites × ~150
LOC of trigger+popover wiring each, or a shared component).

## Recommended approach

I cannot decide between Interpretation X and Y without qarl input
(see Open Questions §). Whichever way qarl wants:

### If qarl picks X (chip-pill unification):

**Approach: CSS class extraction** (dispatch's option b). Lighter
diff, no JS changes.

- Extract a new class `.om-select.is-chip` (or rename
  `.track-block-transition` to a generic `.om-chip-select`) that
  carries Pattern A's styling.
- Add `is-chip` to each Pattern-B `<select>` we want chip-aesthetic.
- Selective per-context: chip for inline content, leave rectangle
  for forms? OR universal: every `<select>` becomes a chip?

Estimated LOC: ~30 CSS, ~12 className changes. 2-3 slices:
  - 9a: extract `.om-chip-select` from `.track-block-transition`.
  - 9b: apply to selected/all Pattern-B sites.
  - 9c: visual QA + adjust per-context (e.g. tighter chip in
    playlist row, slightly larger in settings).

### If qarl picks Y (fully-styled custom popover unification):

**Approach: shared `<Picker>` component** (dispatch's option a).
Heavier diff but better long-term.

- Extract a generic `setupListboxPopover(selectEl, options)` helper
  from `font-picker.js`. Generalized to operate on any
  `[label, value]` array, not just FONT_FAMILIES.
- Convert each Pattern-B `<select>` site: keep the hidden
  `.om-select` for form value-tracking, add a `*-trigger` button
  + `*-popover` div, wire via the shared helper.
- Convert `.track-block-transition` too (the transition picker
  also benefits from a styled open state).

Estimated LOC: ~200 LOC new shared helper, ~40 LOC HTML per call
site × 13 sites = ~520 LOC HTML, ~80 LOC CSS for the unified
popover. 4-5 slices:
  - 9a: extract `setupListboxPopover` from font-picker.
  - 9b: convert settings.js sites (5 controls).
  - 9c: convert editor.js sites (4 controls + transition picker).
  - 9d: convert schedule.js + remaining sites.
  - 9e: drop now-dead Pattern A + Pattern B CSS (small cleanup).

## Minor flaws spotted in the references (not for fixing in recon)

Per dispatch's "surface but don't fix" instruction:

- **font-picker popover**: clicking outside the popover closes it
  via `document.addEventListener("click", ...)` at font-picker.js:151,
  but there's no Escape-key handler. Operators who use keyboard
  navigation can open the popover with click but can't close with
  Esc.

- **track-block-transition focus-visible**: outline uses
  `outline-offset: 1px` (styles.css:647). On a high-DPI screen the
  1px offset can render fractional; consider 2px for consistency
  with `.om-input:focus` which doesn't use outline at all (uses
  border-color shift).

- **color-picker popover**: the `om-color-picker-popover-escape`
  button (color-picker.js:251) closes the popover but isn't
  documented in inline comments; a first-time reader has to read
  the JS to understand what that button is for.

None of these block the consistency fix.

## Open questions for qarl

**Q1. Which interpretation?**
(X) Chip-pill unification — everything looks like the transition
    picker. Closed states all match; open states stay browser-default.
(Y) Fully-styled-popover unification — everything looks like font-
    picker + color-picker. Both closed AND open states match;
    requires a bigger refactor.

**Q2. Selective vs universal?**
If X: every `<select>` becomes a pill chip, OR pill-chip is reserved
for inline-content contexts (playlist track, layer cards) and
rectangular `.om-select` stays for settings/form contexts?

If Y: convert the transition picker to a custom popover too (lose
the chip aesthetic in exchange for a styled open state), OR keep
the chip aesthetic AND add the styled open state (chip closed,
listbox open — most complex)?

**Q3. Mobile sheet nav** (out-of-scope per dispatch scope note):
should the hamburger sheet's nav links inherit the new pattern's
font/spacing? Currently the sheet uses its own styling. Leaving
alone unless qarl says otherwise.

## Closing summary

- **17 popup surfaces** inventoried in `ui/src/`.
- **4 distinct visual patterns**: A (chip-pill styled native select),
  B (form-field styled native select), C (fully-styled custom popover),
  D (unstyled native — only `.rule-playlist`).
- **3 reference sites**, **1 indirect-styled**, **12 generic relative
  to the transition picker** (but absolutely styled — just a different
  visual model), **1 absolutely-unstyled** (Pattern D).
- **The Pattern-D site (#17 `.rule-playlist`) is the unambiguous "fix
  this first" candidate** under any interpretation — even strictest-
  reading-of-the-request says "rule-playlist looks generic". Worth
  flagging as a no-cost-win to attach `.om-select` to it as part of
  whatever larger refactor lands.
- **Recommendation deferred** to qarl per the X-vs-Y ambiguity.
  Both are achievable; Y is the more thorough fix and the larger
  diff.

If qarl picks X: 2-3 slices, ~30 CSS LOC + ~12 className changes.
If qarl picks Y: 4-5 slices, ~800 LOC total across HTML/JS/CSS.

After qarl decides, dispatch the slice plan; this recon doc stays
as the historical record.
