---
date: 2026-05-25
type: scope
surface: ui
---

# UI code-quality improvement targets — 2026-05-25 survey

QA-pivot deliverable per round-survey dispatch: 7 candidate functions
across `ui/src/` for the next several code-quality loop rounds, sorted
by value. Skip-list excludes the 18 surfaces already shipped this
session (drawHub75 / withProgressListener / extractDetailMessage /
setupFontPicker / auto-text-overlay / _coerce_to_schedule / Schedule
extras / MIGRATION_HANDLED_TOP_LEVEL / mountColorPicker / bg-picker
/ scheduled_fetch_items / LruMap insert / summarize_samples /
list_in_playlist_order / blinds parity / error-boundary audit /
bundle-size CI gate / global error boundary).

## Methodology

Three-pass survey on `ui/src/`:

1. **Size + test-coverage scan** — `wc -l` sorted, then walked
   sibling `*.test.js` presence. Surfaced `main.js` (1098 LOC, no
   tests) as the biggest gap.
2. **Smell-pattern greps** — `innerHTML\s*=` (XSS / DOM-mutation
   loops), `setInterval/setTimeout` vs `clear*` (timer-leak
   shape), `addEventListener` vs `removeEventListener` ratio
   (listener-leak shape), `Math\.(round|floor|ceil)` (off-by-one
   shape).
3. **Near-duplicate scan** — function-name patterns:
   `populate/render/show/update` across files for DRY candidates.

Time-boxed at 60 min total. Targets ranked by leverage (correctness
beats DRY beats coverage beats perf).

## Targets (sorted by value — highest first)

### 1. `tickNow` setInterval leak in `settings.js:992-1016`

- **Type:** **correctness bug** (timer leak + dead defensive check)
- **File:line:** `ui/src/settings.js:1016`
- **Sketch:** `mountSettings` returns `{ refresh }` but never tears
  down its `setInterval(tickNow, 1000)`. Once mounted, the timer
  ticks every second forever and holds a closure reference to the
  entire `mountSettings` scope. The defensive `if (!nowValueEl)
  return;` at line 993 is **unreachable** because `nowValueEl` is
  captured at mount time and never reassigned — even if the element
  is later detached from the DOM (e.g. on section unmount + remount),
  the closure still holds the orphan node and `textContent = ...`
  silently no-ops on the orphan.
- **Realistic impact:** Slow memory growth + permanent ~1s wakeup
  for as long as the captive-portal tab is open. Probably bounded
  in practice because `main.js:725` calls `mountSettings` once at
  boot (no per-navigation remount), but the bug is real and gets
  worse the moment that contract changes.
- **Fix shape:** Track the timer id and return a `stop()` (or just
  `destroy()`) on the handle; or clear on the next `refresh()`
  cycle. Add a test that mounts → unmounts and asserts the timer
  isn't ticking after stop. Pattern reference:
  `feedback_test_commits_need_runtime_verify`-grade test in the
  jsdom env (rsync-to-APFS workaround).
- **Positive-example contrast in the same file:** `settings.js:847`
  has `setInterval(pollTailscaleStatus, 3000)` that the codebase
  DOES correctly tear down via `if (tsPollTimer) clearInterval(
  tsPollTimer);` at line 779 — same module, same pattern, but the
  tickNow one missed the discipline. Surface the contrast in the
  commit message so the fix doesn't look like an invented
  discipline.
- **Effort:** **S** (single function + one test + a teardown call
  site update in `main.js` if mountSettings's return shape grows).
- **Sister context:** No direct equivalent shipped this session, but
  the same "function returns a handle but no teardown" surface
  shape was the bug in `withProgressListener` (`cf31bbc`).
- **Risk:** low. Pure additive (handle shape grows from `{refresh}`
  to `{refresh, destroy}`); existing callers ignore the new key.

### 2. `mountSettings` has no test coverage for time-tick contract

- **Type:** **missing test coverage** on a load-bearing surface
- **File:line:** `ui/src/settings.test.js` (existing 602 LOC) has
  zero coverage of `tickNow` / `setInterval` / `nowValueEl`
  rendering. Even if Target #1 is fixed first, the surface deserves
  a regression-lock.
- **Sketch:** With `vi.useFakeTimers()`, mount settings, advance
  500ms (no update yet), advance 1000ms (timestamp formatted),
  assert `nowValueEl.textContent` matches `Intl.DateTimeFormat`
  shape. Bonus assertion: after destroy/unmount, `vi.advanceTimers`
  doesn't fire any further updates (regression-locks Target #1's
  fix).
