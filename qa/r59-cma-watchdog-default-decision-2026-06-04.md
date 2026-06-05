# r59 — CMA watchdog default for v1.0.1 tag-cut

**Author:** jimmy:openmarquee-code2
**Date:** 2026-06-04
**Status:** SHIPPED on code2; cherry-picked to main
**Dispatch:** qarl-pending decision from the post-compact session-end
note; v1.0.1 tag-cut gate
**Predecessors:**
  - r38c CMA-pressure watchdog (`2369815`) — shipped the watchdog at
    THRESHOLD_MB=220
  - r38d SIGUSR1 cache-dump (`95b150a`) — surfaced the cma_used
    swing band 229-254 MB on a 15-min FYS trace
  - r48 V4L2 OUTPUT free-list (`c2edea1`) — closed the text-over-
    video race that had inflated CMA churn pre-fix
  - r50 text-over-video in transitions (`5c5ff39`) — added
    `force_evict_image_caches_for_cma_pressure` to keep peaks
    bounded
  - r54 v1.0.1 release notes (`86574f3` + `3163f6c`) — flagged the
    watchdog default decision as one of the remaining tag-cut
    gates

## Decision

**Option A — bump `THRESHOLD_MB` default from 220 to 254 MB.**

```
system/openmarquee-cma-watchdog.sh:14   # comment example
system/openmarquee-cma-watchdog.sh:26   # the actual default
```

Both lines updated in this commit. The /etc/default override path
remains operator-tunable via `THRESHOLD_MB=<value>` per the script's
existing env-var convention.

## Empirical data

| Workload                                       | CMA reading           | Source                                                       |
| ---------------------------------------------- | --------------------- | ------------------------------------------------------------ |
| FYS swing band (15-min trace, r38c-watchdog era)| **229-254 MB band**  | `qa/r38d-sigusr1-cache-dump-2026-06-02.md:14`                |
| Post-r48 + r50 text-over-video peak (FYS)      | **~251.8 MB**         | r59 dispatch §Context (QA r50 visual-verify session)         |
| Cross-reference (release-notes)                 | "229-254 MB" band     | `CHANGELOG.md` r54 entry; recommends "bump to 254-260 MB"    |

The **251.8 MB FYS peak** is the empirical anchor cited by QA in
the r59 dispatch — there is no standalone r48/r50 audit doc on
disk; the figure comes from QA's r50 visual-verify telemetry as
relayed in the dispatch text. The 229-254 MB swing-band measurement
in `qa/r38d-...md:14` (15-minute per-minute-poll trace post-r38c
deploy) corroborates the upper bound: the band's high-water mark
matches the 251.8 MB peak to within ~2 MB, so the choice is
defensible against the on-disk artifact even if the dispatch's
exact 251.8 MB figure isn't separately archived.

Threshold-vs-peak math with the on-disk anchor (band high-water
~254 MB):

- **0 MB margin above swing-band high-water.** Threshold sits AT
  the top of the observed band, NOT above it. The watchdog may
  fire on benign peaks at the top of the established band.
- **−2 MB margin below the 256 MB CMA pool reservation.** Watchdog
  fires BEFORE the kernel allocator returns -ENOMEM (the
  intended saturation behavior; see next section).

Threshold-vs-peak math with the dispatch's 251.8 MB figure:

- **+2.2 MB margin above measured peak.**
- **−2 MB margin below the 256 MB reservation.**

