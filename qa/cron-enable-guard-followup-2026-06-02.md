# Cron enable-guard follow-up (defense for r38c daily-restart drop-in)

**Author:** jimmy:openmarquee-code2
**Date:** 2026-06-02
**Status:** SHIPPED on code2; cherry-picked to main
**Predecessor:** [r38c CMA-pressure watchdog + daily restart](r38c-cma-pressure-watchdog-2026-06-02.md) (audit §F.5)
**Naming note:** Originally tagged r38d before QA reassigned r38d to the SIGUSR1 cache-dump dispatch. This is an unnumbered ride-along.

## A. Problem

r38c shipped `/etc/cron.d/openmarquee-daily-restart` as the belt-
and-braces companion to the 60s CMA-pressure watchdog. The cron
drop-in only fires if `cron.service` is enabled + active. If a
shipping Pi has cron disabled for any reason — pi-gen substage
mask, operator override, future Debian image hardening — the daily
restart silently never runs. Watchdog still catches fast drift;
slow drift becomes a multi-day wedge.

r38c audit §F.5 noted this failure-mode as defense-in-depth follow-
up. QA's ACK after r38c surfaced it as a ride-along candidate.

## B. Fix shape

New install.sh §3d block runs after §3c (cron drop-in stage). It:

1. Probes for cron daemon presence:
   - `command -v cron` (Debian/trixie default) OR
   - `command -v crond` (RHEL-family / minimal images)
   - If neither: loud `say` warning + continue. Watchdog still
     functional; only the daily safety net is lost.
2. Checks `systemctl is-enabled cron.service`. Three states:
   - `enabled` (or `enabled-runtime`, `alias`, `static`, `indirect`)
     → no-op. Already wired in.
   - `disabled` → run `systemctl enable cron.service` + log. The
     cron drop-in itself doesn't trigger a daemon reload; that's
     handled by cron's own /etc/cron.d watch (or, conservatively,
     by the existing §8 daemon-reload + start sequence).
   - `masked`, `not-found`, or anything else → loud warning + no
     attempt to unmask. Operator-deliberate decision should not be
     silently reverted; the loud warning surfaces the implication
     for the daily restart.

DRY_RUN-safe via the existing `run` wrapper. Idempotent — re-runs
of install.sh on a Pi with cron already enabled are no-ops.

## C. What I chose NOT to do

### C.1 Don't force-unmask cron

If an operator masked cron deliberately (e.g. to run a different
scheduler), silently unmasking it would surprise them. Loud warning
+ no action is the right surface.

### C.2 Don't replace cron drop-in with a systemd timer

A cleaner long-term fix would be converting
`openmarquee-daily-restart.cron` to a `openmarquee-daily-restart.
{service,timer}` pair — same pattern as the watchdog, no cron
dependency. But:

- QA accepted r38c as-shipped ("ship as written"). Refactoring the
  delivery vehicle mid-stream is presumptuous.
- The cron drop-in is correct + works as soon as the guard ensures
  cron is enabled.
- If qarl wants the systemd-timer rewrite, that's a clean separate
  r38e dispatch.

### C.3 Don't enable cron in §8 instead of §3d

§8 enables openmarquee-specific units. cron.service is a system
package's own unit. Threading it into §8 would mix lanes. §3d is
the right surface — paired with the cron drop-in install.

## D. Failure modes

### D.1 cron not installed at all

- `command -v` for both binaries returns 1.
- Loud warning emitted: "cron daemon not installed; daily restart
  drop-in at /etc/cron.d/openmarquee-daily-restart will not fire.
  Watchdog at openmarquee-cma-watchdog.timer still functional."
- install.sh continues; the cron file is staged but inert.
- Operator can install via `apt install cron` and re-run install.sh
  to wire up.

### D.2 cron installed but disabled

- `systemctl is-enabled cron.service` returns `disabled`.
- Run `systemctl enable cron.service` (queues for next boot).
- Note: `enable` only — not `enable --now`. The cron drop-in is
  already on disk; cron will pick it up on its next scan-tick
  after daemon start, which happens at the next boot (or operator-
  initiated `systemctl start cron.service`).
