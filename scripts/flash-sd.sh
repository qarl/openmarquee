#!/usr/bin/env bash
# scripts/flash-sd.sh — write an openMarquee image to an SD card.
#
# DANGEROUS: this dd-overwrites the entire target block device. The
# safety guards below check for the obvious self-foot-shooting patterns,
# but they are NOT a substitute for the operator verifying the target
# device before confirming.
#
# Usage:
#     bash scripts/flash-sd.sh <image> <device>
#     bash scripts/flash-sd.sh /tmp/openmarquee-pi-image-2026-05-11.img.xz /dev/disk4
#     bash scripts/flash-sd.sh /tmp/...img /dev/sdb        # Linux
#     bash scripts/flash-sd.sh --dry-run <image> <device>  # print actions only
#     bash scripts/flash-sd.sh --yes <image> <device>      # skip typed-confirm
#                                                          # (size + system-disk
#                                                          #  guards still fire)
#
# Safety guards (in order):
#   1. Refuse if image file doesn't exist.
#   2. Refuse if device doesn't exist or isn't a block device.
#   3. Refuse if device is the system disk (Mac: /dev/disk0; Linux: the
#      one holding /).
#   4. Refuse devices smaller than 1GB OR larger than 128GB (catches
#      "you meant /dev/sda not /dev/sdb" footguns).
#   5. Print device info + size + first 256 bytes of MBR (if any).
#   6. Require typed confirmation: the operator must type the device
#      path EXACTLY to proceed.
#   7. dd with conv=fsync + status=progress.
#   8. Sync + (Mac: diskutil eject; Linux: sync).

set -euo pipefail

DRY_RUN=0
SKIP_CONFIRM=0
ARGS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)         DRY_RUN=1; shift ;;
        --yes)             SKIP_CONFIRM=1; shift ;;
        --help|-h)         sed -n '2,30p' "$0"; exit 0 ;;
        --*)               echo "unknown flag: $1" >&2; exit 2 ;;
        *)                 ARGS+=("$1"); shift ;;
    esac
done

if [ "${#ARGS[@]}" -ne 2 ]; then
    echo "usage: $0 [--dry-run] [--yes] <image> <device>" >&2
    exit 2
fi

IMAGE="${ARGS[0]}"
DEVICE="${ARGS[1]}"

say() { printf '==> %s\n' "$*"; }
fatal() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf 'DRYRUN: %s\n' "$*"
    else
        "$@"
    fi
}

# --- Guard 1: image exists --------------------------------------------------

if [ ! -f "$IMAGE" ]; then
    fatal "image not found: $IMAGE"
fi

# Extension check fires HERE (before the safety-guard ladder) so a
# typo like `.img.gz` bails immediately rather than wasting confirmation
# theater. dd-supported formats: .img (raw) or .img.xz (xz-compressed).
case "$IMAGE" in
    *.img|*.img.xz) ;;
    *) fatal "unsupported image extension: $IMAGE (expected .img or .img.xz)" ;;
esac

# --- Guard 2: device exists + is a block device -----------------------------

# On macOS /dev/diskN are character devices but appear as block to BSD
# tools. Use `test -b` on Linux and `test -e` on macOS as a fallback.
UNAME=$(uname -s)
case "$UNAME" in
    Linux)
        if [ ! -b "$DEVICE" ]; then
            fatal "not a block device: $DEVICE"
        fi
        ;;
    Darwin)
        if [ ! -e "$DEVICE" ]; then
            fatal "device not found: $DEVICE"
        fi
        # On Mac, prefer /dev/rdiskN (raw, faster) over /dev/diskN.
        if [[ "$DEVICE" == /dev/disk* && "$DEVICE" != /dev/rdisk* ]]; then
            RAW_DEVICE="${DEVICE/\/dev\/disk//dev/rdisk}"
            say "macOS hint: using raw device ${RAW_DEVICE} (10-20x faster)"
            DEVICE="$RAW_DEVICE"
        fi
        ;;
    *)
        fatal "unsupported platform: $UNAME"
        ;;
esac

# --- Guard 3: refuse the system disk ----------------------------------------

