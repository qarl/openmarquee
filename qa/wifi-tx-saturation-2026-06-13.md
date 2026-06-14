# fireplacesign wifi-under-TX-saturation watchdog reboot — note, not a fix

**Date:** 2026-06-13
**Status:** DO NOT FIX YET — recorded so it doesn't get lost. Blocks
visual QA capture on FYS via scp/rsync; needs its own dispatch.

## What QA observed

While debugging the 2026-06-13 PRELOAD_MODE regression, code1 and QA
needed to pull capture artifacts off FYS to compare against the
offscreen golden PNGs. Pulling files via `scp` (or `rsync` over ssh)
from a workstation to FYS, with the renderer + backend actively
running and animating transitions, reliably triggers:

1. The wifi link's TX queue saturates as the file transfer + Tailscale
   keep-alives + the renderer's metrics/scanout traffic compete for the
   single 2.4 GHz radio's airtime budget.
2. The wifi-watchdog (the
   `openmarquee-best-wifi.service` / `openmarquee-cma-watchdog.service`
   neighbour) sees connectivity health drop, decides the radio is wedged,
   and reboots the unit.
3. The sign goes down mid-capture. QA loses the artifact AND has to
   wait ~90 seconds for Plymouth → backend → renderer → first paint to
   come back up.

Reproducibility was high — every scp of an ~80 MB artifact during a
live render cycle tripped at least one watchdog reboot. The bug is
NOT specific to today's transition work; today's work just put us in
a workflow that needed file pulls repeatedly.

## What we don't yet know (and need before a fix)

- Which watchdog is firing. `openmarquee-best-wifi.service` and
  `openmarquee-cma-watchdog.service` are the two candidates from
  `system/`; we haven't pinned which is the trigger.
- Whether the TX saturation is really starving the watchdog's health
  probe, or whether the wifi driver itself is going into a recovery
  state and the watchdog is just observing the result.
- Whether making the watchdog tolerant to brief stalls (e.g.
  consecutive-failure threshold, or 5s timeout vs current value)
  would fix it without hiding a real link failure.
- Whether traffic-shaping the file-pull side (rate-limited rsync, or
  shaping via tc on the workstation interface) is a sufficient
  workaround for QA capture.

## Why we're NOT fixing this now

- Out of scope for today's PRELOAD_MODE permanence dispatch.
- Risk of touching the wifi-watchdog without diagnosis: making it
  more tolerant could silently mask a real radio failure on a sign
  that's actually unreachable.
- Workaround for QA capture during the transition arc: code1's
  in-renderer live-preview hook (`OPENMARQUEE_LIVE_PREVIEW_PATH`,
  shipped in `3159789`) writes the PNG to local disk instead of
  requiring an scp pull during render. That's what unblocks today's
  visual-confirm pipeline.

## When to revisit

Open a follow-up dispatch when ANY of:

1. QA needs sustained out-of-band file pulls from FYS for ongoing
   verification (e.g. capturing many transitions for a study) — the
   live-preview workaround becomes insufficient.
2. A customer reports the sign rebooting under similar load
   conditions (e.g. someone updating content over the same wifi
   link). This becomes a real-world stability issue, not just a QA
   workflow blocker.
3. A wifi-driver / kernel update lands that changes the TX-queue
   behaviour and we want to re-baseline.

## Likely investigation order

1. `journalctl -u openmarquee-best-wifi -u openmarquee-cma-watchdog
   --since "$START" --until "$END"` during a reproducer to identify
   which unit fires the reboot.
2. `iw dev wlan0 station dump` snapshots before / during / after the
   scp to see RSSI + TX rate + retry counts.
3. Compare watchdog firing under: scp alone (no render), render alone
   (no scp), both concurrent — to confirm the load is the trigger and
   not something else.
4. Patch shape: most likely a `StartLimitIntervalSec` / hysteresis
   tweak on the watchdog unit + maybe a tc-shaping recipe for QA's
   workstation that ships in `docs/`.

## Cross-references

- `3159789` (live-preview commit) — the today-workaround for visual
  capture that doesn't need network pulls.
- `system/openmarquee-best-wifi.service` — wifi-health unit.
- `system/openmarquee-cma-watchdog.service` — CMA-pool watchdog (less
  likely to be the wifi trigger but neighbour to inspect).
- `feedback_systemd_start_rate_limit_bite_pattern.md` (memory) —
  general systemd watchdog-flapping pattern, not specific to this.
