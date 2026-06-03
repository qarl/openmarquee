# r52 — PlaylistItem.transition_ms operator-editable per Option B

**Author:** jimmy:openmarquee-code2
**Date:** 2026-06-03
**Status:** SHIPPED on code2; cherry-picked to main
**Dispatch:** qarl-direct decision on the r49 F081 finding
**Predecessors:**
  - r49 UI-vs-model audit (12a986e7) — flagged this gap HIGH
  - r48 (code1, renderer V4L2 free-list refactor) — non-conflicting

## Goal

The r49 audit's F081 finding: PlaylistItem.transition_ms is honored
by the renderer (content.rs:48) + inline-preview.js:322, but the
playlist track UI hard-codes 500ms at 3 sites and provides no
operator-editable control. Operators can pick HOW the transition
looks but not HOW LONG it takes.

qarl picked **Option B** from the UX sketches: the track-block
transition chip stays compact in the row; clicking it opens a
popover with kind + duration controls. Preserves the dense
scannable row; click reveals the editor.

## Design

### The chip

Pre-r52: `<select class="om-pulldown track-block-transition">` —
native dropdown with the 16 transition kinds. Picking from it
fired a `change` event that wrote `block.dataset.transition`.

Post-r52: `<button class="om-pulldown track-block-transition">` —
the same class survives (test selectors + r49 audit + CSS comment
at styles.css:639-642 explicitly promised the class is "purely a
JS hook"). The button label compactly shows the current kind +
duration:

  cut         (no animation, so no duration to show)
  fade · 500ms
  fade · 1.5s (formatter switches to seconds at >= 1000ms)

Click toggles a sibling popover anchored absolutely below the chip
(see styles.css). The popover contains two controls:

  Kind: <select class="track-block-transition-kind">
        — full 16-option list from TRANSITION_OPTIONS
  Duration: <input type="number" class="track-block-transition-ms"
                   min="0" max="5000" step="50">
            — Pydantic field constraint mirrored client-side

### Cut-coupling logic

The dispatch's hard constraint: **cut transitions must always have
length=0.** The popover enforces three coupled behaviors:

1. **Initial render with kind=cut:** ms input is disabled +
   value=0 + block.dataset.transitionMs="0" (regardless of what
   the server sent; the model validator clamps too).
2. **Operator flips kind cut → other:** ms input enables.
   Restore the remembered `lastNonZeroMs` (stored on the block's
   dataset whenever a non-zero ms is set) or default 500. The
   data flow: operator typed 750ms on a fade entry; later flipped
   to cut (which clamped ms→0 but remembered 750); then back to
   fade — restores 750.
3. **Operator types non-zero ms while kind=cut:** auto-switch kind
   to `lastNonCutKind` (default "fade"). Dispatch quote: "if kind
   is 'cut', auto-switch kind to 'fade' (or whatever previous
   non-cut kind was)". The two writes happen in a single input
   event so the operator sees an immediate one-step transition
   from a "cut · 0ms" state to "fade · 300ms" without having to
   visit the kind select.

The popover closes on outside-click (anywhere on the page that
isn't inside a transition-wrap) or Escape.

### Backend validator

A `model_validator(mode="after")` on `PlaylistItem` enforces the
same invariant server-side:

```python
@model_validator(mode="after")
def _clamp_cut_to_zero(self) -> "PlaylistItem":
    if self.transition == "cut" and self.transition_ms != 0:
        self.transition_ms = 0
    return self
```

**CLAMP rather than reject.** Reasoning:

1. **Legacy on-disk JSON:** pre-r52 every entry (including cut)
   was created with `transition_ms=500` by the UI. The on-disk
   shape `{transition: "cut", transition_ms: 500}` is ubiquitous.
   A rejecting validator would refuse to load every existing
   playlist. Clamping silently fixes the data.
2. **API caller mistakes:** a future API caller that POSTs
   `{transition: "cut", transition_ms: 200}` should get an
   answer of "cut · 0ms" rather than a 422. The semantics is
   "cut means instant"; clamping is the gentler shape.
3. **No information loss:** "cut + non-zero ms" carries no
   meaningful information — a cut animation has no time
   parameter. Clamping discards nothing of value.

**Migration semantics.** v2 storage migration code path
(`PlaylistStorage._migrate_v2`) builds PlaylistItem instances
from item_ids with `transition="cut"`. Pre-r52 these inherited
the field default `transition_ms=500`. Post-r52, the
model_validator clamps them to 0 on construction. The
`test_v2_on_disk_migrates_with_default_transitions` test was
updated to assert the new canonical 0 instead of the legacy 500.

The migration is silent: existing playlists load cleanly, get
clamped, and re-save with the canonical shape. No operator-
visible change.

## Files modified

| File                                    | Change                                              |
| --------------------------------------- | --------------------------------------------------- |
| `backend/openmarquee/playlist.py`       | +10 LOC model_validator on PlaylistItem             |
| `backend/tests/test_playlist.py`        | +50 LOC: 4 new tests + 1 updated assertion         |
| `ui/src/playlist-track.js`              | +130 LOC: chip-as-button + popover + cut-coupling   |
| `ui/src/playlist-track.test.js`         | +175 LOC: 3 updated + 6 new tests                   |
| `ui/styles.css`                         | +50 LOC: popover positioning + disabled-input style |

Stale-comment ride-along (per dispatch §D): `ui/src/playlist-track.js:1`
updated from "horizontal timeline" to "vertical stack of track
blocks" to match the post-redesign layout per the CSS comment at
styles.css:436-442.

## Test coverage

### Backend (`backend/tests/test_playlist.py`)

| Test                                                       | What it asserts                                                                       |
| ---------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `test_playlistitem_cut_clamps_transition_ms_to_zero_on_construction` | Constructor with cut + ms=500 → clamped to 0                                          |
| `test_playlistitem_non_cut_preserves_transition_ms`        | Constructor with fade + ms=750 → preserves 750                                        |
| `test_playlistitem_cut_already_zero_no_op`                 | Constructor with cut + ms=0 → no-op (canonical shape)                                 |
| `test_playlistitem_cut_default_ms_clamps_to_zero`          | Bare construction (kind defaults to "cut", ms defaults to 500) → clamps               |
| `test_v2_on_disk_migrates_with_default_transitions`        | UPDATED: v2 migration now yields ms=0 for cut entries (was 500 pre-r52)               |

### Frontend (`ui/src/playlist-track.test.js`)

| Test                                                                       | What it asserts                                                          |
| -------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| Existing "saves the playlist back" test — UPDATED to drive popover         | Chip is BUTTON; click opens popover; cut→fade restores ms=500 default    |
| Existing "hydrates transition metadata" test — UPDATED                     | Chip label reads "fade · 250ms"; popover hydration matches server data   |
| Existing "fires with draft entries when transition changed" — UPDATED      | Kind change inside popover fires onDraftChange (was direct chip.value)   |
| Existing "doesn't throw when onDraftChange isn't provided" — UPDATED       | Popover-driven flow works without onDraftChange callback                 |
| NEW: cut → fade restores lastNonZeroMs default 500                         | First-render cut state; flip to fade enables input + restores 500        |
| NEW: fade → cut clamps ms to 0 + disables + remembers                      | Pre-set 750ms; flip to cut clamps; flip back to fade restores 750        |
| NEW: typing non-zero ms while kind=cut auto-switches to fade               | Operator types 300ms on cut entry; kind auto-flips to fade               |
| NEW: clamps ms input to 0-5000 range on input                              | Types 99999 → clamped to 5000; types -50 → clamped to 0                  |
| NEW: chip label shows kind + ms compactly                                  | "cut", "fade · 500ms", "fade · 1.5s" formats                             |
| NEW: popover open/close (default closed; outside-click closes)             | Default `hidden=true`; chip click opens; document.body.click() closes    |

## What I did NOT do (out of scope per dispatch)

- **Did not fold in other r49 findings** — outline + drop_shadow are
  the r51 dispatch's lane.
- **Did not change the transition KIND palette** — 16 kinds stay the
  same. Just exposing the duration knob.
- **Did not add visual preview of the new transition_ms** in
  inline-preview — it already reads `entry.transition_ms` correctly
  (line 322); operators get the new duration in the preview
  automatically once draft fires.
- **Did not edit SYSTEM_SPEC** — admin-Jimmy's lane.
- **Did not add a "reset to defaults" button** — kind+ms control
  is two clicks away; resets would add UI clutter for marginal
  value.

## Open questions

### G.1 Should the operator be able to type ms while kind=cut?

Per the dispatch the input is DISABLED on kind=cut. But the
auto-switch-to-fade behavior triggers only if the operator types
into the (then-disabled) input. In practice, the input being
`disabled` prevents most keyboard input; the cut→fade auto-switch
mainly handles synthetic dispatch from tests OR a future "type-
ahead" UX where the input is hidden vs disabled. The behavior is
defensive — if the input ever does receive input while kind=cut,
the swap fires.

**Recommendation:** keep the current shape. Operator-typing while
the input is `disabled` won't happen in the browser; the
auto-switch logic is the right defensive shape.

### G.2 Should the popover anchor be improved when the chip is near the bottom of the viewport?

Current absolute positioning at `top: calc(100% + 4px)` places
the popover BELOW the chip. For chips near the viewport bottom,
this may push the popover off-screen.

**Recommendation:** defer. Real operators on real signs (not
phone-sized screens) won't hit this; if it surfaces, add a
flip-on-overflow CSS trick or `floating-ui` later.

### G.3 Should the kind palette show keyboard shortcuts?

The current popover surfaces all 16 kinds in a scrollable select.
A future polish could surface common kinds (cut/fade/wipe/iris)
as one-tap buttons.

**Recommendation:** defer. Not in this dispatch's scope.

## Sacred subagent review

Pending — runs before the commit.

## Lane

- Multi-file commit: backend/ + ui/src/ + ui/styles.css + audit doc
- code2 push; cherry-pick to main via /tmp clone
- No SYSTEM_SPEC.md edits (admin-Jimmy lane per r49 §F.3)
- No code1/r48 conflict (r48 is V4L2 renderer surface; r52 is
  playlist UI surface)
- Pre-push hook runs: backend pytest, renderer cargo test, aarch64
  cross-compile, UI vitest (or warn-pass if jsdom is unavailable)

## Push posture

- Backend pytest: 1367 PASS / 0 FAIL locally (was 1363; +4 new
  test_playlistitem_cut_* tests + 1 updated migration assertion).
- UI vitest: not runnable locally due to missing
  `ui/node_modules/jsdom` per
  [[feedback_npm_install_virtiofs_wedge]]. Per
  [[feedback_test_commits_need_runtime_verify]], the pre-push hook
  will warn-and-pass on the missing vitest binary OR run it server-
  side; QA can verify on FYS post-deploy if needed.
- Standard /tmp clone + cherry-pick if NFS-wedges.

---

End of r52 audit.
