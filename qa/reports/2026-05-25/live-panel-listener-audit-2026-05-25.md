---
date: 2026-05-25
type: audit
surface: ui/live-panel
---

# live-panel.js listener-lifecycle audit — 2026-05-25

Closes survey target #5 (822d7d2). Read-only audit per round-14
dispatch — no code changes.

## Method

`grep -nE "addEventListener\(|removeEventListener\(" ui/src/live-panel.js`
enumerated the 12 listener-add sites (plus 3 explicit removes). For
each add, traced the binding context (module scope vs per-session
inner function) and matched against the four-way classification
(Page-lifetime / per-Session-cleaned / Leaks / Guarded). Cross-
verified by reading the destroy() body (line 1406-1427) +
teardownPC() body (line 746+) for cleanup coverage.

Time spent: ~25 min of focused reading. Files involved: just
`ui/src/live-panel.js`.

## Inventory

| Line | Target            | Event                     | Class | Cleanup site                                                                                | Risk |
|------|-------------------|---------------------------|-------|----------------------------------------------------------------------------------------------|------|
| 404  | document          | `openmarquee:settings-updated` | P     | `destroy()` line 1413: `document.removeEventListener("openmarquee:settings-updated", onSettingsUpdated)` | low  |
| 673  | pc (RTCPeerConnection) | `icegatheringstatechange` | S     | Self-removes at line 669 inside `onChange`: removes itself when state reaches "complete"      | low  |
| 793  | pc (RTCPeerConnection) | `connectionstatechange`   | G     | No explicit remove; `state.pc !== pc` guard at line 794; documented at 785-791 (`teardownPC()` nulls `state.pc` BEFORE `close()` so the `'closed'` event from teardown short-circuits in the listener); pc + listener cycle GC's once pc reference drops | low  |
| 1275 | goLiveBtn          | `click`                   | P     | `container.innerHTML = ""` in `destroy()` line 1426 (DOM node removal severs listener)        | low  |
| 1278 | startVlcBtn        | `click`                   | P     | Same — `container.innerHTML = ""` at 1426                                                    | low  |
| 1282 | opt (loop over sourceOptEls) | `click`         | P     | Same — `container.innerHTML = ""` at 1426 (loop adds N listeners; all die with their DOM nodes) | low  |
| 1288 | vlcUrlEl           | `keydown`                 | P     | Same — `container.innerHTML = ""` at 1426                                                    | low  |
| 1294 | stopBtn            | `click`                   | P     | Same — `container.innerHTML = ""` at 1426                                                    | low  |
| 1297 | takeOverBtn        | `click`                   | P     | Same — `container.innerHTML = ""` at 1426                                                    | low  |
| 1301 | flipCameraBtn      | `click` (async)           | P     | Same — `container.innerHTML = ""` at 1426                                                    | low  |
| 1323 | cancelTakeoverBtn  | `click`                   | P     | Same — `container.innerHTML = ""` at 1426                                                    | low  |
| 1333 | window             | `pagehide`                | P     | `destroy()` line 1412: `window.removeEventListener("pagehide", onPageHide)`                  | low  |

**Tally: 12 sites — 10 P / 1 S / 1 G / 0 L.**

## Real leaks (L)

**ZERO real leaks.** Every per-session listener is either:
- explicitly removed at the corresponding teardown site (S — icegatheringstatechange self-removes),
- or guarded such that stale firings short-circuit and the listener dies with its target on GC (G — connectionstatechange with the `state.pc !== pc` pattern),
- or attached to a DOM element whose removal via `container.innerHTML = ""` in `destroy()` severs the listener (P, button-style — implicit but reliable per browser semantics).

## Notes on the borderline cases

**Line 793 connectionstatechange (G):** This is the one site without an explicit `removeEventListener`. The closure pins `state` + `pc`. After `teardownPC()`:
- `state.pc = null` happens BEFORE `pc.close()` (line 753 → 756), so the `'closed'` event fires AFTER `state.pc` has already been nulled, and the listener's `if (state.pc !== pc) return;` short-circuits.
- The PC + its listener form a closed cycle (pc → registry → listener → pc-via-closure). Modern GCs handle reference cycles; once the outer `pc` variable goes out of scope (the `negotiate()` call returns) and no other refs exist, the whole cycle is eligible for collection.
- The deliberate design + the documenting comment at 785-791 + the `state.pc !== pc` guard at 794 together make this safe.
- A belt-and-suspenders alternative (capture the handler, `pc.removeEventListener` in teardownPC) would be slightly cleaner but adds boilerplate without changing the correctness story. **Not recommended** as a follow-up dispatch.

**Lines 1275-1325 button-listeners (P via implicit DOM removal):** When `destroy()` sets `container.innerHTML = ""`, every descendant DOM node is removed. Browsers detach attached listeners as the node is removed (no manual cleanup needed for listeners on detached subtrees). This is reliable but implicit; an `eslint-plugin-react-hooks`-style strictness audit might flag it. The pattern is the JS-DOM idiomatic equivalent of React's auto-cleanup on unmount.

**Line 404 onSettingsUpdated (P via explicit destroy()):** Bound at mount, removed at destroy. Clean.

**Line 673 icegatheringstatechange (S, self-removing):** Inside `waitForIceGathering(pc)`. The `onChange` handler is bound, and the very first thing it does on `pc.iceGatheringState === "complete"` is `pc.removeEventListener(..., onChange)` then `resolve()`. The Promise pattern ensures the listener can't outlive the await. Textbook clean.

## Items intentionally not audited

- **Non-listener event sources** — MutationObserver / IntersectionObserver usage: `visibilityObserver` at line 1198/1408-1410 is properly `.disconnect()`'d in `destroy()`. Not in the dispatch scope but checked in passing; clean.
- **`setInterval`/`setTimeout` lifecycle** — `elapsedTimer` + `statsTimer` cleared in `destroy()` at 1417-1424. Out of audit scope but checked in passing; clean. Note that tickNow in `settings.js` was the leak shape from round 11 (`124d2b4`); live-panel's timers are properly tracked.
- **media-stream track cleanup** — `state.localStream.getTracks().forEach(stop)` in `teardownPC()` at 766-768. Camera-light hygiene; out of listener-audit scope.
- **destroy() being actually called** — confirmed live-panel.js exports a handle with `destroy`, but whether `main.js` (or whichever caller) actually calls it on section unmount is a higher-level audit question. Live-panel's contract is correct; the caller's discipline is a separate concern.

## Recommendation for QA triage

**No fix dispatch needed.** All 12 listener sites are accounted for and safe. The connectionstatechange site at 793 is the only one without explicit `removeEventListener`, but the documented `state.pc !== pc` guard + GC semantics make it correct-by-design. A belt-and-suspenders explicit-remove refactor would be ~5 LOC of additional code with no behavior change.

The audit's main takeaway: the original survey concern (#5 in 822d7d2) anticipated that "12 add / 3 remove" might indicate leaks, but tracing each site shows the codebase uses a mix of three legitimate cleanup strategies: explicit `removeEventListener`, self-removing event handlers, and DOM-removal-via-innerHTML which severs listeners as a side effect. The 3:12 ratio is misleading because two-thirds of the adds are on DOM elements that get cleaned via the third strategy.

If a future "make all cleanups explicit" lint rule lands, the work would be:
1. Capture the connectionstatechange handler in `negotiate()`, `pc.removeEventListener` in `teardownPC()`.
2. For the 8 button listeners, no actionable change — DOM-removal cleanup is the idiomatic JS pattern and is reliable.