- If the operator wants the cron live this boot, they can
  `systemctl start cron.service` manually.

### D.3 cron masked

- `is-enabled` returns `masked`.
- Loud warning. NO unmask attempt.
- install.sh continues. Cron drop-in inert until operator unmasks.

### D.4 cron is running but is-enabled returns weird state

- `static`, `indirect`, `alias`, `enabled-runtime` — all treat as
  "OK, no action needed." These reflect already-wired states.

## E. Files modified

| File                | Change                                            |
| ------------------- | ------------------------------------------------- |
| `scripts/install.sh` | New §3d block after §3c (~45 LOC); see G.1 below  |

## F. Test

The watchdog shell test
([scripts/tests/test_cma_watchdog.sh](../scripts/tests/test_cma_watchdog.sh))
does not need updates — this is install.sh-only and doesn't change
watchdog behavior.

A shell test for install.sh §3d is impractical at this layer (it
requires `command -v` shimming + systemctl shimming + DRY_RUN
threading + the rest of install.sh's preamble). The block is small,
the logic is straightforward, and the loud-warning paths are
visible at install-time. Manual verification on FYS is the
acceptance gate.

## G. Implementation diff

### G.1 install.sh new §3d

After the §3c cron drop-in stage, insert:

See `scripts/install.sh` for the canonical block. Behavior:

```
say "Ensure cron daemon enabled (for §3c daily-restart drop-in)"
if command -v cron OR command -v crond:
    cron_state := systemctl is-enabled cron.service 2>&1 || "unknown"
    case cron_state:
        enabled | enabled-runtime | static | indirect | alias |
        linked | linked-runtime | generated:
            no-op
        disabled:
            run systemctl enable cron.service
        masked | masked-runtime:
            loud warn, no action (operator-deliberate)
        *:  # transient, bad, unknown, anything we don't know
            loud warn, no action
else:
    loud warn — cron not installed at all
```

## H. Verification on FYS (post-deploy, code1's lane)

```
# 1. Confirm cron.service is enabled
systemctl is-enabled cron.service     # expect: enabled

# 2. Confirm cron drop-in is picked up on next scan tick
sudo grep openmarquee /var/log/syslog | tail -5
# expect (after a few minutes of cron-daemon-scan):
#   reloading /etc/cron.d/openmarquee-daily-restart

# 3. (Optional) Verify install.sh §3d output on re-run
sudo bash /opt/openmarquee/scripts/install.sh 2>&1 | grep -A 2 "Ensure cron"
# expect: "cron.service is enabled; no action needed"
```

## I. Open questions for qarl

### I.1 Should the guard ALSO `--now` enable cron?

**Current behavior:** `systemctl enable cron.service` only. The cron
drop-in is staged; cron will pick it up at next-boot start (or
operator-manual `systemctl start cron.service`).

**Alternative:** `systemctl enable --now cron.service` — wires up
the cron drop-in immediately at install time.

**Recommendation:** `enable` only (current). Reasons:
- §8 already runs unit start/restart for openmarquee-specific units;
  cron is a system-level concern best left for next boot or
  operator decision.
- If cron was deliberately stopped (rare but real — pi-gen
  intermediate-build state), `--now` reverses that decision.

Override if qarl wants `--now` semantics.

### I.2 Should `masked` also unmask?

**Current behavior:** loud warn, no unmask.

**Recommendation:** keep loud-warn-only. Mask is a strong operator
signal ("don't run this"); silently unmasking violates the principle
of least surprise.

Override if qarl thinks masked-cron is always an error state for
us.

## J. Lane

- This is code2's lane: install.sh + audit doc only. No system/
  changes, no Python changes, no renderer changes.
- Pre-push hook gate applies (install.sh changed) — backend ruff,
  backend pytest, renderer cargo + cross-compile. r38c's push went
  clean through all gates; this guard's diff is smaller.
- Cherry-pick to main via /tmp clone per standard pattern (r38c
  needed manual replay due to §3 region offset; this guard lands
  AFTER §3c which is identical between code2 and main now, so the
  cherry-pick should auto-merge cleanly).

---

End of cron-enable-guard audit.
