#!/bin/bash -e
# 02-run.sh — boot-config substage runner.
#
# Runs on the build HOST (pi-gen convention) with ${ROOTFS_DIR}
# pointing at the image rootfs. Patches the Pi boot files so the
# plymouth splash from substage 01 owns the screen for the whole
# boot:
#   - config.txt  disable_splash=1  — silences the firmware rainbow
#   - cmdline.txt  quiet splash ... — silences the kernel console
#     text and tells plymouth to show
#
# The patch logic lives in boot-config-lib.sh (unit-tested by
# test-boot-config.sh) — cmdline.txt is a single-line file and a
# botched edit bricks boot, so the logic is isolated + tested.

DIR="$(cd "$(dirname "$0")" && pwd)"
source "${DIR}/boot-config-lib.sh"

# Trixie places the boot partition at /boot/firmware; older layouts
# used /boot. Resolve whichever this rootfs uses; fail loudly (and
# abort the build via `-e`) if neither has the files — far better an
# aborted build than a flashed image with an unpatched / mis-patched
# cmdline.
boot_dir=""
for candidate in "${ROOTFS_DIR}/boot/firmware" "${ROOTFS_DIR}/boot"; do
    if [ -f "${candidate}/cmdline.txt" ] && [ -f "${candidate}/config.txt" ]; then
        boot_dir="$candidate"
        break
    fi
done
if [ -z "$boot_dir" ]; then
    echo "02-run.sh: cmdline.txt + config.txt not found under ${ROOTFS_DIR}/boot[/firmware]" >&2
    exit 1
fi
echo "02-run.sh: patching boot config in ${boot_dir}"

patch_config_txt  "${boot_dir}/config.txt"
patch_cmdline_txt "${boot_dir}/cmdline.txt"
# Postmortem mitigation #5 (2026-05-23): the base Pi OS image
# carries `cgroup_disable=memory` in cmdline.txt, which suppresses
# kernel PSI/cgroup memory accounting + blocks systemd-OOMD
# policies. Strip it so we get memory-pressure telemetry.
strip_cmdline_token "cgroup_disable=memory" "${boot_dir}/cmdline.txt"
# r110 c3.3.2-followup (2026-06-11): bake the GPU memory split
# into the image defaults so a fresh Jason-class deploy boots
# with a reloc heap that can allocate a ril.video_decode
# component. gpu_mem=64 (stock Pi Zero 2 W default) cannot —
# vchiq ETIME on component create, reloc heap starved at
# ~17M/44M idle. gpu_mem=128 restores enough reloc heap.
#
# cma=256M (FIRST-LIGHT FIX 2026-09-21). The 2026-07-09 GAP2 bump to
# cma=320M ("validated live-sign value") bricked a fresh arm64/trixie
# COLD boot on a 512MB Zero 2 W — it had only ever been validated on a
# sign that already booted with a smaller CMA, never cold from a pi-gen
# image. Math on 512MB: gpu_mem=128 -> 384MB ARM; cma=256M -> ~128MB
# normal (non-CMA) RAM = SAFE. cma=320M -> ~64MB normal = OOM-brick
# before the framebuffer console (blank HDMI, zero kernel output, solid
# ACT LED; reproduced on 2 cards, proven config-only by a byte-identical
# kernel8.img diff vs stock). Do NOT re-bump above 256M without a real
# cold-boot test. See patch_cmdline_txt_cma in boot-config-lib.sh + the
# writeup in code/docs/first-light-rootcause-cma-2026-09-21.md.
patch_config_txt_gpu_mem  "${boot_dir}/config.txt"
patch_cmdline_txt_cma     "${boot_dir}/cmdline.txt"
# HDMI audio 2026-07-01 (qarl decision, locked): the vc4hdmi ALSA
# card is exposed by vc4-kms-v3d + dtparam=audio=on. Trixie ships
# the line commented; live production sign already has it
# uncommented, so bake the same shape into the SD-card image + the
# redeploy path.
patch_config_txt_audio    "${boot_dir}/config.txt"
# USB-gadget networking 2026-09-16 (qarl dev device "fireplacesign"):
# present a CDC-ether gadget (usb0) to a host tethered over the USB data
# port so the Pi is reachable as fireplacesign.local over the cable — the
# wired recovery path the Pi Zero 2 W lacks (no onboard ethernet). Both
# lines are required: dtoverlay=dwc2,dr_mode=peripheral in config.txt loads
# the controller in peripheral (gadget) role — NOT the stock [cm4] host
# mode — and modules-load=dwc2,g_ether in cmdline.txt (inserted right after
# rootwait, order matters) binds the gadget driver. Coexists with onboard wlan0
# (SDIO) station mode + HDMI. The usb0 address + avahi advertisement are
# set by substage 05-usb-gadget + system/avahi.
patch_config_txt_dwc2     "${boot_dir}/config.txt"
patch_cmdline_txt_modules "${boot_dir}/cmdline.txt"
