---
severity: major
date: 2026-05-25
surface: ui/captive-portal
type: audit
---

# UI Error-Boundary Audit — 2026-05-25

## Summary

Audited the 27 vanilla-JS modules under `ui/src/` covering 142 `addEventListener` sites and 9 distinct `fetch(`/`apiFetch(`/`mediaSrc(` callers. Bottom line: the SPA is in materially better shape than the dispatch's worst-case hypothesis — there is no realistic operator action that blanks the screen and forces a refresh+re-auth. There are however **zero global error boundaries** (no `window.onerror`, no `unhandledrejection` handler) and **two animation-loop hazards** where a single throw inside a per-frame callback kills the loop with no auto-recovery. Reporting **8 findings: 0 blocker, 2 major, 5 minor, 1 nit.**

## What's already good

- **`api.js` centralizes the fetch contract.** Every non-pre-auth fetch goes through `apiFetch()` (lines 45-105) which injects auth, handles 401 redirect, and throws on auth failure. The wrapper-level write functions (`saveTextSlide`, `updateImage`, `patchSlideDuration`, etc.) consistently throw `Error("Save failed (HTTP NNN): <detail>")` so callers downstream can surface a real message via `err.message`.
- **`auto-save.js` is exemplary.** Every save attempt is wrapped in try/catch (lines 88-101) and on rejection paints `Couldn't save · <message>` into the status element with `data-state="error"`. Every editor/uploader/playlist/schedule save flows through this helper.
- **`extractDetailMessage()` (api.js:142-166)** pulls operator-friendly copy out of FastAPI's 3 error shapes (validation array, raw `detail` string, structured `{error, error_class}`) so toasts get useful text instead of "400 Bad Request".
- **`apiFetch` 401 handling** auto-redirects to `/login.html` or `/welcome.html` depending on whether the backend was ever configured — recovers from stale-token scenarios without operator intervention.
- **`rotation-rerender.js`** is a model of resilience: each per-slide rerender is in its own try/catch (lines 159-177), one bad slide can't abort the bulk pass, and the summary `{total, rerendered, skipped, failed}` is returned cleanly.

## Findings (sorted severity descending)

### 1. `inline-preview.js:1114-1133` — animation loop dies on first `renderOnce()` throw

**Severity: major**

