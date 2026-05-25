---
date: 2026-05-25
type: scope
surface: ui/main
---

# main.js improvement targets — 2026-05-25 survey

Closes survey target #7 from 822d7d2 (the parent UI survey). Narrowed
to `ui/src/main.js` (1098 LOC, no sibling test file). 7 candidate
improvements sorted by leverage. Sub-types covered: correctness (1),
missing-tests (3), API hygiene (1), DRY (1), architectural (1).

## Methodology

Three-pass scan, ~40 min:

1. **Structure scan** — `grep -nE "^function|^export|^const|^let"` for
   top-level + inner-scope inventory. Read: main.js has 3 top-level
   functions (`resolvePanelDims`, `fetchResolvedPlaylist`, `boot`)
   plus ~10 inner functions all defined inside `boot()`. `boot()`
   spans lines 180-1094 — a 900-LOC single-function shell — which
   is THE obstacle to any unit-test coverage.
2. **Smell-pattern greps** — `innerHTML\s*=` (16 sites; spot-checked
   all 16; all are static literals or carry only constant or
   pre-escaped data — no XSS), `setInterval/setTimeout` (1 site,
   line 1001, properly page-lifetime), `addEventListener` (11
   sites — all on page-lifetime targets; covered separately in
   round-14 audit for live-panel; main.js is structurally similar),
   `Math.round/floor/ceil` (1 site, line 115, in
   `resolvePanelDims`).
3. **Function-call cross-referencing** — looked for places main.js
   does work that could live in a helper. Found two: the 5-block
   mount-uploader scaffolding (lines 580-633) and the
   window.confirm/window.alert pair duplicated between
   deletePlaylist (384-403) and the openmarquee:delete-slide
   handler (1055-1090).

## Targets (sorted by value — highest first)

### 1. `onSaveWithRefresh` runs 8 refreshes serially (perf)

- **Type:** correctness/perf — silent UX latency on every save
- **File:line:** `ui/src/main.js:361-379`
- **Sketch:** The higher-order wrapper does `await saveFn(...args)`
  then **9 sequential awaits** for refresh cascades (playlistTrack,
  editor.refreshBrowser, 4 uploader.refreshBrowser, slidesShell,
  refreshSidebarCounts, inlinePreviewHandle.refresh). These are
  independent operations — none depend on each other's result. With
  9 typical-100ms refreshes, the operator waits ~900ms after a save
  before the UI finishes refreshing instead of ~100ms.
- **Fix shape:**
  ```js
  await saveFn(...args);
  const saved = await saveFn(...args);
  await Promise.allSettled([
      playlistTrack?.refresh(),
      editor?.refreshBrowser?.(),
      imageUploader?.refreshBrowser?.(),
      videoUploader?.refreshBrowser?.(),
      streamUploader?.refreshBrowser?.(),
      webSlideEditor?.refreshBrowser?.(),
      slidesShell?.refreshCounts?.(),
      refreshSidebarCounts(),
      inlinePreviewHandle?.refresh?.(),
  ]);
  ```
  `Promise.allSettled` (not `Promise.all`) so one refresh failing
  doesn't blank the others.
- **Effort:** **S** (single block; rewrite + add one defensive test
  asserting all are kicked off without await-chaining).
- **Sister context:** Same family as `withProgressListener`'s
  sync-throw fix (cf31bbc) — correctness of an awaited path; not
  exactly the same bug shape but adjacent.
