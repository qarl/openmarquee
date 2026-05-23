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