The `tick(now)` rAF callback wraps `renderOnce()` in a try/**finally** (the finally only closes `markEnd`, not a catch), so if `renderOnce()` throws (e.g. `samplerCtx.getImageData` on a 0×0 canvas during a layout race, a corrupt cached image, or a font-load edge case in `drawTextOverVideo`), the exception propagates past the `finally` and **the next `rafId = requestAnimationFrame(tick)` is never reached.** The Playlists-panel preview freezes mid-frame; pressing play/pause does nothing (play handler sees `playing` is already `true`); operator has to navigate away+back or refresh to recover. Console gets an unhandled rejection at the rAF boundary that operators on phones can't see.

**Recommended fix shape:** wrap the rAF body in a try/catch that logs and still schedules the next frame, so a transient bad-frame doesn't take the loop down permanently. Same shape `rotation-rerender.js` already uses for its per-slide loop.

### 2. `editor.js:530-547` — motion preview rAF loop dies on first `drawCanvas` throw

**Severity: major**

Same shape as Finding #1: `tick(now)` calls `drawCanvas(canvas, state, { elapsed_s })` (line 544) with no try/catch around it, then reschedules with `motionRafId = requestAnimationFrame(tick)`. If `drawCanvas` throws (motion state goes degenerate, font-face load race, etc.) the next rAF is never scheduled. The editor canvas freezes on a single static frame; subsequent edits paint correctly via the synchronous `drawCanvas(canvas, state)` calls on input/change, but **motion preview is silently dead until the operator switches tabs and back** (which re-mounts via `openmarquee:settings-updated`, but only if a settings change occurs — otherwise: refresh required).

**Recommended fix shape:** try/catch around the `drawCanvas` call; on error, log + skip the frame + reschedule. Or wrap the whole `tick` body the same way.

### 3. `playlist-track.js:577-601` — duration save error invisible to operator

**Severity: minor**

The duration-chip click handler does `await onUpdateDuration(id, ms)` inside a try/catch whose catch is **`console.error` only** (line 599). If `patchSlideDuration` throws (server down, 4xx, network drop), the prompt closes, the operator sees no change, and the persisted duration is unchanged. On a phone with no devtools the operator's mental model is "the click did nothing" — likely retry, possibly conclude the feature is broken.

**Recommended fix shape:** surface the error in `statusEl` (the same status element auto-save uses) with the standard `Couldn't save · <message>` pattern.

### 4. `main.js:1013-1023` — `runDeleteCascade` rejection becomes unhandled promise

**Severity: minor**

After a successful delete, `await runDeleteCascade({...})` is called with no try/catch. The cascade's own contract (slide-event-handlers.js:74-78) explicitly notes "if ANY refresh rejects, the cascade rejects." So a single failing refresh (e.g. `playlistTrack.refresh()` 500s because the operator just deleted the active playlist's only slide) becomes an unhandled rejection. The delete itself succeeded so the UI isn't blank — but the user sees stale tiles on N-1 surfaces with no error indication.

**Recommended fix shape:** wrap the cascade in try/catch; on rejection, log + paint a non-blocking banner like "Deleted, but some panels may need a refresh."

### 5. `main.js:407-420` — `createNewPlaylist` has no error path

**Severity: minor**

`createNewPlaylist` chains five awaits (`listPlaylists`, `createPlaylist`, then four `refresh()` calls) and is invoked from `playlist-track.js:176` as `() => onCreatePlaylist?.()` — fire-and-forget, no `await`, no `catch`. If `createPlaylist` rejects (server down, name collision raced), the click is silently swallowed; the operator clicks New again, possibly creating duplicate playlists once the network recovers.

**Recommended fix shape:** wrap the body of `createNewPlaylist` in try/catch; surface via the playlists panel status element. Bonus: disable the New button while in flight.

### 6. `main.js:503-513` — playlist `onSelect` rejection unhandled

**Severity: minor**

The playlist-browser tile click invokes `onSelect(playlistId)` synchronously (playlist-browser.js:124), which calls the `main.js:503` async arrow. If `playlistTrack.refresh()` or `inlinePreviewHandle.refresh()` rejects, the rejection propagates out of the click handler with nowhere to go (becomes unhandled). UI is left half-switched: highlight updated, but the displayed track and preview reflect the OLD playlist. No operator-visible error.

**Recommended fix shape:** try/catch the body of the `onSelect` arrow; on error, revert the highlight + surface a status message.

### 7. `main.js:983-996` — `openmarquee:edit-slide` error logs only

**Severity: minor**

The edit-slide custom-event handler does `await fetchContentItem(id)` then `route.load(slide)` in a try/catch. The catch (line 991-995) is **`console.error` only**. An operator who clicks the ✎ pencil on a slide tile while the backend is briefly down sees the URL hash change (line 989 runs before the await throws... actually wait — the await happens first, so the hash doesn't change either). They see nothing happen. On a phone, this is hard to distinguish from "I missed the tap target."

**Recommended fix shape:** add a window.alert or a top-level toast helper (none exists today; would need creating). Lower-effort: surface via the editor's status pill if the route resolved before the throw.

### 8. No global error boundary anywhere in the SPA

**Severity: nit**

`grep -rn "window.onerror\|unhandledrejection\|window.addEventListener.*error"` across `ui/src/*.js` returns ZERO matches. Every uncaught throw and unhandled rejection from any of the above findings (and any unknown future ones) goes straight to the browser console with no operator surface. On a phone in the captive-portal flow this is invisible; operators get no signal that anything went wrong other than "the thing I clicked did nothing."

**Recommended fix shape:** add a small top-level handler in `main.js` (after `boot()` is wired) that listens for `error` and `unhandledrejection` on `window` and paints a dismissible banner ("Something went wrong — try again or refresh") into a fixed slot in the topbar chrome. Defense-in-depth — caught issues from Findings 1-7 would still need their own surface for context-appropriate messages, but this is a catch-all backstop.

## Out-of-scope

- **`ui/test/`, `ui/e2e/`, `*.test.js`** — test files, not production.
- **`ui/dist/`** — Vite build artifact; rewritten on every build.
- **`renderer-wasm/`** — Rust source compiled to wasm; not part of this audit.
- **Backend Python (`backend/`)** — separate concern; only matters here insofar as it sends shapes the UI's `extractDetailMessage` already handles.
- **Pre-auth pages (`login.js`, `set-password.js`, `welcome.js`, `first-run.js`)** — spot-checked; each has try/catch on its submit path + visible error elements. No findings.
- **`flock.js`, `settings.js`, `schedule.js`, `live-panel.js`, `video-upload.js`, `image-upload.js`, `stream-upload.js`, `web-slide.js`** — read in full; all event-handler bodies have try/catch with operator-visible status surfaces. No findings.
- **Lazy/dynamic imports (`sortablejs`, `qrcode`, font-picker popovers)** — load failures degrade visibly today (Sortable drops to no-reorder, QR shows placeholder); not in scope to harden further.

## Reproduction recipe for the worst item (Finding #1, inline-preview loop death)

This is the hardest of the two major findings to provoke in normal use (the editor motion-loop death in #2 is structurally identical but harder to trigger without an exotic state).

1. Boot the UI against a healthy backend with at least one playlist containing a `text_slide` with `auto_mode` set + a `background_video_slide_id` reference (the `drawTextOverVideo` path at inline-preview.js:919).
2. Navigate to the Playlists panel — inline preview begins its rAF tick loop.
3. In DevTools console, monkey-patch `CanvasRenderingContext2D.prototype.drawImage` to throw on the next call:

```js
const orig = CanvasRenderingContext2D.prototype.drawImage;
let armed = true;
CanvasRenderingContext2D.prototype.drawImage = function(...args) {
    if (armed) { armed = false; throw new Error("simulated draw failure"); }
    return orig.apply(this, args);
};
```

4. Press play on the inline preview. **Expected (good behavior):** one frame skipped, loop recovers, playback continues. **Actual (today):** the loop stops at the failed frame. The play button's icon and `playing` flag remain consistent with "playing" but no rAF is scheduled. Pressing pause then play does not recover (the play handler short-circuits on `next === playing`).

5. **Recovery for the operator:** navigate to a different section + back, OR refresh the page. No data loss — preview state is ephemeral — but lost operator confidence.

For Finding #2 (editor motion loop), the same pattern works against any text-slide editor with at least one layer set to `motion != "static"`; replace `drawImage` with throwing inside `requestAnimationFrame`'s scheduled fn.
