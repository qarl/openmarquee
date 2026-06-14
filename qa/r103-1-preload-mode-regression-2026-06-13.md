# FYS PRELOAD_MODE regression — 2026-06-13 closed

**Status:** CLOSED 2026-06-13. Config-side fix applied to FYS
(`OPENMARQUEE_PRELOAD_MODE` drop-in removed → defaults to code's
`defer`). Permanence work landed in
`task/preload-mode-permanence-2026-06-13` (PR pending).

## The bug

On FYS (Pi Zero 2 W, 720p text-over-video playlist, renderer r103.1 =
md5 75bd94bb = commit `c935f2e`), qarl observed: **the outgoing video
background went to ALL BLACK the instant a transition starts.** Only
the text layers composited; the bg video on slide A disappeared the
moment BeginTransition fired, despite playing fine during slide A's
steady-state hold.

The bug was NOT visible in the offscreen `--capture-sb-mid` golden
runner from PR #2 — that path takes a different code route
(poster-substitution in the capture path; PR #2 fixed it for offscreen
testing). The live path on r103.1 routes through the IPC PaintTransition
handler's dual-decoder bake, which is a separate failure surface.

## Root cause

`OPENMARQUEE_PRELOAD_MODE=max` had been left over from QA's earlier
dual-1080p experiment work as a systemd drop-in on FYS. Under `max`,
the Python playback loop fires `PreloadSlide(N+1)` immediately after
`begin_slide(N)` returns (see `backend/openmarquee/playback.py`), AND
the Rust IPC arm skips the r97 `should_defer_preload_for_codec_contention`
guard at `renderer/src/ipc_main.rs:3668`. The result for an
all-text-over-video playlist:

- BeginSlide(A) opens A's bg V4L2 decoder. Steady-state plays fine.
- Immediately, PreloadSlide(B) fires under `max`, opens B's bg V4L2
  decoder. Both decoders alive concurrently for the full ~5 s of A's
  hold. A's decoder produces frames per tick; B's decoder sits idle.
- BeginTransition(B) fires. Both decoders are now baked per tick by
  `paint_and_present_one_transition_frame`. They share the single
  bcm2835-codec VPU.
- The pre-r106 blocking-feed cadence in
  `bake_video_slide_to_current_fbo` (which is what r103.1 has — r106
  feed/drain decouple was never picked up onto the r103.1 baseline)
  feeds 1 sample per bake-call then polls `next_frame` for up to
  10×3ms. Under contention, that's not enough input — A's decoder
  underfeeds and returns `Ok(None)` ("no frame this tick"). The
  paint-tick is skipped (`hdmi.rs:6059`), scanout *should* hold A's
  prior frame, but in practice the outgoing video went BLACK from
  qarl's vantage on the wall.

The empirical confirmation was a soak A/B on FYS:

| Mode    | from-side no-frame | text-only/black | EINVAL | delta_ms median |
| ------- | ------------------ | --------------- | ------ | --------------- |
| `max`   | non-zero (per-tx)  | YES             | n/a    | 251 ms          |
| `defer` | 0                  | 0               | 0      | 40 ms           |

## Diagnosis history

The diagnosis path (recorded for the lessons file):

1. Initially mistook this for the SAME bug as PR #2 (which fixed the
   offscreen capture path). My PR #2 root-cause analysis incorrectly
   claimed r103.1 predated r46/r50's `TransitionEndpoint::TextOverVideo`
   work. **QA caught it:** verified by direct git check that c935f2e
   has `TransitionEndpoint::TextOverVideo` 14 times, identical to HEAD.
   r103.1 is 2026-06-09; r46 is 2026-06-02; r50 is 2026-06-03. r103.1
   DOES contain the live TextOverVideo path.
2. Pivoted to git-archaeology of commits between `5c5ff39` (r50) and
   `c935f2e` (r103.1) touching the bake/transition path. Three prime
   candidates: r97 deferred-preload + r98 mode knob interaction; r102.2
   FBO cache; r101 EGLImage cache.