case "$UNAME" in
    Linux)
        # Find the disk holding /. Sample: /dev/sda2 -> /dev/sda.
        ROOT_SRC=$(findmnt -no SOURCE /)
        ROOT_DISK=$(echo "$ROOT_SRC" | sed -E 's/[0-9]+$//')
        if [ "$DEVICE" = "$ROOT_DISK" ]; then
            fatal "REFUSED: $DEVICE is the system disk (holds /)"
        fi
        ;;
    Darwin)
        # /dev/disk0 is canonically the boot drive on Mac.
        if [ "$DEVICE" = "/dev/disk0" ] || [ "$DEVICE" = "/dev/rdisk0" ]; then
            fatal "REFUSED: $DEVICE is the Mac system disk"
        fi
        ;;
esac

# --- Guard 4: device size sanity (1GB <= size <= 128GB) ---------------------

# An SD card for a Pi is 4GB-64GB typical. <1GB = wrong device; >128GB
# = probably an external HDD, refuse to be safe (operator can override
# in source if they really need a bigger card).
case "$UNAME" in
    Linux)
        DEVICE_BYTES=$(blockdev --getsize64 "$DEVICE" 2>/dev/null || echo 0)
        ;;
    Darwin)
        # diskutil info reports "Disk Size" in bytes via parseable form.
        DEVICE_BYTES=$(diskutil info "$DEVICE" 2>/dev/null | \
                       awk -F'[()]' '/Disk Size:/ {print $2}' | \
                       awk '{print $1}' | head -1)
        DEVICE_BYTES=${DEVICE_BYTES:-0}
        ;;
esac

MIN_BYTES=$((1024 * 1024 * 1024))           # 1 GB
MAX_BYTES=$((128 * 1024 * 1024 * 1024))     # 128 GB

if [ "$DEVICE_BYTES" -lt "$MIN_BYTES" ]; then
    fatal "REFUSED: $DEVICE is ${DEVICE_BYTES} bytes (<1GB); wrong device?"
fi
if [ "$DEVICE_BYTES" -gt "$MAX_BYTES" ]; then
    fatal "REFUSED: $DEVICE is ${DEVICE_BYTES} bytes (>128GB); external HDD?"
fi

# --- Guard 5: print device info --------------------------------------------

say "Target device:"
say "  Path:  $DEVICE"
say "  Size:  ${DEVICE_BYTES} bytes ($((DEVICE_BYTES / 1024 / 1024 / 1024)) GB)"
say "  Image: $IMAGE ($(stat -f%z "$IMAGE" 2>/dev/null || stat -c%s "$IMAGE") bytes)"
case "$UNAME" in
    Darwin)
        diskutil info "$DEVICE" 2>/dev/null | head -10 || true
        ;;
    Linux)
        lsblk -do NAME,SIZE,MODEL,SERIAL "$DEVICE" 2>/dev/null || true
        ;;
esac

# --- Guard 6: typed confirmation -------------------------------------------

if [ "$SKIP_CONFIRM" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
    echo
    echo "This will OVERWRITE all data on $DEVICE."
    # Print the EXACT expected string so the operator isn't surprised
    # by the silent /dev/disk -> /dev/rdisk macOS rewrite. Quote it
    # to make whitespace visible.
    echo "Type '$DEVICE' to confirm (anything else aborts):"
    read -r CONFIRM
    if [ "$CONFIRM" != "$DEVICE" ]; then
        fatal "confirmation mismatch; aborting"
    fi
fi

# --- 7. Decompress + dd ----------------------------------------------------

# On macOS, dd needs sudo for raw devices. On Linux, dd needs sudo for
# block devices owned by root. Use sudo unconditionally for portability.
say "Flashing... (will take 5-15 min on a USB 3 SD reader)"
case "$IMAGE" in
    *.xz)
        if [ "$DRY_RUN" -eq 1 ]; then
            printf 'DRYRUN: xz -dc %s | sudo dd of=%s bs=4M conv=fsync status=progress\n' \
                   "$IMAGE" "$DEVICE"
        else
            xz -dc "$IMAGE" | sudo dd of="$DEVICE" bs=4M conv=fsync status=progress
        fi
        ;;
    *.img)
        run sudo dd if="$IMAGE" of="$DEVICE" bs=4M conv=fsync status=progress
        ;;
    # No fallthrough case here -- the early extension check at script
    # top rejects unsupported formats before we reach the safety ladder.
esac

# --- 8. Sync + eject -------------------------------------------------------

say "Sync"
run sudo sync

case "$UNAME" in
    Darwin)
        say "Eject"
        # The non-raw device is what diskutil accepts.
        EJECT_DEVICE="${DEVICE/\/dev\/rdisk//dev/disk}"
        run diskutil eject "$EJECT_DEVICE"
        ;;
esac

say "Done. SD card is ready -- insert into Pi and power on."
