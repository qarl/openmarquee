# First-light boot failure root-cause (2026-09-21)

Handoff: Jimmy-openmarquee-code → admin. Why the openMarquee pi-gen image
(branch `task/fireplacesign-burn-prep-2026-09-16` @ 8d770d5, arm64/trixie)
does NOT boot on a confirmed-good Pi Zero 2 W, while stock Raspberry Pi OS
Lite arm64 boots fully on the same Pi.

## VERDICT: `cma=320M` (memory config) — admin's lead #2. Lead #1 (branch) DISCONFIRMED.

## Decisive evidence — diff of OUR non-booting image vs the STOCK booting image (boot partitions)

- **`kernel8.img` is BYTE-IDENTICAL** — sha256 `c9f5af153236ec42eb012700e39ff43d02bc7c5f2ab71abad6da1f20880343e0` on BOTH. Same size 10192324.
- **`bcm2710-rpi-zero-2-w.dtb` identical** — 33708 bytes both.
- **Firmware differs only trivially** — `start4.elf` ours 2298912 / stock 2298560 (~352B), `fixup4.dat` 5510/5512 (2B). A normal trixie firmware point-version delta, NOT an incompatibility.
- ⇒ **The pi-gen `arm64` branch, the kernel, the DTB and the firmware are all fine.** Admin's lead #1 (legacy-branch kernel/firmware mismatch) is dead. (The `arm64` branch is also current: HEAD 74d08a3 2026-09-16, stage0 requires RELEASE=trixie.)

## The ONLY boot-affecting difference is our config

`config.txt` / `cmdline.txt` additions vs stock:
- **MEMORY: `cma=320M` (cmdline) + `gpu_mem=128` (config)**  ← the culprit
- USB gadget: `[all] dtoverlay=dwc2,dr_mode=peripheral` + `modules-load=dwc2,g_ether`
- Cosmetic: `disable_splash=1`, `quiet splash plymouth.ignore-serial-consoles`, `cfg80211.ieee80211_regdom=US`

Stock `cmdline.txt` has **no `cma=`** and boots.

### Why `cma=320M` is the cause
- 512MB Pi Zero 2 W: `gpu_mem=128` → 384MB ARM; `cma=320M` reserves 320MB → ~**64MB normal (non-CMA) ARM RAM**.
- `cma=320M` is documented to OOM-brick this board (memory `feedback_cma_budget_pi_zero_2w_aggressive_brick`: **384M bricks, 256M safe**).
- Symptom match is exact: kernel dies in early memory init, **before** the framebuffer console → blank HDMI (firmware rainbow only), **zero kernel output even with `quiet` removed** (fbcon inits too late to ever print), solid ACT LED, never reaches userspace (no usb0 tether, no wifi). Reproducible on 2 cards.
- This is precisely the failure class nspawn `--boot` + file-presence checks CANNOT catch — they never execute firmware→kernel with the real CMA reservation.

## FIX
In `images/openmarquee/stage-openmarquee/02-boot-config/boot-config-lib.sh` → `patch_cmdline_txt_cma`: change the appended value **`cma=320M` → `cma=256M`** (documented-safe). Keep `gpu_mem=128` and the dwc2 gadget lines (both fine; dwc2 is a standard overlay, very low boot-suspicion).
- Add a regression test asserting the built cmdline has `cma` ≤ 256M (non-vacuous: a real stock cmdline in, assert the value).
- If 256M is still marginal on this arm64/trixie **cold first-boot**, fall to `cma=128M` (boot + boot-card need very little CMA; full video decode CMA can be tuned up once it boots and we measure peak `cma_used` with ≥50MB headroom).

## Memory to correct
Code memory `feedback_cma_aggressive_on_pi_zero_2w` says "use cma=320M". That is WRONG for a fresh arm64/trixie COLD first-boot — 320M was likely validated on an already-running/updated sign (which booted with defaults first) or a different arch/release, never on a cold pi-gen first-boot. Will update.

## Confidence + caveat
HIGH: byte-identical kernel + config-only diff + documented cma-brick + exact symptom. Cannot boot-test from this host; QEMU would NOT faithfully reproduce the Pi VideoCore memory split, so it is not a reliable pre-check — the real proof is the re-flash + Pi boot. Secondary suspect if the cma fix alone doesn't boot: the `dwc2` overlay (isolate next by testing a build with cma-fixed + dwc2 lines removed).

## Next (HELD for admin GO + fleet-safety disk check)
cma 320→256M + regression test + subagent review + commit → rebuild image under df-watchdog → hand back for flash.
