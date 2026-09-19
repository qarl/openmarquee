# First-light failure root-cause (2026-09-19)

Handoff: Jimmy-openmarquee-code → admin. Root-cause of the two bugs admin
found reading the burned fireplacesign card. **Rebuild is HELD** per admin's
instruction (awaiting live serial-console evidence + a confirmed fix list).
Fleet-safety: any pi-gen rebuild needs admin GO + disk check (df, no
concurrent pi-gen build, arm the watchdog) — see memory `feedback_no_concurrent_pigen_builds`.

## Bug 1 — `dtoverlay=dwc2,dr_mode=host` (should be peripheral)

**CONFIRMED** in `images/openmarquee/stage-openmarquee/02-boot-config/boot-config-lib.sh` → `patch_config_txt_dwc2()`.

The idempotency guard greps the **whole file** for any uncommented
`dtoverlay=dwc2...` and no-ops if it finds one:
```
grep -qE '^[[:space:]]*dtoverlay[[:space:]]*=[[:space:]]*dwc2([[:space:]]*$|,)'
```
Stock Trixie `config.txt` ships `[cm4]` and `[cm5]` sections carrying
`dtoverlay=dwc2,dr_mode=host` (Compute-Module host-port config). The function
matches that line and **no-ops → never appends the `[all] dtoverlay=dwc2`
block the Pi Zero 2 W needs.** So the only dwc2 line in the file is the
CM-scoped `dr_mode=host`; nothing ever sets peripheral for the Zero 2 W.

The function's own comment even documents treating
`dtoverlay=dwc2,dr_mode=host` as an already-satisfied match — that *is* the bug.
Admin's read (presence-checked, dr_mode value not enforced) is exactly right.

**Test gap:** `test-boot-config.sh` (the 24 checks QA ran) never asserts
`dr_mode`, so it passed vacuously on this.

**Fix direction (pending admin confirm):**
- Make the dwc2 patch **section-aware**: ignore `[cm4]`/`[cm5]` lines when
  deciding whether a Zero 2 W-applicable dwc2 is present.
- Append `[all]` with `dtoverlay=dwc2,dr_mode=peripheral` — enforce the
  **value**, not just presence. (Peripheral is the canonical Pi-gadget role;
  the Zero 2 W data port doesn't reliably ID-detect for otg.)
- Add a non-vacuous regression test: a stock config with `[cm4] dtoverlay=dwc2,dr_mode=host`
  must end with `dr_mode=peripheral` effective for the Zero 2 W.

## Bug 2 — cloud-init never ran (empty log, stock hostname, no bundle extract)

**CONFIRMED.** `cloud-init` IS apt-installed (`00-install-packages/00-packages`
line 22), but the recipe does neither thing needed to make it run:

1. **Services never enabled.** The only `systemctl enable` of cloud-init lives
   *inside* cloud-init's own `runcmd` (`cloud-init/user-data`) and inside
   `scripts/install.sh` — both of which only run *if cloud-init already ran*
   (chicken-and-egg). No substage `*-run.sh` enables `cloud-init*`, so nothing
   lands in `multi-user.target.wants` — exactly admin's finding. apt-installing
   cloud-init in the pi-gen chroot does not reliably leave the services enabled
   on Debian/RPi-OS.
2. **NoCloud datasource never pointed at the boot partition.** No
   `datasource_list`, `seedfrom`, or `ds=nocloud` anywhere in the repo. Unlike
   Ubuntu's Pi images, Debian/RPi-OS cloud-init does not auto-seed from
   `/boot/firmware`, so the staged `user-data`/`meta-data`/`network-config`
   are never consumed.

Either gap alone → no seed consumed → no bundle extraction → `install.sh`
never runs → black screen. Both match admin's evidence.

**Fix direction (pending admin confirm):** new substage `06-cloud-init` that:
- `systemctl enable cloud-init-local.service cloud-init.service cloud-config.service cloud-final.service` in the chroot; remove any `/etc/cloud/cloud-init.disabled`; unmask if masked.
- Drop `/etc/cloud/cloud.cfg.d/99_openmarquee.cfg` with `datasource_list: [ NoCloud ]`
  and a NoCloud seed pointing at the boot partition (trixie: `/boot/firmware/`).
- Admin's live login can confirm whether the base image ships
  `cloud-init.disabled` or a masked unit that also needs clearing.

## Status
Ready to implement both fixes + regression tests the moment admin confirms the
fix list. Not touching code or building until then.