- **Risk:** low. `allSettled` semantics preserve the "all attempts
  fire, individual failures don't propagate" intent the existing
  serial code has (each method is optional-chained, so errors
  inside them already wouldn't propagate up the await chain). The
  switch makes them concurrent rather than serial.

### 2. `resolvePanelDims` — missing test for clamp + fallback contract

- **Type:** missing test coverage on a load-bearing surface
- **File:line:** `ui/src/main.js:98-142`
- **Sketch:** Pure-ish async function: fetches settings via
  `getSettings()`, coerces + clamps `brightness` to `[0, 100]`,
  defaults `outputMode`, `rotation`, falls back to
  `{ width: 128, height: 96, ... }` on any error. Five separate
  defensive shapes (`Number.isFinite`, `Math.max/min/round`,
  truthy-fallback for `outputMode`, `Number(... || 0)` for
  rotation, try/catch around the whole thing) — none locked by
  tests today.
- **Sketch test shape:**
  ```js
  it("clamps brightness to [0, 100] and rounds fractions", async () => {
      // mock getSettings to return brightness: 50.7 → assert 51
      // mock getSettings to return brightness: -10 → assert 0
      // mock getSettings to return brightness: 150 → assert 100
      // mock getSettings to return brightness: "abc" → assert 80 (default)
  });
  it("falls back to 128x96/hdmi/0/80 when getSettings throws", async () => {...});
  it("preserves rotation 90 in the returned shape", async () => {...});
  ```
- **Effort:** **S** (~4 tests in a new `main.test.js`; getSettings
  mock + dim-resolve mock).
- **Sister context:** `mountSettings` tickNow test (124d2b4) —
  same pattern of locking previously-undocumented invariants.
- **Risk:** low. Pure additive tests. Note: needs a new
  `main.test.js` file; first test for main.js. May surface jsdom
  setup quirks; rsync-to-APFS workaround likely needed.

### 3. `refreshFlockChrome` — missing test for the self+1 invariant

- **Type:** missing test coverage
- **File:line:** `ui/src/main.js:950-999`
- **Sketch:** "Treat last_seen_at within ~30s as 'online'" + the
  "self counts in the flock's headcount" semantics (per the long
  comment at 966-975) are non-trivial domain rules that are easy
  to silently break in a refactor. Surfaces: peerDots HTML,
  `[data-flock-count]` text, `[data-peer-pill-text]` text.
- **Sketch test shape:**
  ```js
  it("counts only freshly-seen peers as online and adds self", async () => {
      // mock listFlock to return [{last_seen_at: now}, {last_seen_at: now-60s}]
      // → expect [data-flock-count] === "2/3 online" (1 fresh peer + 1 self;
      //   2 peers + 1 self total)
  });
  it("treats stale peers as offline but still counts them in total", ...);
  it("keeps last-known UI when listFlock throws", ...);
  ```
- **Effort:** **S** (~3 tests; listFlock mock + jsdom-based DOM
  assertions). Same scaffolding as #2.
- **Risk:** low.

### 4. dual window.confirm+window.alert pattern (API hygiene)

- **Type:** API hygiene + testability concern
- **File:line:** `ui/src/main.js:386,390` (deletePlaylist) +
  `:1059,1064` (openmarquee:delete-slide handler) — same shape
  duplicated.
- **Sketch:** Both delete flows use:
  ```js
  if (!window.confirm(`Delete "${label}"? This can't be undone.`)) return;
  try { await deleteApi(); } catch (err) {
      window.alert(`Could not delete: ${err?.message || err}`);
      return;
  }
  ```
  Three problems:
  - `window.confirm/alert` BLOCK the JS thread — incompatible with
    the rest of the codebase's non-blocking status-pill UX.
  - JSDOM doesn't reliably handle blocking dialogs — these paths
    are effectively untestable today.
  - Operators on captive-portal mobile see system dialogs that
    look different from the rest of the UI.
- **Fix shape options:** Replace with a small custom-confirm helper
  (modal in-DOM that the rest of the codebase already implements
  via the `om-modal` class pattern). Or accept the inconsistency
  and just centralize the duplicated 9 LOC into a
  `confirmAndDelete(label, deleteApi, refreshFn)` helper.
- **Effort:** **S** for the centralize-only fix; **M** for the
  full replace-with-om-modal swap (touches CSS + 2 call sites +
  needs visual review).
- **Sister context:** `extractDetailMessage` operator-UX work
  (6f4ace0) + `populateWifiScan` status-pill pattern (d702f41) —
  both moved silent or jarring UX toward in-DOM status surfaces.
- **Risk:** medium for the full replace (operator-visible UX
  change; needs design review). Low for the centralize-only path.
- **Recommendation:** start with the centralize, defer the
  replace-with-modal to a separate dispatch with explicit qarl
  approval (operator-visible UX change).

### 5. 5-block mount-uploader scaffolding (DRY)

- **Type:** DRY
- **File:line:** `ui/src/main.js:580-633` — 5 nearly-identical
  blocks for editor / image / video / stream / web uploaders.
- **Sketch:** Each block does:
  ```js
  const slot = root.querySelector(".X-slot");
  slot.innerHTML = "";
  xUploader = mountX(slot, { width, height, fetchItems, onSave, ... });
  ```
  Variations:
  - editor has additional `rotation` + `onGenerateBackground` props
  - video has additional `outputMode` prop
  - stream + web omit `width`/`height` entirely (their mounts are
    pure-metadata editors with no canvas; they don't need the dims)
- **Sketch fix:**
  ```js
  const UPLOADERS = [
      { slot: ".editor-slot",      mount: mountEditor,         varName: "editor",         extra: { rotation, onGenerateBackground: onSaveWithRefresh(generateBackground) } },
      { slot: ".image-upload-slot", mount: mountImageUploader, varName: "imageUploader",   extra: {} },
      ...
  ];
  for (const u of UPLOADERS) { ... }
  ```
  Mid-effort because the variations need careful parameterization;
  could end up more lines than it saves if not careful.
- **Effort:** **M** (clear shape, modest LOC delta; needs care to
  preserve identifier-vs-prop naming for the outer-scope variable
  assignments).
- **Sister context:** `bg-picker.js` dedupe (7aeec9a), bg-picker
  shape exactly. The variation count is higher here (3 distinct
  shapes across the 5 blocks vs 2 shapes across 2 in bg-picker).
- **Risk:** medium. Each mount call has its own quirks; getting
  the table data right matters.

### 6. `refreshSectionTitle` + `refreshSidebarCounts` — tiny pure helpers, no tests

- **Type:** missing test coverage
- **File:line:** `ui/src/main.js:871-876` + `:343-355`
- **Sketch:** Both are tiny (~5 LOC), pure-DOM, easy to lock.
  `refreshSectionTitle` reads `window.location.hash`, looks up
  `SECTION_TITLES`, paints into `[data-section-title]`.
  `refreshSidebarCounts` fetches listContent + listPlaylists,
  paints counts into `[data-slide-count]` + `[data-playlist-count]`.
- **Sketch test shape:**
  ```js
  it("refreshSectionTitle paints the hash's title", () => {
      window.location.hash = "#/settings";
      refreshSectionTitle();
      expect(slot.textContent).toBe("Settings");
  });
  it("falls back to 'Slides' for unknown hashes", ...);
  ```
- **Effort:** **S** (~4 tests across the two functions). BUT
  blocked by the architectural target #7 below — both functions
  are inner-scope of boot() and aren't exportable today.
- **Sister context:** same as targets #2 + #3.
- **Risk:** low.
- **Dependency:** can't ship before target #7 (or a partial
  unwinding of it) because the functions aren't accessible from
  test code today.

### 7. `boot()` is 900 LOC — architectural obstacle to all unit testing

- **Type:** architectural
- **File:line:** `ui/src/main.js:180-1094`
- **Sketch:** `boot()` is one async function spanning lines
  180-1094 (~915 LOC). Every inner helper (`refreshSidebarCounts`,
  `surfacePlaylistError`, `surfaceSlideTypeError`,
  `createNewPlaylist`, `mountDimensionedPanels`,
  `refreshBrandSignName`, `paintIcon`, `refreshSectionTitle`,
  `refreshFlockChrome`, plus dozens of closures) is inside the
  function scope, captured-but-not-exported. None can be unit
  tested without first extracting them.
- **Sketch fix (vertical slices):**
  1. **Slice 1** — extract `refreshSectionTitle` + the
     `SECTION_TITLES` constant to module scope (~6 LOC moved).
     Lockable via test #6 above.
  2. **Slice 2** — extract `refreshSidebarCounts` + `refreshFlockChrome`
     to module scope or to a new `chrome.js` helper file. Tests
     for both ride this slice.
  3. **Slice 3** — extract the 5-uploader mount scaffolding (target
     #5) — naturally moves to a `mountSlideEditors.js` helper.
  4. **Slice 4** — extract the openmarquee:edit-slide + delete-slide
     handlers (already partially abstracted via
     `slide-event-handlers.js`'s `runDeleteCascade` shipped
     earlier this session; finish the abstraction).
  5. **Slice 5** — boot() shrinks to a thin orchestrator that
     calls the slice-1-to-4 helpers in order. Becomes testable
     via "given mock helpers, assert boot calls them in the right
     order with the right args."
- **Effort:** **L** total; each slice is **S** to **M**.
- **Sister context:** mountSettings is one big function but with
  smaller scope + a returned handle (124d2b4 added destroy()) —
  similar shape but smaller magnitude. boot() is the
  whole-page-shell equivalent.
- **Risk:** medium per slice (need to carefully preserve
  closure-captured state — many inner functions reference outer-
  scope `currentPlaylistId`, `playlistDraft`, `editor`,
  `imageUploader`, etc. Move-to-module-scope would break that;
  these need to become parameters or live in a shared state
  object).
- **Recommendation:** start with slice 1 (smallest, lowest risk,
  unblocks target #6's tests). Defer slices 2-5 until QA priorities
  shift to architecture work — this is genuinely large.

## Items intentionally not audited (skip-list rationale)

- **`runDeleteCascade`** — shipped this session in earlier work; out
  of scope per dispatch.
- **The 11 `addEventListener` sites** — main.js's listeners are
  predominantly page-lifetime (window.hashchange,
  document.openmarquee:*, etc.) — same shape live-panel's audit
  (cf625e7) classified as P. The setInterval at line 1001 also
  intentionally page-lifetime per its visibility-aware design.
  No leak shape worth surfacing.
- **The 16 `innerHTML\s*=` sites** — spot-checked all 16; ~12 are
  static literal templates, the other 4 interpolate only bounded
  or pre-escaped data. `dots.innerHTML = ...peerDots...` at line
  989 interpolates `<i class="${fresh ? "" : "off"}"></i>` — class
  name is bounded, no user data, XSS-safe.
- **`paintIcon` at line 824** — 12 LOC innerHTML-stamp that
  delegates to `fn(opts)`. Currently a 4-line wrapper; not worth
  re-engineering.
- **`fetchResolvedPlaylist` at line 156** — pure async; testable but
  touches the module-level `playlistDraft` state which would need
  threading through tests. Lower leverage than #2 and #3 which
  are cleaner test surfaces.

## Recommended ordering

1. **Target #1** (onSaveWithRefresh allSettled) — small, real perf
   win on every save, regression-lock test is straightforward.
2. **Target #7 slice 1** (extract refreshSectionTitle + SECTION_TITLES
   to module scope) — unblocks target #6's tests and is the smallest
   architectural-decomposition step.
3. **Target #6 tests** (now possible after #7 slice 1).
4. **Target #2** (resolvePanelDims tests) — independent of #7;
   already at module scope.
5. **Target #3** (refreshFlockChrome tests) — needs #7 slice 2
   first OR an export carveout.
6. **Target #4 centralize-only** (delete confirm/alert dedupe) —
   small win; the full modal swap should wait for explicit
   operator-UX approval.
7. **Target #5** (5-uploader DRY) — medium; falls naturally out
   of target #7 slice 3.
8. Target #7 slices 2-5 — large architectural work; explicit qarl
   greenlight recommended before starting.