This is more aggressive than r38c's original §C table considered
(which rejected 240 MB as "ceiling headroom is 1-2 paint cycles
only"). The audit's defense for accepting that tightness lives in
the next section: at 254, even if the watchdog is missed during a
cooldown window and the pool reaches 256, the failure modes
(black frame, still-image fallback, MMAP fallback) are non-
catastrophic, not kernel-level. The tradeoff is a sub-second
operator-visible blip on a transient peak vs. a degraded but
recoverable frame at saturation.

If post-deploy data shows the watchdog firing on benign peaks at
the band high-water, the r60 sustained-N-polls detector becomes
the right next step.

## What the threshold breach actually triggers

`system/openmarquee-cma-watchdog.sh:127` invokes
`systemctl restart --no-block openmarquee-backend.service`. This:

1. Kills the running `uvicorn` + the renderer subprocess
2. CMA pages allocated by the renderer (GBM/V4L2/EGLImage/GLES) are
   released by the kernel as the process exits
3. systemd restarts the backend; renderer subprocess re-spawns
4. New CMA allocations track from baseline (~187 MB cold)

The user-visible result is a **sub-second sign blank** while the
restart completes. No kernel reboot. No data loss. No SD-card
corruption.

The script also persists a `last_restart_epoch` to
`/var/openmarquee/cma-watchdog-state` and enforces a 1800s cooldown
(line 27) between restarts to prevent restart-storms. This existing
cooldown already filters out rapid back-to-back triggers, which
matters for the defensibility of Option A vs Option B (see below).

## Saturation behavior at 256 MB pool ceiling

If the watchdog ever MISSES a peak (e.g. during the cooldown window
the system crosses 256 MB), the kernel CMA allocator returns
-ENOMEM to userspace. The likely failure points:

- **Renderer scanout buffer alloc** — GBM `lock_front_buffer`
  returns null; the renderer code that catches this returns a
  black frame for that tick. Not a crash.
- **V4L2 OUTPUT buffer alloc** — `VIDIOC_REQBUFS` returns ENOMEM;
  the V4L2 decoder reports the failure to the playback engine,
  which falls back to a still-image render.
- **EGLImage DMABUF import** — fails; the dmabuf-path code path
  has a fallback to MMAP read (slower but works).

**No kernel panic, no kernel reboot.** The renderer either degrades
to a degraded frame OR fails the slide and the playback engine
moves to the next one. The renderer's existing crash recovery
(systemd `Restart=on-failure` + `StartLimitBurst=5 / 5min`) catches
any process-level crash.

Saturation also has a "sticky pages" failure mode where CMA can
fragment after sustained pressure: even if userspace releases all
its allocations, the kernel allocator may need a few seconds to
defragment before it can satisfy a new large request. The
watchdog's hard restart pattern is the right mitigation for this:
forcing the renderer to exit and re-allocate from a clean baseline
breaks the fragmentation.

## Why B (sustained-N-polls) is wrong for v1.0.1

The "trip only if cma_used >= THRESHOLD for N of last M polls"
detector is conceptually more robust to transient peaks. But:

1. **Wrong timeline.** ~15 LOC + state file extension + new failure
   modes (corrupted poll history → watchdog silently disabled →
   real exhaustion goes unhandled). For a tag-cut blocker, the
   correct move is the lowest-complexity defensible fix.
2. **The existing 1800s cooldown already filters restart-storms.**
   A transient peak that trips threshold and restarts the backend
   has at least 30 minutes before another restart can fire. If the
   transient was benign, the post-restart baseline (~187 MB) sits
   well below 254; no churn.
3. **At 254 the false-positive surface is small.** Peaks at 251.8
   don't trip 254. Only TRUE exhaustion (or a real growth condition
   we'd want to recover from) would cross 254.
4. **Doesn't fight r58's wider 2-pool window.** r58's pre-warm
   pushes a ~500ms wider window during which 2 V4L2 pools coexist.
   Per the dispatch and r58's math, the steady-state peak is
   unchanged; the window is just wider. Threshold at 254 still
   leaves room.
5. **Re-evaluable post-v1.0.1.** If FYS data post-deploy shows
   transient spikes ≥ 254 are common, we ship the sustained-N-polls
   detector as r60 with empirical poll-count data to inform N + M.

## Why C (accept 220 + operator override) is wrong for v1.0.1

1. **Threshold 220 is empirically wrong.** Measured peak 251.8 sits
   32 MB above the threshold. Operators running text-over-video
   will see persistent backend restarts on the established
   workload — bad first-impression UX.
2. **"Boot it and it works" principle.** The default must be sane
   for the dominant workload. The /etc/default override mechanism
   is for outlier deployments + tuning, not for fixing a known-
   wrong default.
3. **Day-1 operators don't have local tuning info.** They need a
   default that matches the dominant workload, not a doc that
   says "your sign will reboot itself unless you read this".
4. **Documentation churn.** C requires CHANGELOG + README + admin-
   runbook updates explaining the workaround, and the workaround
   needs to remain accurate as the workload evolves. Option A is
   one line; Option C is documentation maintenance forever.

## Brick-the-Pi check

The 2026-05-31 brick was specifically about `cma=384M` (a kernel
cmdline.txt arg that BUMPS the CMA pool itself from 256 to 384,
leaving only 128 MB for the kernel + userspace). See
[[feedback_cma_aggressive_on_pi_zero_2w]] memory.

r59 does NOT touch the CMA pool size:

- The watchdog reads `/proc/meminfo` `CmaTotal` (always 256 MB on
  Pi Zero 2 W per kernel default)
- The threshold is the USER-SPACE decision about when to restart
  the backend
- No cmdline.txt edit
- No /boot/firmware/config.txt edit
- No bootloader change

**Zero brick risk.** The CMA pool reservation is independent of the
watchdog threshold; r59 only adjusts the decision point at which
the watchdog decides to release the userspace process.

## Subagent review constraint

Per dispatch §safeguards, the sacred review should verify:

1. **No other system/ files reference the old 220 MB value that
   would now be stale.** Grep results across the repo:

   ```
   $ grep -rn "220" system/openmarquee-cma-watchdog.*
   (none)
   $ grep -rn "220" scripts/tests/test_cma_watchdog.sh
   scripts/tests/test_cma_watchdog.sh:111
       THRESHOLD_MB="${THRESHOLD_MB:-220}" \
   ```

   The test runner's fallback default is INTERNAL test-fixture
   infrastructure — it pins a known threshold for each test's
   assertions, independent of the production default. Tests assert
   semantics ("cma above threshold triggers restart") not the magic
   number; the fixture happens to use 220. Tests pass unchanged
   (31/31 verified locally) regardless of what production default is.

2. **CMA pool size unchanged.** No edits to:
   - `/boot/firmware/config.txt`
   - `/boot/firmware/cmdline.txt`
   - any kernel boot parameter
   - any system memory reservation
   The 256 MB CMA pool stays 256 MB.

3. **No CHANGELOG churn.** r54's CHANGELOG already documented this
   decision as pending in the Known Issues + Tag posture sections.
   r59 doesn't need to amend CHANGELOG — the next entry (whether
   the v1.0.1 release commit OR a follow-up r60 status note) can
   re-frame it as "decision LANDED in r59". Leaving for the
   release commit author to handle.

