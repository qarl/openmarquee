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
# Handover reconcile 2026-07-09 (GAP2): cma=320M — the validated
# live-sign value (was 256M on the earlier pi-gen path). Relative
# to the 384M old default: cma shrinks 64M (frees 64M to ARM) and
# gpu_mem grows 64M (takes it back), so ARM-side is net unchanged,
# within budget on a 512MB Zero 2 W. See patch_cmdline_txt_cma in
# boot-config-lib.sh for the value + idempotency contract.
patch_config_txt_gpu_mem  "${boot_dir}/config.txt"
patch_cmdline_txt_cma     "${boot_dir}/cmdline.txt"
# HDMI audio 2026-07-01 (qarl decision, locked): the vc4hdmi ALSA
# card is exposed by vc4-kms-v3d + dtparam=audio=on. Trixie ships
# the line commented; live production sign already has it
# uncommented, so bake the same shape into the SD-card image + the
# redeploy path.
patch_config_txt_audio    "${boot_dir}/config.txt"
