# Cross-renderer parity test harness — design

**Status:** design draft 2026-05-12. Author: claude (openmarquee-code). For qarl review before commit 2 (implementation).

## Problem

The browser preview renderer (used in slide-editor + playlist-track panels) and the Rust renderer (Pi HDMI scanout) can drift. qarl saw visible divergence on the demo today. We need automated tests that capture both outputs and surface diffs at PR-time, not at "operator notices on glass."

## Two renderer surfaces

### Browser (preview)

- Entry: `drawCanvas(canvas, state, opts)` in `ui/src/rasterize.js:196`. Paints background → iterates layers → wraps each `paintLayer` call with motion via `paintLayerWithMotion` (`ui/src/canvas-motion.js:82`).
- Background patterns: `paintPatternOnCanvas` (`ui/src/bg-system.js:211`), 12 procedural patterns via Canvas2D gradient/repeating-linear-gradient/radial-gradient primitives.
- Text: Canvas2D `ctx.fillText` with `ctx.font = "${weight} ${px}px ${family}"`. Bundled TTFs loaded via `@font-face` in `ui/styles.css:10-32`; same Anton/Oswald/etc. TTF files the Rust path uses via FontCatalog.
- Off-screen capture already exists: `rasterizeAtTarget(state) → canvasToBase64()` in `ui/src/rasterize.js:354`. Creates 4K canvas, paints once, returns PNG-as-base64. Used today for thumbnail save.
- Motion tick: rAF loop (`ui/src/editor.js:493-541`) computes `elapsed_s = (now - motionT0)/1000` and passes to `drawCanvas`. **No tick-pin support today.** The caller can pass `{ elapsed_s: 1.75 }` directly to bypass the loop — that's our injection point.

### Rust (Pi scanout)

- Entry: `hdmi::capture_slide_to_png(card, &slide, fonts, content_root, png_path, tick_override)` (`renderer/src/hdmi.rs:3046`).
- Already tick-pinnable via `--capture-slide-at-tick <t>` (Batch 17.2).
- Text rasterizer: `fontdue` on the same TTF bytes.
- Existing harness: `scripts/render_tests.sh` walks fixtures → captures on Pi → diffs against `renderer/tests/golden/<NAME>.png`.

## Capture mechanism

**Decision: Playwright (real Chromium) for the browser side.**

Considered alternatives:
- `node-canvas` (Cairo-backed): different rasterizer than the browser → can't parity-test what operators see.
- jsdom with canvas stubs: same problem; not real Canvas2D.
- `puppeteer`: similar to Playwright; the project already has Playwright config (`ui/playwright.config.js` from Batch 20.5 e2e).

Playwright gives us:
- Real Chromium font rasterization (matches what operators see on macOS/Linux desktops).
- DOM access to call `rasterizeAtTarget(state)` from the page context via `page.evaluate(...)`.
- Element/canvas screenshot fallback if the off-screen path doesn't work for some fixture.

Browser capture flow:
1. Playwright launches Chromium (headless), navigates to a parity-test HTML harness page (new, lightweight: just imports `rasterize.js` + `auto-text-overlay.js` + relevant deps).
2. Test JS reads fixture's `item.json` from disk → hydrates a `state` object matching what `loadForEdit` produces.
3. Calls `window.__parityCapture(state, tickSeconds)` (new helper exported on `window` for test-mode use).
4. Helper calls `drawCanvas(canvas, state, { elapsed_s: tickSeconds })` against an off-screen 1920×1080 canvas, returns `canvas.toDataURL('image/png')`.
5. Test extracts the base64 PNG → writes to disk.

Rust capture stays unchanged — drive `render_tests.sh` against the same fixture UUIDs.

## Diff strategy + threshold

**Decision: SSIM (structural similarity) as primary gate; max-per-pixel-delta as secondary gate; mean-delta + %-pixels-over-threshold as informational columns.**

