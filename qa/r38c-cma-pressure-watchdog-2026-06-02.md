# r38c — CMA-pressure watchdog + daily restart (stopgap until r38b lands)

**Author:** jimmy:openmarquee-code2
**Date:** 2026-06-02
**Status:** SHIPPED on code2; cherry-picked to main
**Predecessors:** r37 CMA allocator static audit ([qa/r37-cma-allocator-leak-audit-2026-05-31.md](r37-cma-allocator-leak-audit-2026-05-31.md))
**Companion:** r38b (code1's deep-read of 4 SUSPECT GBM scanout paths — the actual fix lane)

## A. Why a watchdog, not a `cma=` raise

Per r37 G.5 my own recommendation was **NO** to raising `cma=` in the
boot cmdline. qarl validated this empirically on 2026-05-31 evening:
bumping `cma=384M` on the Pi Zero 2 W (512 MB total RAM) starved the
kernel + userspace + Tailscale of headroom and the box wedged at boot
([cma=384M too aggressive on Pi Zero 2 W (512MB)](../../.claude/projects/-Users-qarl-project-openmarquee-code/memory/feedback_cma_aggressive_on_pi_zero_2w.md)).

A pressure watchdog is the better stopgap because:

1. **No change to boot allocation.** Boot CMA stays at 256 MB; total
   physical-RAM math is unchanged. No risk of OOM-at-boot.
2. **Bounded uptime.** When `CmaUsed` approaches the ceiling, we
   recycle the leaker (backend service + its renderer subprocess)
   BEFORE the wedge. Steady-state correctness is preserved; the cost
   is a brief sub-second blip of the sign at restart.
3. **Belt-and-braces with a daily cron.** Watchdog catches fast drift
   (load-driven); daily cron at 03:00 catches slow drift that never
   crosses the threshold. Two independent failure-modes, two
   independent guards.
4. **Reversible without flashing.** When r38b fixes the actual leak,
   the watchdog can be disabled by `systemctl disable
   openmarquee-cma-watchdog.timer` + rm the cron drop-in. No image
   rebuild.

This doc is the audit + ship surface. The actual leak fix is r38b's
lane; this is the **bridge** until that lands.

## B. Architecture decision — single long-running unit vs timer + oneshot

Two viable shapes:

### B.1 Single long-running unit (rejected)

```
[Unit]
Description=openMarquee CMA-pressure watchdog
[Service]
Type=simple
ExecStart=/opt/openmarquee/system/openmarquee-cma-watchdog.sh --loop
Restart=always
RestartSec=10s
```

`--loop` runs `while true; do check; sleep 60; done` in process.

- **Pros:** one process to introspect; simple.
- **Cons:** in-process state (last-restart timestamp) is lost on
  watchdog crash, defeating cooldown. A hang inside `check` (e.g.
  `systemctl restart` blocked on a stuck unit) wedges the watchdog
  itself. Recovery needs an external watchdog-of-the-watchdog, which
  is circular.

### B.2 Timer + oneshot service (chosen)

```
openmarquee-cma-watchdog.timer    OnUnitActiveSec=60s
openmarquee-cma-watchdog.service  Type=oneshot, ExecStart=...
```

The oneshot:
1. Reads `CmaUsed` from `/proc/meminfo`.
2. If above threshold, checks cooldown state file. If outside
   cooldown, `systemctl restart openmarquee-backend.service` +
   writes new state file.
3. Exits.

- **Pros:** canonical systemd pattern (`systemctl list-timers`
  introspectable). State persists in `/var/openmarquee/
  cma-watchdog-state` across watchdog process death. RuntimeMaxSec on
  the service kills a wedged oneshot without leaking processes. Each
  invocation is independent.
- **Cons:** more files (3 vs 1). Slightly chattier journald.

**Chosen: B.2** — failure modes are bounded by systemd's own
oneshot/timer machinery rather than relying on a long-running process
to self-recover.

## C. Threshold rationale

### C.1 Empirical anchors (from code1's r35 D.2)

```
cma_total          = 262144 KB = 256.0 MB  (Pi Zero 2 W default carveout)
cma_used_peak      = 261939 KB = 255.8 MB  (immediately pre-wedge)
cma_used_baseline  ≈ 191488 KB ≈ 187.0 MB  (post-sidecar-restart)
drift              ≈  70451 KB ≈  68.8 MB  (~70 MB leak per uptime cycle)
```

### C.2 Threshold options

| Threshold | Headroom-to-ceiling | Margin-above-baseline | Pros / cons                                                                                                  |
| --------- | ------------------- | --------------------- | ------------------------------------------------------------------------------------------------------------ |
| 200 MB    | 56 MB               | 13 MB                 | Tight margin to baseline — single transition burst could trip immediately after restart. **Restart-loop risk.** |
| 210 MB    | 46 MB               | 23 MB                 | Better margin, still relatively tight.                                                                       |
| **220 MB**| **36 MB**           | **33 MB**             | **Recommended.** 33 MB above baseline matches a comfortable single-paint-cycle BO churn (~8-16 MB per scanout flip × 2-3 cycles in flight). 36 MB headroom before ceiling tolerates a single large alloc spike between watchdog ticks. |
| 230 MB    | 26 MB               | 43 MB                 | Safer baseline margin, narrower ceiling headroom.                                                            |
| 240 MB    | 16 MB               | 53 MB                 | Most tolerance for noisy growth; ceiling headroom is 1-2 paint cycles only. Risk: a spike between ticks blows the ceiling before the watchdog fires. |

**Chosen default: 220 MB.** Configurable via
`/etc/default/openmarquee-cma-watchdog` (THRESHOLD_MB=).

The threshold sits 33 MB above the post-restart baseline (a comfortable
single-paint-cycle BO churn budget) and 36 MB below the ceiling. r37
estimates GBM BO size at 8-16 MB per scanout-flip; the watchdog has 2-3
paint cycles worth of headroom between detection and wedge.

### C.3 Why not a percentage?

```
220 / 256 = 85.94%
```

Watchdog computes raw MB and compares against `THRESHOLD_MB`. Percent
is a derived display value; MB is the actionable threshold. If the CMA
carveout ever changes (e.g. Pi 4 with `cma=512M` for higher-RAM
shipping Pis), `THRESHOLD_MB` is re-tuned per-target — a percentage
would be wrong in both directions (256→512 doubles the absolute
margin even if utilization fraction is unchanged).

## D. Cooldown rationale

The cooldown is the **min interval between restarts**, not between
watchdog checks.

### D.1 Why a cooldown at all?

Without cooldown, a fast leak ( > 70 MB / minute) would trigger
restart-after-restart in a tight loop:

```
t=0:    cma_used = 230 MB → restart → 187 MB
t=60s:  cma_used = 222 MB → restart → 187 MB  (if leak rate exceeds 35 MB/min)
t=120s: cma_used = 224 MB → restart → 187 MB  ...
```

This would surface as continuous sub-second blips on the sign, exceed
the backend.service `StartLimitBurst=5` ([openmarquee-backend.service:20-21](../system/openmarquee-backend.service)),
and trip the `StartLimitIntervalSec=300` rate-limit. The unit
would then refuse to restart further — exactly the wedge the watchdog
exists to prevent.

### D.2 Cooldown options

| Cooldown | Recovery semantics                              | Pros / cons                                                                                                   |
| -------- | ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------- |
| 10 min   | Up to 6 restarts/hour                            | Aggressive — would trip backend.service StartLimitBurst if leak is fast.                                      |
| 20 min   | Up to 3 restarts/hour                            | Safer; under StartLimitBurst envelope.                                                                        |
| **30 min**| **Up to 2 restarts/hour**                       | **Recommended.** Comfortably under StartLimitBurst (5 starts / 5 min). For a ~70 MB leak per restart cycle observed in D.2, this caps the user-visible blip rate at 2/hour while still recovering well before any wedge. |
| 60 min   | Up to 1 restart/hour                             | Conservative; if leak rate exceeds ~33 MB/30min the watchdog would let the box wedge between checks.          |

**Chosen default: 30 min** (`COOLDOWN_SEC=1800`).

For the observed ~70 MB drift per uptime cycle, restart frequency is
bounded at 1/cycle, so 30 min cooldown is not the binding constraint —
leak rate is. The cooldown's role is to PREVENT runaway loops if leak
rate accelerates, not to throttle steady-state restarts.

### D.3 Interaction with daily restart

The daily cron at 03:00 runs `systemctl restart
openmarquee-backend.service` unconditionally, ignoring the watchdog
cooldown (they don't share state). On a healthy day, the watchdog
never fires; only the cron does. On a leaky day, both may fire — but
they're independent surfaces, and the worst case is one extra blip
per day, which is acceptable.

## E. Restart target — which unit?

Dispatch quote:

> ### A. Watchdog daemon shape
> Polls /proc/meminfo for CmaUsed every N seconds...
> `systemctl restart openmarquee-sidecar` (or whatever the actual
> sidecar service name is — verify from FYS systemd, don't guess)

### E.1 Audit of FYS service surface

The Rust renderer ("sidecar" in the dispatch's language) is **not** an
independent systemd unit. It is a subprocess of
`openmarquee-backend.service`, spawned by the FastAPI lifespan via
`OPENMARQUEE_RENDERER=auto` +
`OPENMARQUEE_RENDERER_BINARY=/usr/local/bin/openmarquee-render`
([openmarquee-backend.service:68-69](../system/openmarquee-backend.service)).

When the backend service is restarted:
1. systemd sends SIGTERM to the uvicorn process (the systemd unit's
   main PID).
2. uvicorn's lifespan teardown runs — calls the renderer subprocess's
   stop hook, which IPC-shuts-down the Rust process.
3. The Rust subprocess exits, releasing all GBM BOs, EGLImage refs,
   V4L2 buffer pool, GLES textures, and DMABUF imports.
4. The kernel reclaims the CMA pages backing each.
5. uvicorn exits.
6. systemd starts a fresh `openmarquee-backend.service`, which spawns
   a new renderer subprocess from scratch.

The leak is in the **renderer process address space**, but the
**only correct release surface is the backend service unit** because
that's what owns the renderer subprocess's lifecycle.

### E.2 Why not `pkill openmarquee-render`?

Tempting alternative: kill the renderer subprocess directly, let the
backend's lifespan-spawn-renderer logic respawn it without a uvicorn
restart. Two problems:

1. The backend's renderer-supervisor lifecycle (in
   `openmarquee/playback/renderer_session.py` per memory recall) was
   built for crash recovery, not external kills. Behavior under
   external SIGKILL of the subprocess is not characterized; could
   leave the backend in an unhealthy state (zombie supervisor, IPC
   socket leftover, etc.).
2. The chromium subprocess leak fix
   ([feedback memory](../../.claude/projects/-Users-qarl-project-openmarquee-code/memory/project_chromium_subprocess_leak_fix.md))
   established `start_new_session` + `killpg` as the correct pattern
   for multi-proc subprocess management; ad-hoc pkill is the wrong
   surface.

**Chosen: `systemctl restart openmarquee-backend.service`.** The
backend is the owning systemd unit; restart of that unit is the only
well-defined surface for releasing the renderer's CMA-backed
allocations.

### E.3 Cost: a restart is ~3-8 seconds of black screen

From operational memory, a full backend restart takes ~3-8 seconds
(uvicorn shutdown + spawn + lifespan init + first renderer IPC paint).
Watchdog fires ~2x/hour at worst-case leak rate → ~16 seconds of
sub-second blips per hour. Acceptable for a sign with 1-2 minute
slide cadence; a single transition takes ~750ms.

## F. Failure modes

### F.1 Watchdog process crash mid-check

- systemd's oneshot Type=oneshot semantics: if the script exits
  non-zero, the next .timer firing still triggers a fresh oneshot.
  No carry-state, no zombie process.
- The state file's last-restart timestamp persists. If a crash happens
  BEFORE the restart fires, the next firing re-evaluates and may
  trigger. If a crash happens AFTER the restart fires (in the state
  file write), the next firing reads a stale/missing timestamp and
  treats it as "no recent restart" — worst case, an extra restart
  within 60s. Acceptable.

### F.2 `systemctl restart openmarquee-backend.service` hangs

- The watchdog runs `systemctl restart` with `--no-block` to avoid
  blocking the oneshot. systemd handles the restart's own timeout
  (backend.service has no explicit `TimeoutStartSec`, so it uses the
  systemd-wide default — 90s).
- If a restart genuinely hangs (backend's lifespan deadlocks), the
  cooldown still prevents repeat attempts. Operator intervention via
  journalctl + manual `systemctl reset-failed` is the recovery path
  per the existing backend.service preamble comment block
  (around lines 13-21 in openmarquee-backend.service).

### F.3 `/proc/meminfo` unreadable or CmaTotal missing

- Script defaults to `CMA_USED_MB=0` if either CmaTotal or CmaFree
  parses to empty. 0 ≤ THRESHOLD so no restart fires.
- Emits a journald warning (`cma-watchdog: /proc/meminfo CmaTotal
  unreadable`) so operators know the watchdog is non-functional on
  this kernel rather than silently passing.
- This is the same defensive posture as `mem.rs`'s `read_linux`
  returning `None` on unreadable files ([mem.rs:46-58](../renderer/src/mem.rs)).

### F.4 State file corrupted / unparseable

- Script treats unparseable state as "no prior restart" and proceeds
  with normal check. Worst case: one extra restart within cooldown.
  Self-healing — next state write overwrites the corrupted content.

### F.5 Cron daemon disabled

- Daily restart will not fire; watchdog remains the only guard.
- For a leak rate ≤ 35 MB/30min, the watchdog alone is sufficient
  (it would catch drift well before the ceiling).
- For slower drift the cron exists to catch, the failure surfaces as
  eventual wedge ~36-72 hours later (220 MB - 187 MB baseline = 33
  MB headroom; at slow drift, that's 1-2 days at most).
- Install.sh verifies `systemctl is-enabled cron.service` post-install
  (added).

### F.6 Threshold tuning wrong (too high)

- Watchdog never fires; wedge happens as before.
- Cron continues to restart daily, capping wedge probability at one
  cycle-per-day.

### F.7 Threshold tuning wrong (too low)

- Watchdog fires too often; backend.service StartLimitBurst trips
  after 5 restarts in 5 min. Backend goes failed; manual recovery
  needed.
- Cooldown of 30 min prevents this for THRESHOLD ≥ 200 MB — even if
  the leak immediately tripped on cma_used = THRESHOLD + 1, the next
  restart attempt is gated at 30 min, well clear of the 5 min
  StartLimitIntervalSec.

### F.8 Watchdog races with operator-initiated restart

- Operator manually restarts the backend at t=0. Watchdog state file
  doesn't know about it.
- At t=60s, watchdog reads cma_used = (fresh) ~187 MB → no fire.
- No problem. State file isn't required to track operator restarts
  because the threshold check is the gate; restart-just-happened
  drives cma_used below threshold naturally.

## G. Implementation surface

### G.1 Files added

| File                                                | Purpose                                       | LOC |
| --------------------------------------------------- | --------------------------------------------- | --- |
| `system/openmarquee-cma-watchdog.sh`                | Threshold check + cooldown + restart trigger  | ~70 |
| `system/openmarquee-cma-watchdog.service`           | Oneshot wrapping the .sh                      | ~25 |
| `system/openmarquee-cma-watchdog.timer`             | 60s periodic firing                           | ~15 |
| `system/openmarquee-daily-restart.cron`             | 03:00 daily restart cron drop-in              | ~10 |
| `scripts/tests/test_cma_watchdog.sh`                | Mocked /proc/meminfo + systemctl assertions   | ~120|

### G.2 Files modified

| File              | Change                                                     |
| ----------------- | ---------------------------------------------------------- |
| `scripts/install.sh` | Section 3 unit-copy loop adds 3 new units; Section 3a chmod +x adds watchdog.sh; Section 8 enables timer + installs cron drop-in |

### G.3 No SD bundle script change needed

`scripts/build_sd_bundle.sh` already uses
`rsync -a system/` ([:196-202](../scripts/build_sd_bundle.sh)),
which picks up the 4 new files automatically. Same for `scripts/`.

### G.4 No backend / renderer code changes

This is a pure system-surface stopgap. r38b is the renderer fix lane.

## H. Verification on FYS (post-deploy)

```
# 1. Confirm units installed
ls -la /etc/systemd/system/openmarquee-cma-watchdog.*
ls -la /etc/cron.d/openmarquee-daily-restart

# 2. Confirm timer is active
systemctl is-active openmarquee-cma-watchdog.timer
systemctl list-timers openmarquee-cma-watchdog.timer

# 3. Confirm last-fire of the oneshot
journalctl -u openmarquee-cma-watchdog.service -n 20 --no-pager

# 4. Manual trigger via fake high CmaUsed
sudo bash -c 'echo "CMA_USED_OVERRIDE_MB=250" > /run/openmarquee-cma-watchdog-test'
sudo systemctl start openmarquee-cma-watchdog.service
journalctl -u openmarquee-cma-watchdog.service -n 5 --no-pager
# expect: "cma-watchdog: triggered restart (cma_used=250MB >= 220MB)"
# then verify openmarquee-backend was restarted:
systemctl show openmarquee-backend.service --property=ActiveEnterTimestamp

# 5. Cleanup
sudo rm /run/openmarquee-cma-watchdog-test

# 6. Cron sanity
sudo cat /etc/cron.d/openmarquee-daily-restart  # should show 0 3 * * * line
```

The override file at `/run/openmarquee-cma-watchdog-test` is read by
the watchdog script if present, allowing the operator to inject a
test value without modifying `/proc/meminfo`. `/run` is tmpfs so the
override doesn't persist across reboot. Code1's job to actually run
these on FYS post-deploy; that's the deploy lane.

## I. Open questions for qarl

### I.1 Threshold default

**Recommendation:** 220 MB.

**Question:** Override to 210 MB (tighter) or 240 MB (more tolerance)?

Tighter = more restarts per uptime cycle but smaller wedge risk.
Looser = fewer blips but smaller margin for spike-between-checks.

### I.2 Cooldown default

**Recommendation:** 30 min.

**Question:** Override to 20 min (more aggressive recovery) or 60
min (more conservative)?

### I.3 Watchdog polling interval

**Recommendation:** 60s (`OnUnitActiveSec=60s`).

**Question:** Should this be 30s (catches spikes faster, doubles
journald chatter) or 120s (lighter footprint, larger detection lag)?

### I.4 Daily-restart time

**Recommendation:** 03:00 local time (Pi system TZ is local per
[feedback memory](../../.claude/projects/-Users-qarl-project-openmarquee-code/memory/feedback_pi_system_tz_must_stay_local.md)).

**Question:** Override (02:00, 04:00, etc.)? Any need to avoid
overlap with another scheduled maintenance window?

### I.5 r38b interaction

When r38b lands the actual leak fix:

- Option A: leave watchdog + cron enabled as defense-in-depth (if
  there are 0 restarts in a 7-day soak, the guard is silent — no
  cost).
- Option B: disable both, accept that any future leak class will
  re-emerge as a wedge with no auto-recovery.

**Recommendation:** Option A. The watchdog at 220 MB will never fire
on a healthy renderer (steady-state stays at ~187 MB per D.2). The
daily cron at 03:00 is the only visible cost — a ~3-8 second blip
during the slowest viewer-traffic hour. Acceptable.

### I.6 Disable for non-FYS deployments?

This watchdog is tuned for Pi Zero 2 W with `cma=256M`. For Pi 4 or
other shipping Pis with different CMA carveouts, the threshold needs
re-tuning. install.sh installs unconditionally; should it conditional
on detected hardware? Or just ship-and-let-operator-tune?

**Recommendation:** Ship unconditionally. `THRESHOLD_MB` configurable
via `/etc/default/openmarquee-cma-watchdog`; operator-tunable
post-install. Pi 4 with `cma=512M` operator would set
THRESHOLD_MB=440 to preserve the same 36 MB margin.

### I.7 Should the watchdog also restart when CmaUsed is 100% growing-without-bound but well below threshold?

A monotonic-growth detector would be a more general guard but
requires per-tick state and a baseline notion. Out of scope for
this stopgap; r38b's leak fix removes the need entirely.

## J. Code1 / admin Jimmy follow-ups

- **Code1:** verify the watchdog interaction with r38b's leak fix
  once landed. If r38b is correct, the watchdog should never fire on
  a soak. If watchdog fires post-r38b, that's a regression signal —
  treat as r38b is incomplete or there's a second leak surface.
- **Admin Jimmy:** no outer-repo edit needed for r38c. The watchdog
  is an internal-stability guard; not a spec-level shipping concern.

## K. Push posture

- Doc-only on this PR-equivalent: no, this PR includes system/, scripts/,
  install.sh code changes. Subagent review required per AGENTS.md.
- `--no-verify` will be used only if the NFS-wedge bite pattern recurs
  during commit, per
  [feedback memory](../../.claude/projects/-Users-qarl-project-openmarquee-code/memory/feedback_nfs_wedge_on_pre_push_hook.md).
- Cherry-pick to main via `/tmp/openmarquee-main` clone per the
  standard pattern.

---

End of r38c audit.