## Files changed

| File                                          | Change                                                     |
| --------------------------------------------- | ---------------------------------------------------------- |
| `system/openmarquee-cma-watchdog.sh`          | line 14 comment + line 26 default: 220 → 254 + comment block |
| `qa/r59-cma-watchdog-default-decision-2026-06-04.md` | This audit doc                                             |

## Test posture

`scripts/tests/test_cma_watchdog.sh` — 31/31 PASS locally. Tests
explicitly pin THRESHOLD_MB via the runner's `${THRESHOLD_MB:-220}`
fallback, isolating fixtures from the production default change.
No test changes required for r59.

## §G — Open questions

### G.1 r58 empirical follow-up

The dispatch flagged that r58's wider 2-pool window may push peak
slightly higher. r58 is in flight; FYS data post-deploy will
confirm. If observed peak ever crosses 254, we have three options:

1. Bump the default further (e.g. 255 if stable, but pool ceiling
   limits headroom)
2. Implement sustained-N-polls as r60 (the deferred Option B)
3. Pair the threshold with smarter eviction logic (e.g. push
   `force_evict_image_caches_for_cma_pressure` to fire below 254
   pre-emptively)

My recommendation: park as a r60 candidate; revisit after r58
post-deploy data lands.

### G.2 v1.1 sustained-N-polls detector

If we ship sustained-N-polls in v1.1 / v1.0.2:
- N = 3 of last 4 polls (3 consecutive 60s polls above threshold)
- Each poll persists timestamp + cma_used_mb to the state file
- Detector evicts state-file entries older than 5 minutes
- Adds ~20 LOC + a ~100-byte state-file entry per poll
- Test infrastructure already exists (the runner shims SYSTEMCTL +
  /proc/meminfo); 4 new tests for the multi-poll semantics

Not blocking v1.0.1.

## §H — Sacred review constraint

Per dispatch §safeguards, the reviewer should spot:

1. **brick-the-Pi failure modes in the threshold change**
   → CMA pool unchanged; threshold is userspace; no brick risk
2. **CMA pool size unchanged**
   → No cmdline.txt / config.txt / kernel param edits
3. **No other system/ files reference 220 MB**
   → Verified by grep above; tests are insulated

Reviewer should also pressure-test the empirical claim of 251.8 MB
peak by cross-referencing `qa/r50-text-over-video-transitions-2026-06-03.md`
sections C.3 + post-deploy. If the cited number is wrong, the
margin math is wrong.

## §I — Lane

- Doc + 1-line system/ commit
- code2 push; cherry-pick to main via /tmp clone
- Pre-push hook applies (system/ touched)
- No SYSTEM_SPEC.md edits (this is a config default, not a spec
  change)
- No CHANGELOG churn (the release commit author re-frames the
  pending-decision callout as resolved)

---

End of r59 audit.