Rationale:
- Pixel-exact is unrealistic. Canvas2D's font rasterizer (CoreText/DirectWrite/FreeType depending on browser host OS) differs fundamentally from fontdue. Even with the same TTF, glyph edges have different alpha values.
- SSIM > 0.95 catches structural drift (a layer in the wrong place, a missing element, a wrong color) while tolerating sub-pixel AA differences.
- Max-per-pixel-delta < 50 (out of 255) catches localized severe drift even when SSIM scores high.
- **Informational columns** (per subagent review nit): mean-per-pixel-delta and `% pixels with delta > 10` reported alongside SSIM. SSIM at 1920×1080 can be misleadingly high (>0.97) even when a whole text layer is missing if the rest of the frame matches. Informational columns surface "lots of small drift everywhere" that SSIM smooths over.

Per-fixture configurable gating thresholds — start lax (SSIM 0.95, max-delta 50) and tighten as drift is fixed. Informational columns are always reported regardless of gating.

Implementation: `pip install scikit-image` for SSIM; pillow already in deps for image I/O.

## Fixture set

**First wave: 6 fixtures, covering the top three drift hypotheses from the survey.**

| Fixture | UUID | Purpose | Drift surface |
|---|---|---|---|
| `parity_text_static` | f0000000-...-000000000001 (p2g_overlay_route) | Text-only static slide | Canvas2D vs fontdue text AA |
| `parity_bg_pattern` | f0000000-...-000000000003 (bg_pattern_dots) | Pattern bg + text | bg-system.js (Canvas2D) vs Rust pattern shader parity |
| `parity_motion_ticker` | 2c858968-...-85257de50bcd (FYS chant_wall) | Ticker motion @ tick=1.75 | Motion-phase math + frequency tables |
| `parity_blend_overlay` | f0000000-...-000000000001 (overlay_route) | Multi-layer overlay blend | Canvas globalCompositeOperation vs GLES2 blend |
| `parity_image_slide` | f0000000-...-000000000008 (B.18.1) | Image asset blit | Bilinear sampling differences |
| `parity_transition_mid` | 3964c302→2c858968, fade@t=0.5 | Transition midpoint | Inline-preview transition path (`playlist-track` cross-fade) vs Rust SP-tier composite shader |

Reuses existing renderer/tests/fixtures/<UUID>/item.json snapshots + the transition_mid_fade golden from Batch 17.1. No new fixture authoring.

**Video deferred**: VideoSlide capture (B.18.1) covers the thumbnail/first-frame paint only; the actual decode-pipeline parity (browser HTML5 video vs Pi ffmpeg) is a separate concern best handled in a dedicated video-parity dispatch. Documented gap, not in this commit.

## Harness architecture

Single Python driver, mirroring `scripts/render_tests.sh`'s shape:

```
scripts/parity_tests.sh   # entry point, like render_tests.sh
  ├─ scripts/parity/      # implementation
  │   ├─ capture_browser.py   # Playwright orchestrator
  │   ├─ capture_rust.py      # wraps render_tests.sh per-fixture
  │   ├─ diff.py              # SSIM + max-delta computation
  │   └─ fixtures.json        # fixture name → (UUID, tick, thresholds)
  └─ ui/parity-harness.html   # the page Playwright loads
```

Test invocation:
- `bash scripts/parity_tests.sh` → captures both, diffs, reports per-fixture SSIM + max-delta + mean-delta + %-pixels-over-10 + PASS/FAIL.
- `bash scripts/parity_tests.sh --bless` → saves browser PNGs as the new baseline (for when a known-intentional renderer change shifts the baseline).