- **Effort:** **S** (one describe block, 2-3 tests).
- **Sister context:** `setupFontPicker`'s tile-click test (e636eee)
  set the precedent for "the only guardrail for a load-bearing
  behavior".
- **Risk:** low (additive tests only).
- **Bundle suggestion:** ship Target #1 + Target #2 in one commit
  (the fix + the regression-lock together).

### 3. `renderTrackBlock` + `renderPalletTile` near-duplicate in `playlist-track.js:611-682`

- **Type:** **DRY**
- **File:line:** `ui/src/playlist-track.js:611-657` (renderTrackBlock)
  + `:660-682` (renderPalletTile) — both build a `<li>` with
  `<img class="*-thumb">` + `lockedBadge` + `attachAutoTextOverlay`
  on a `*-thumb-wrap` container, both interpolate `safeName` via
  `escapeHtml`, both consume `cacheBust` for the asset URL.
- **Sketch:** Differences are real (track-block has duration + grip
  + transition pulldown; pallet-tile has hover edit/delete buttons),
  but the **shared scaffolding** (thumb-wrap with locked-badge +
  attachAutoTextOverlay invocation + mediaSrc URL composition) is
  20+ lines repeated verbatim. Extract `renderThumbWrap(item, {locked,
  cacheBust})` → returns an HTMLElement; each renderer builds its
  surrounding chrome around it. Public API of both renderers stays
  unchanged.
- **Effort:** **M** (shared helper + 2 call-site rewrites + verify
  existing playlist-track tests still pass).
- **Sister context:** Direct sibling of `bg-picker.js` dedupe
  (7aeec9a) and `auto-text-overlay`'s `renderOne` extraction
  (8b682b5).
- **Risk:** low-medium. Both renderers are exercised by existing
  tests in `playlist-track.test.js` (704 LOC). The helper's exact
  return shape matters for the surrounding template-literal
  composition; a syntax-edge could silently break the rendered
  thumbnail.

### 4. `fillPlaylistOptions` in `schedule.js:338` vs the new bg-picker helper

- **Type:** **API hygiene / potential DRY** (mild)
- **File:line:** `ui/src/schedule.js:338-356`
- **Sketch:** Fourth select-populator in the codebase (after the two
  in bg-picker.js and `populateAutoFormatOptions` in `editor.js`).
  Has a feature the bg-picker helper doesn't: when `currentValue`
  doesn't match any choice's id, it appends a synthetic
  `<option value="${currentValue}">${id.slice(0,8)}… (missing)</option>`
  so a stale UUID round-trips. Could either: (a) extend
  bg-picker's `_populateOptions` to accept an optional
  `missingValuePlaceholder` so all 3 can share, or (b) leave alone
  since the missing-value feature is schedule-domain-specific.
- **Effort:** **S** if (b) — skip with rationale doc. **M** if (a) —
  needs the bg-picker helper to grow + all 3 call sites to update.
- **Sister context:** `bg-picker.js` dedupe (7aeec9a). My
  recommendation: skip with a comment unless QA wants the full
  3-way generalization.
- **Risk:** medium-low (extending a working helper). The
  schedule-side `(missing)` behavior is a regression-trap if the
  refactor drops it.

### 5. `live-panel.js` listener-lifecycle audit

- **Type:** **correctness audit** (potential leak)
- **File:line:** `ui/src/live-panel.js` (1429 LOC). 12
  `addEventListener` calls; 3 `removeEventListener`. Three of the
  bound listeners look page-lifetime (`window.pagehide`,
  `document.openmarquee:settings-updated`, etc.) and are
  appropriately not cleaned. But the per-session ones
  (`pc.addEventListener("connectionstatechange", ...)` at line 793,
  `pc.addEventListener("icegatheringstatechange", ...)` at line
  673, the button listeners at 1275-1323) need explicit audit:
  are any per-call listeners surviving a session teardown +
  restart?