3. Wrote D1 / D2 / D3 confirmation tests. **D1** ("flip
   `OPENMARQUEE_PRELOAD_MODE=defer`") confirmed Candidate A across two
   soaks: starvation signature cleanly absent under defer.
4. Visual confirmation via code1's live-preview infra
   (`OPENMARQUEE_LIVE_PREVIEW_PATH`, commit `3159789`).

## Fix

**Config-side only.** No live-code change shipped. The fix was:
remove the `Environment=OPENMARQUEE_PRELOAD_MODE=max` drop-in from
FYS's systemd unit, reload, restart, verify the value is empty per
`systemctl show openmarquee-backend | grep PRELOAD_MODE`.

The code's default is `defer` (Python `_resolve_preload_mode` returns
`PRELOAD_MODE_DEFER` when env is unset; Rust `parse_preload_mode(None)`
returns `PreloadMode::Defer`). Both were correct already.

## Permanence work (this branch)

Branch: `task/preload-mode-permanence-2026-06-13`. Shipped:

1. **docs/hardware-ceilings.md** — new ⚠ section documenting
   `OPENMARQUEE_PRELOAD_MODE=max` as an experiment-only knob that
   MUST NEVER be set on 720p production. Cleanup recipe inline.
2. **playback.py `_resolve_preload_mode`** — emits a `log.warning`
   when the resolved mode is `lead` or `max`, citing the FYS
   regression + the docs path. Loud-at-startup so a future operator
   SSHing in to inspect doesn't miss the experiment knob.
3. **ipc_main.rs `PreloadMode` doc-comment** — variants `Lead` /
   `Max` carry an `EXPERIMENT-ONLY` tag + cross-link to
   `docs/hardware-ceilings.md`.
4. **backend/tests/test_playback.py** — 4 new tests:
   - `test_max_emits_experiment_only_warning`
   - `test_lead_emits_experiment_only_warning`
   - `test_defer_does_not_emit_experiment_warning`
   - `test_no_repo_setter_ships_preload_mode_max` (grep regression-lock
     scanning the deploy surface for `OPENMARQUEE_PRELOAD_MODE=max|lead`
     literal in any shipped `*.service` / `*.sh` / `*.conf` file)
5. **renderer/src/ipc_main.rs tests** — 2 new tests pinning the
   `EXPERIMENT-ONLY` tag + `hardware-ceilings.md` cross-link in the
   `Max` and `Lead` doc-comments so a future doc cleanup can't silently
   drop the contract.
6. **backend/openmarquee/rendering/preload_journal.py** — new
   host-portable journal analyzer that classifies a journalctl capture
   window into a `PreloadJournalSummary` and exposes
   `assert_production_clean()`. The QA-facing failure message points
   directly at `OPENMARQUEE_PRELOAD_MODE` and the docs path so the next
   time someone hits this, the fix is one journal capture away.
7. **backend/tests/rendering/test_preload_journal.py** — 20 new tests
   covering the analyzer's classification surface, including a
   `TestFysRegressionShape` class that pins the empirical FYS shape
   (`max` soak counters vs `defer` soak counters).
8. **renderer/tests/scripts/run_live_preload_contention.sh** —
   QA-runnable Linux-only driver that boots the production unit with a
   2-text-over-video playlist, soaks 30 s under `defer`, captures the
   journal, pipes through the analyzer, and asserts production-clean.
   Optional A/B against `max` via `RUN_BROKEN_MODE_AB=1`.

## Lessons captured (memory candidates)

1. **My RC claim that "r103.1 predates TextOverVideo" was wrong.**
   I asserted it without grepping `c935f2e` for `TransitionEndpoint::
   TextOverVideo`. QA caught it in one greps. Memory entry:
   ALWAYS verify "predates" claims against the actual commit, not
   against memory of when a feature shipped.

2. **An offscreen golden test does NOT cover the live PaintTransition
   dual-decoder path.** The PR #2 golden test was for a different
   bug — the `--capture-sb-mid` code path with no V4L2 plumbing.
   Live-pipeline regressions need live-pipeline tests, which is what
   the `run_live_preload_contention.sh` runner is for.

3. **A leftover experiment env knob can mimic a code regression
   indistinguishably.** Before architecture-level fixes (poster-freeze
   etc.), always check the device's actual runtime env (the
   `systemctl show` recipe is fast and cheap).

## Cross-references

- `c935f2e` — r103.1 production binary commit.
- `5c5ff39` — r50 commit (where TextOverVideo live shipped).
- `a946f9f` — r106 feed/drain decouple (not on r103.1; would solve
  the dual-decode contention if it had been picked up).
- `85153f6` — r97 conditional defer preload (the guard that `max`
  bypasses).
- `1c6d778` — r98 PRELOAD_MODE knob (where `max` exists).
- `docs/hardware-ceilings.md` — production contract.