Skip-gracefully behavior:
- Pi unreachable → skip Rust-side capture (the dispatch's "Pi unreachable" case).
- Playwright not installed → skip browser-side capture.
- Both skip → harness reports "no captures available" rather than failing.

## Implementation plan (commit 2)

1. **Add `window.__parityCapture` test-mode helper** in `ui/src/rasterize.js` (5 LOC, gated on a `data-parity-mode` attribute on `<body>` so it's not in the production bundle path).
2. **Build `ui/parity-harness.html`** (~50 LOC: minimal HTML + JS that imports rasterize.js + loads fixture from `?fixture=<UUID>&tick=<t>`).
3. **Build `scripts/parity/capture_browser.py`** (~120 LOC: Playwright orchestrator that launches Chromium, hits the harness URL, extracts the base64 PNG, writes to `renderer/tests/parity/captures/<NAME>.browser.png`).
4. **Build `scripts/parity/capture_rust.py`** (~80 LOC: wraps the existing `render_tests.sh` logic per-fixture, but writes to `parity/captures/<NAME>.rust.png` rather than `golden/`).
5. **Build `scripts/parity/diff.py`** (~60 LOC: scikit-image SSIM + numpy max-delta; outputs JSON metrics).
6. **Build `scripts/parity_tests.sh`** (~80 LOC: orchestrates the above; same shape as `render_tests.sh`).
7. **Land the 6 fixtures in `scripts/parity/fixtures.json`** with thresholds.
8. **Document any drift the first run catches** in the commit message — this is the dispatch's diagnostic deliverable.

Estimated scope: ~400 LOC + 6 fixture entries. ~3-4h including the documentation pass and bringing up Playwright in this code path for the first time. (The transition fixture in particular requires prior+next slide state hydration on the browser side, which `parity-harness.html` needs to support — not just a single-slide page.)

## Known risks

1. **Playwright dependency weight**: ~50MB Chromium download on first install. Mitigate via `skipOnMissing` so the harness no-ops when Playwright isn't on the CI runner.
2. **Browser-host font rendering varies**: Mac Chromium vs Linux Chromium vs Windows Chromium have different font hinting. CI may produce different goldens than the developer's Mac. Mitigation: bless on a single canonical host (the Mac dev box, like the Pi-side renderer goldens). Document in `fixtures.json`'s provenance section. Long term: a Docker-based renderer for the browser side if cross-host drift becomes a problem.
3. **Tick-pinning the rAF loop**: my injection-point design bypasses the rAF loop entirely (call `drawCanvas` once with the override). Risk: if there's hidden state that only `maybeStartMotionLoop` initializes (e.g., a layer-key cache for motion seeds), the static `drawCanvas` call may miss it. **Mitigation in commit 2**: deliberately capture twice on the motion fixture during the first bless — once cold (single `drawCanvas` call), once after letting the rAF loop run a few ticks then advancing to the target tick. If the two PNGs match: lock in the cold path. If they don't: we've found a real bug in the static path that operators may also hit on preview-tab-switch (worth filing separately).
4. **Image slide bilinear sampling**: Canvas2D uses default `imageSmoothingQuality: 'low'` (browser-default). The Rust GLES2 path uses GL_LINEAR. Different filters could produce visibly different mids on the image_slide fixture. Document any drift; may need to add `imageSmoothingQuality: 'high'` to the parity harness.
5. **Threshold over-tightening**: starting with SSIM > 0.95 / max-delta < 50 keeps the first commit landable even if real drift exists. The drift is the deliverable; tighter thresholds become follow-up dispatches as each drift case is fixed.

## What this commit does NOT do

- Build the harness. That's commit 2.
- Run the captures. That's commit 2.
- Surface the actual drift findings. That's commit 2's commit message (and may produce a separate fix-list dispatch).
- Add Playwright to the build dependencies. That's commit 2 (or stays optional / dev-only).

## What the harness does NOT cover (scope honesty)

- **HVS / multi-plane DRM atomic compositor path** (Phase 6/6.5, see `project_phase6_hdmi_landed` memory): the Rust capture goes through `capture_slide_to_png`, which is the *single-plane EGL/GLES2* path. Live scanout on Pi often goes through the multi-plane HVS compositor (per-frame plane property changes for motion). Browser-vs-capture parity therefore does NOT prove browser-vs-what-operators-see-on-glass parity. Acceptable for now (HVS parity is a separate hardware-only test surface), but document this in the harness output so a "parity GREEN" verdict isn't misread as "no Pi drift."
- **Video decode pipeline** (HTML5 `<video>` element vs Pi's ffmpeg+swscale): noted in fixture table; deferred to a dedicated video-parity dispatch.
- **Cross-browser variance**: the harness captures via headless Chromium (Playwright default). Firefox/Safari font rasterizers differ; we don't claim to catch their drift.
- **Cross-host variance for the canonical baseline**: bless on a single canonical Mac host. CI bless on a Linux runner would produce different goldens. Documented in `fixtures.json`'s provenance section in commit 2.