- **Sketch:** Spend ~30 min reading `teardownPC()` / `failTo()` /
  the close-out paths, mapping which listeners get bound at session
  start vs page boot. Surface any per-session listener that
  outlives its PC. Likely finding: 0-2 real leaks (the
  `state.pc !== pc` guard at line 794 suggests the team is aware of
  the pattern, but that's only one of the 12).
- **Effort:** **M** to audit; **S** to fix any specific leak found.
- **Sister context:** `withProgressListener` sync-throw leak
  (cf31bbc) — different mechanism (no-finally vs no-removeListener)
  but same correctness class.
- **Risk:** medium for the audit (read-only); low for any fix
  shipped.

### 6. `populateWifiScan` silent-fail UX in `settings.js:971-974`

- **Type:** **operator UX** (silent failure mode)
- **File:line:** `ui/src/settings.js:971-974`
- **Sketch:** `populateWifiScan`'s catch swallows the error with
  `console.debug("[settings] wifi-scan failed:", err)` and falls
  through to the "(type manually)" placeholder. Operator sees the
  fallback but doesn't know **why** — could be transient network,
  could be backend permission issue, could be the WiFi card down.
  Surface to a `statusEl` (the same pattern bg-picker uses on
  fetch failure, per 7aeec9a).
- **Sketch fix:**
  ```js
  } catch (err) {
      console.debug("[settings] wifi-scan failed:", err);
      // operator-visible breadcrumb so the (type manually)
      // fallback doesn't look like a missing feature
      const status = container.querySelector(".settings-wifi-status");
      if (status) status.textContent = `WiFi scan unavailable: ${err.message}`;
  }
  ```
- **Effort:** **S** (one catch handler + maybe one tiny test).
- **Sister context:** `extractDetailMessage` improvement
  (`6f4ace0`) on operator-visible error copy.
- **Risk:** low.

### 7. `main.js` (1098 LOC) — zero test coverage

- **Type:** **missing test coverage** for a critical app-startup
  surface
- **File:line:** `ui/src/main.js` (1098 LOC, no `main.test.js`)
- **Sketch:** Hash routing + section-mounting + event-bus wiring
  (`openmarquee:edit-slide`, `openmarquee:delete-slide`) +
  `refreshFlockChrome()` + auth-status probe all live here.
  Untested surface — and the `setInterval` at line 1001 has the
  same pattern as Target #1 (no teardown; but main is genuinely
  page-lifetime so it's defensible). The `dots.innerHTML =
  '<i></i>${peerDots}'` at line 989 is **safe** (peerDots comes
  from a bounded class-name template, no user-controlled string),
  but a regression toward unsafe interpolation would be silent.
- **Sketch:** Start with the smallest tractable test: hash
  routing. `vi.useFakeTimers()` + `window.location.hash = "#/settings"`
  + assert the right `mount*` function was called. Build out from
  there incrementally.
- **Effort:** **L** (genuinely large surface; recommend ship in
  vertical slices — first slice: hash routing + section mount
  dispatch; later slices: event bus / peer poll / first-run
  redirect).
- **Sister context:** No direct, but `mountColorPicker`'s
  e636eee/a055cd7 covered the precedent for "test the smallest
  load-bearing slice first" pattern.
- **Risk:** medium (test scaffolding for an app shell is finicky;
  jsdom env quirks + the rsync workaround apply).

## Items ruled out

- **`renderComplexPicker` in `color-picker.js:223`** — already in
  good shape per the QA round-9 sub-agent review; popover-DOM
  built detached + cleaned up correctly.
- **`auto-format.js` Tomohiko Sakamoto math** — `Math.floor` chain
  is a well-known formula; tests at `auto-format.test.js` cover the
  edge years. Not a real off-by-one risk despite matching the
  smell-pattern grep.
- **`bg-system.js` Math.round on density-lerp values** — visual
  parameters whose rounding boundaries are aesthetic, not
  correctness-critical. Bounded range, bounded inputs.
- **`mountFlock` discoverList innerHTML at `flock.js:573`** — pre-
  audit'd via security-scope; uses `escapeHtml` for the hostname
  per the Bundle C item 4 sanitization. Clean.
- **`renderRule` in `schedule.js:277`** — has tests in
  `schedule.test.js` covering its render contract.
- **`mountLive` in `live-panel.js`** — DON'T touch the live-stream
  state machine end-to-end without explicit QA dispatch; that's a
  Pi-debug surface where qarl explicitly halted Phase 8 work per
  `project_phase8_in_flight`. The listener-lifecycle audit (Target
  #5) is the safe subset.
- **`mountEditor` / `editor.js` (1858 LOC)** — too large to survey
  in 60min. Worth its own dedicated audit round.
- **`auto-save.js`** — its `setTimeout` debounce is properly
  cleared via `clearTimeout(pending)` (verified by grep at
  `auto-save.js:setTimeout|clearTimeout`); not the leak shape.

## Recommended ordering

1. Targets #1 + #2 bundled (correctness fix + regression-lock; the
   highest-leverage single commit in the list)
2. Target #5 (live-panel audit — read-only investigation; surfaces
   may or may not produce a fix-commit)
3. Target #6 (small UX win; no risk)
4. Target #3 (medium-effort DRY; well-scoped)
5. Target #7 (vertical slice 1: hash routing) — biggest surface,
   defer until smaller wins are done
6. Target #4 (skip or generalize after #3 ships — depends on
   3-way-generalization appetite)
