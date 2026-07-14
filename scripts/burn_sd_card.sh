#!/usr/bin/env bash
# scripts/burn_sd_card.sh -- Mac-side one-shot SD card flasher.
#
# Collapses "use Pi Imager GUI, then run stage_sd_card.sh" into a
# single CLI command. Validates the target, downloads + caches the
# latest Pi OS Lite arm64 image, flashes it via dd (using the rdisk
# raw variant for ~5x throughput), waits for bootfs auto-mount,
# stages the openMarquee bundle, ejects.
#
# Usage:
#     scripts/burn_sd_card.sh /dev/diskN
#     scripts/burn_sd_card.sh --dry-run /dev/diskN     # validate + plan only
#     scripts/burn_sd_card.sh --help
#
# Optional WiFi pre-config (Phase 4e-b, 2026-05-15):
#     scripts/burn_sd_card.sh --wifi-ssid HomeWifi /dev/diskN
#         # Reads password from $OPENMARQUEE_WIFI_PASSWORD env var or
#         # prompts interactively. Drops an NM keyfile onto bootfs;
#         # openmarquee-firstboot.sh copies it to NM's
#         # system-connections/ on first boot and the Pi joins the
#         # network without going through the AP setup dance.
#     scripts/burn_sd_card.sh --wifi-ssid HomeWifi \
#                             --wifi-password-file ~/.wifi-pass \
#                             /dev/diskN
#         # Reads password from a file (preferred over --wifi-password
#         # for security: --wifi-password PASS shows up in `ps auxww`).
#     scripts/burn_sd_card.sh --wifi-ssid HomeWifi \
#                             --wifi-password 'inline-secret' /dev/diskN
#         # Inline -- WARNING: visible in `ps`. Use env / file in CI.
#
# Optional mgmt-WiFi pre-config (r34, 2026-05-31):
#     scripts/burn_sd_card.sh --mgmt-wifi-ssid InstallerWifi /dev/diskN
#         # Independent of --wifi-ssid: drops a SECOND NM keyfile
#         # pinned to interface-name=wlan-dongle (the udev-renamed
#         # USB-WiFi dongle). When a rt2800usb-family dongle is
#         # plugged in, the Pi auto-joins the installer's WiFi via
#         # the dongle while the built-in radio (wlan0) stays free
#         # for sign-WiFi or the captive-portal AP. See
#         # docs/dual-radio-shipping-test.md for the verification
#         # sweep + qa/r31-dongle-topology-recommendation-2026-05-31.md
#         # for the design.
#         #
#         # Password sources mirror --wifi-password: --mgmt-wifi-password
#         # PASS (inline, ps-visible), --mgmt-wifi-password-file PATH
#         # (preferred), $OPENMARQUEE_MGMT_WIFI_PASSWORD env (CI-safe),
#         # or interactive prompt when stdin is a tty.
#
# Safety:
#   - The target must be a removable / external disk per diskutil
#     info. Internal disks (/dev/disk0 / /dev/disk1 typically) are
#     refused.
#   - The operator must type the EXACT "diskN" identifier to confirm
#     before any destructive operation. No --force / --yes flag.
#   - SIGINT mid-dd re-ejects the card + prints a "card is in an
#     undefined state, re-burn" warning.
#
# macOS version: tested on Sonoma (14) / Sequoia (15). Should work
# on Ventura (13) and later; relies on `diskutil info -plist` (post-
# 10.7) + BSD `dd` (preinstalled). dd progress on BSD comes via
# Ctrl-T (SIGINFO), not status=progress.
#
# Cache:
#   $OPENMARQUEE_BUILD_DIR/cache/pi-os-lite-arm64.img.xz
#   (falls back to ~/Library/Caches/openmarquee/ when OPENMARQUEE_
#   BUILD_DIR is unset)

set -euo pipefail

# ============================================================
# Constants + paths.
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

IMAGE_URL="https://downloads.raspberrypi.com/raspios_lite_arm64_latest"
CACHE_MAX_AGE_DAYS=30

if [ -n "${OPENMARQUEE_BUILD_DIR:-}" ]; then
    CACHE_DIR="$OPENMARQUEE_BUILD_DIR/cache"
else
    CACHE_DIR="$HOME/Library/Caches/openmarquee"
fi
CACHED_IMAGE="$CACHE_DIR/pi-os-lite-arm64.img.xz"
CACHED_SHA256="$CACHE_DIR/pi-os-lite-arm64.img.xz.sha256"

DRY_RUN=0

# ============================================================
# Logging helpers.
# ============================================================

log()  { printf '%s\n' "$*"; }
info() { printf '==> %s\n' "$*"; }
warn() { printf 'warn: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

# ============================================================
# Arg parsing.
# ============================================================

TARGET=""
WIFI_SSID=""
WIFI_PASSWORD=""
WIFI_PASSWORD_FILE=""
WIFI_PASSWORD_INLINE=0
# r34 (2026-05-31): mgmt-WiFi (the USB-WiFi-dongle path). Independent
# of the sign-WiFi pre-config above. See qa/r31-dongle-topology-
# recommendation-2026-05-31.md §B.2 for the role split.
MGMT_WIFI_SSID=""
MGMT_WIFI_PASSWORD=""
MGMT_WIFI_PASSWORD_FILE=""
MGMT_WIFI_PASSWORD_INLINE=0
# 2026-07-13: optional --ssh-key forwarded to stage_sd_card.sh to seed
# openmarquee's SSH authorized key. If omitted, stage_sd_card auto-detects
# ~/.ssh/id_ed25519.pub (then id_rsa.pub), so the common case needs nothing.
SSH_KEY_PATH=""

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --ssh-key)
            SSH_KEY_PATH="${2:-}"
            [ -z "$SSH_KEY_PATH" ] && die "--ssh-key requires a value"
            shift 2
            ;;
        --help|-h)
            # r34: range bumped from 2,55 to 2,66 to keep the new
            # mgmt-WiFi block + the Safety / macOS / Cache sections
            # in --help output. Re-bump if the header grows again;
            # the boundary is one line above `set -euo pipefail`.
            sed -n '2,66p' "$0"
            exit 0
            ;;
        --wifi-ssid)
            WIFI_SSID="${2:-}"
            [ -z "$WIFI_SSID" ] && die "--wifi-ssid requires a value"
            shift 2
            ;;
        --wifi-ssid=*)
            WIFI_SSID="${1#*=}"
            shift
            ;;
        --wifi-password)
            WIFI_PASSWORD="${2:-}"
            WIFI_PASSWORD_INLINE=1
            [ -z "$WIFI_PASSWORD" ] && die "--wifi-password requires a value"
            shift 2
            ;;
        --wifi-password=*)
            WIFI_PASSWORD="${1#*=}"
            WIFI_PASSWORD_INLINE=1
            shift
            ;;
        --wifi-password-file)
            WIFI_PASSWORD_FILE="${2:-}"
            [ -z "$WIFI_PASSWORD_FILE" ] && die "--wifi-password-file requires a path"
            shift 2
            ;;
        --wifi-password-file=*)
            WIFI_PASSWORD_FILE="${1#*=}"
            shift
            ;;
        --mgmt-wifi-ssid)
            MGMT_WIFI_SSID="${2:-}"
            [ -z "$MGMT_WIFI_SSID" ] && die "--mgmt-wifi-ssid requires a value"
            shift 2
            ;;
        --mgmt-wifi-ssid=*)
            MGMT_WIFI_SSID="${1#*=}"
            shift
            ;;
        --mgmt-wifi-password)
            MGMT_WIFI_PASSWORD="${2:-}"
            MGMT_WIFI_PASSWORD_INLINE=1
            [ -z "$MGMT_WIFI_PASSWORD" ] && die "--mgmt-wifi-password requires a value"
            shift 2
            ;;
        --mgmt-wifi-password=*)
            MGMT_WIFI_PASSWORD="${1#*=}"
            MGMT_WIFI_PASSWORD_INLINE=1
            shift
            ;;
        --mgmt-wifi-password-file)
            MGMT_WIFI_PASSWORD_FILE="${2:-}"
            [ -z "$MGMT_WIFI_PASSWORD_FILE" ] && die "--mgmt-wifi-password-file requires a path"
            shift 2
            ;;
        --mgmt-wifi-password-file=*)
            MGMT_WIFI_PASSWORD_FILE="${1#*=}"
            shift
            ;;
        --)
            shift
            TARGET="${1:-}"
            shift || true
            ;;
        -*)
            die "unknown flag: $1 (try --help)"
            ;;
        *)
            if [ -z "$TARGET" ]; then
                TARGET="$1"
            else
                die "unexpected positional arg: $1 (target already set to $TARGET)"
            fi
            shift
            ;;
    esac
done

# Resolve WiFi password from one of three sources, in priority order:
#   1. --wifi-password-file PATH (preferred; trimmed of trailing newline)
#   2. OPENMARQUEE_WIFI_PASSWORD env (preferred; ps-safe)
#   3. --wifi-password PASS (warn: visible in ps)
#   4. Interactive prompt via read -s (when --wifi-ssid given but no source)
# Refuse partial specs (e.g., SSID without password) loudly.
if [ -n "$WIFI_SSID" ]; then
    if [ -n "$WIFI_PASSWORD_FILE" ]; then
        [ -r "$WIFI_PASSWORD_FILE" ] || die "--wifi-password-file: cannot read $WIFI_PASSWORD_FILE"
        # Read first line, trim trailing newline. Don't expose path or
        # contents in error messages even if read fails.
        WIFI_PASSWORD="$(head -n1 "$WIFI_PASSWORD_FILE" | tr -d '\r\n')"
        [ -z "$WIFI_PASSWORD" ] && die "--wifi-password-file: file is empty"
    elif [ -n "${OPENMARQUEE_WIFI_PASSWORD:-}" ]; then
        WIFI_PASSWORD="$OPENMARQUEE_WIFI_PASSWORD"
    elif [ "$WIFI_PASSWORD_INLINE" -eq 1 ]; then
        warn "--wifi-password as inline arg is visible in 'ps auxww'."
        warn "    For production / CI use \$OPENMARQUEE_WIFI_PASSWORD or"
        warn "    --wifi-password-file instead."
    else
        # Interactive prompt -- only viable for hands-on operator burns.
        if [ -t 0 ]; then
            printf 'WiFi password for SSID %s (input hidden): ' "$WIFI_SSID" >&2
            IFS= read -rs WIFI_PASSWORD || die "read failed"
            printf '\n' >&2
            [ -z "$WIFI_PASSWORD" ] && die "empty password"
        else
            die "--wifi-ssid requires --wifi-password, --wifi-password-file, or \$OPENMARQUEE_WIFI_PASSWORD (no tty for interactive prompt)"
        fi
    fi
fi

# r34 (2026-05-31): mirror the resolution chain above for the
# mgmt-WiFi credentials. Same priority order; same partial-spec
# refusal. The two paths are independent: an operator can pre-burn
# both, neither, or just one.
if [ -n "$MGMT_WIFI_SSID" ]; then
    if [ -n "$MGMT_WIFI_PASSWORD_FILE" ]; then
        [ -r "$MGMT_WIFI_PASSWORD_FILE" ] || die "--mgmt-wifi-password-file: cannot read $MGMT_WIFI_PASSWORD_FILE"
        MGMT_WIFI_PASSWORD="$(head -n1 "$MGMT_WIFI_PASSWORD_FILE" | tr -d '\r\n')"
        [ -z "$MGMT_WIFI_PASSWORD" ] && die "--mgmt-wifi-password-file: file is empty"
    elif [ -n "${OPENMARQUEE_MGMT_WIFI_PASSWORD:-}" ]; then
        MGMT_WIFI_PASSWORD="$OPENMARQUEE_MGMT_WIFI_PASSWORD"
    elif [ "$MGMT_WIFI_PASSWORD_INLINE" -eq 1 ]; then
        warn "--mgmt-wifi-password as inline arg is visible in 'ps auxww'."
        warn "    For production / CI use \$OPENMARQUEE_MGMT_WIFI_PASSWORD or"
        warn "    --mgmt-wifi-password-file instead."
    else
        if [ -t 0 ]; then
            printf 'mgmt-WiFi password for SSID %s (input hidden): ' "$MGMT_WIFI_SSID" >&2
            IFS= read -rs MGMT_WIFI_PASSWORD || die "read failed"
            printf '\n' >&2
            [ -z "$MGMT_WIFI_PASSWORD" ] && die "empty password"
        else
            die "--mgmt-wifi-ssid requires --mgmt-wifi-password, --mgmt-wifi-password-file, or \$OPENMARQUEE_MGMT_WIFI_PASSWORD (no tty for interactive prompt)"
        fi
    fi
fi

[ -z "$TARGET" ] && die "missing target disk path. usage: $0 [--dry-run] /dev/diskN"

# ============================================================
# Required tool presence (`xz`, `curl`, `shasum`, `diskutil`,
# `dd`, `plutil`). Bail with brew-install hints on macOS.
# ============================================================

require_tool() {
    local cmd="$1" hint="$2"
    command -v "$cmd" >/dev/null 2>&1 || die "$cmd not found. $hint"
}
require_tool xz      "brew install xz"
require_tool curl    "should be preinstalled on macOS"
require_tool shasum  "should be preinstalled on macOS"
require_tool diskutil "macOS-only; this script is Mac-side"
require_tool dd      "should be preinstalled on macOS"
require_tool plutil  "macOS-only; should be preinstalled"

# ============================================================
# Validate target disk: must exist, must be /dev/disk*, must
# be removable/external per diskutil info. Internal storage
# (typically /dev/disk0 + /dev/disk1) is refused outright.
# ============================================================

validate_target_disk() {
    local target="$1"
    case "$target" in
        /dev/disk[0-9]|/dev/disk[0-9][0-9])
            ;;
        *)
            die "target must be /dev/diskN (a whole disk, not a partition like /dev/disk4s1). got: $target"
            ;;
    esac

    if ! diskutil info -plist "$target" >/dev/null 2>&1; then
        warn "diskutil doesn't recognize $target. Available external/removable disks:"
        diskutil list external removable >&2 || true
        die "no such disk: $target"
    fi

    local plist
    plist="$(diskutil info -plist "$target")"

    local is_removable
    is_removable="$(plutil -extract RemovableMediaOrExternalDevice raw -o - - <<<"$plist" 2>/dev/null || echo "false")"
    local is_ejectable
    is_ejectable="$(plutil -extract Ejectable raw -o - - <<<"$plist" 2>/dev/null || echo "false")"

    # 2026-05-15 fix: built-in Mac SD slot reports Internal=true even
    # though the medium IS Removable+Ejectable. Internal SSD reports
    # Internal=true + Removable=false + Ejectable=false. Discriminate
    # by medium-removability, not slot-location -- SD-slot cards must
    # not be falsely refused as "internal storage", while truly-
    # internal storage (the Mac SSD) still gets rejected here since
    # both Removable and Ejectable are false for it.
    if [ "$is_removable" != "true" ] && [ "$is_ejectable" != "true" ]; then
        die "$target is not flagged removable/ejectable per diskutil. Refusing for safety."
    fi

    # Size sanity: SD cards for Pi are typically 16-128 GB, max 512.
    # Anything > 512 GB is almost certainly an external SSD/HDD the
    # operator does NOT want to flash. 2026-05-15: added because the
    # Internal-flag removal widened the set of accepted disks; this
    # protects against accidentally targeting a Time Machine / backup
    # external SSD.
    local total_size_bytes
    total_size_bytes="$(plutil -extract TotalSize raw -o - - <<<"$plist" 2>/dev/null || echo 0)"
    local size_cap_bytes=$((512 * 1024 * 1024 * 1024))
    if [ "$total_size_bytes" -gt "$size_cap_bytes" ]; then
        die "$target is $((total_size_bytes / 1024 / 1024 / 1024)) GB which exceeds the 512 GB SD-card sanity cap. Refusing -- this is likely an external SSD/HDD, not an SD card."
    fi
}

human_readable_target() {
    local target="$1"
    local plist size_bytes size_gb media_name
    plist="$(diskutil info -plist "$target" 2>/dev/null || echo "")"
    size_bytes="$(plutil -extract TotalSize raw -o - - <<<"$plist" 2>/dev/null || echo 0)"
    media_name="$(plutil -extract MediaName raw -o - - <<<"$plist" 2>/dev/null || echo "unknown")"
    # bash arithmetic on integers; truncate to int GB.
    size_gb=$(( size_bytes / 1000000000 ))
    printf '%s (%s, %s GB)' "$target" "$media_name" "$size_gb"
}

info "validating target disk..."
validate_target_disk "$TARGET"
TARGET_BASENAME="${TARGET##*/}"          # diskN
TARGET_RDISK="/dev/r${TARGET_BASENAME}"  # /dev/rdiskN -- raw, ~5x faster for dd
log "    target: $(human_readable_target "$TARGET")"
log "    raw device: $TARGET_RDISK"

# ============================================================
# Confirmation prompt: operator must type the exact diskN
# identifier. No --force / --yes bypass.
# ============================================================

if [ "$DRY_RUN" -eq 0 ]; then
    log ""
    log "About to ERASE $TARGET. This is destructive + irreversible."
    log "Type the EXACT identifier ($TARGET_BASENAME) to confirm, or anything else to abort:"
    read -r CONFIRM
    if [ "$CONFIRM" != "$TARGET_BASENAME" ]; then
        die "confirmation did not match '$TARGET_BASENAME' (got '$CONFIRM'). Aborting."
    fi
    log "    confirmed: $TARGET_BASENAME"
fi

# ============================================================
# Cache Pi OS Lite arm64 image.
# ============================================================

cache_is_fresh() {
    [ -f "$CACHED_IMAGE" ] || return 1
    # mtime in epoch seconds vs (now - 30 days)
    local mtime now max_age_secs
    mtime="$(stat -f %m "$CACHED_IMAGE" 2>/dev/null || echo 0)"
    now="$(date +%s)"
    max_age_secs=$(( CACHE_MAX_AGE_DAYS * 86400 ))
    [ $(( now - mtime )) -lt $max_age_secs ]
}

download_image() {
    info "downloading Pi OS Lite arm64 image..."
    mkdir -p "$CACHE_DIR"
    local tmp_img="$CACHED_IMAGE.partial"
    rm -f "$tmp_img"

    # First, resolve the _latest redirect to capture the versioned URL
    # so we can derive the .sha256 sibling URL.
    local resolved_url
    resolved_url="$(curl -sIL -o /dev/null -w '%{url_effective}' "$IMAGE_URL")"
    if [ -z "$resolved_url" ] || [ "$resolved_url" = "$IMAGE_URL" ]; then
        # Some curl versions don't follow on HEAD; fall back to a GET
        # with -L and abort the body.
        resolved_url="$(curl -sL -o /dev/null -w '%{url_effective}' --max-time 30 -r 0-0 "$IMAGE_URL" || true)"
    fi
    [ -z "$resolved_url" ] && die "could not resolve $IMAGE_URL (curl returned no effective URL)"
    log "    resolved: $resolved_url"

    info "fetching image (this takes a few minutes; ~500 MiB)..."
    curl -fL --retry 3 --retry-delay 5 --progress-bar -o "$tmp_img" "$resolved_url" \
        || die "image download failed"
    mv "$tmp_img" "$CACHED_IMAGE"

    info "fetching SHA256 manifest..."
    local sha_url="${resolved_url}.sha256"
    local tmp_sha="$CACHED_SHA256.partial"
    rm -f "$tmp_sha"
    if curl -fsL --retry 3 -o "$tmp_sha" "$sha_url"; then
        mv "$tmp_sha" "$CACHED_SHA256"
    else
        rm -f "$tmp_sha"
        warn "SHA256 manifest not available at $sha_url (proceeding without verify)"
        return 0
    fi
}

verify_image() {
    [ -f "$CACHED_SHA256" ] || { warn "no SHA256 manifest cached; skipping verify"; return 0; }
    info "verifying SHA256..."
    # The .sha256 file format is "<sha256>  <filename>". We only need
    # the hash; ignore the filename column (it may differ from our
    # cached path).
    local expected actual
    expected="$(awk '{print $1; exit}' "$CACHED_SHA256")"
    actual="$(shasum -a 256 "$CACHED_IMAGE" | awk '{print $1}')"
    if [ "$expected" != "$actual" ]; then
        rm -f "$CACHED_IMAGE" "$CACHED_SHA256"
        die "SHA256 mismatch (expected $expected, got $actual). Cache purged; re-run."
    fi
    log "    sha256 ok"
}

if cache_is_fresh; then
    log "    cached image is fresh: $CACHED_IMAGE"
    verify_image
else
    if [ "$DRY_RUN" -eq 1 ]; then
        info "[DRY-RUN] would download $IMAGE_URL to $CACHED_IMAGE + verify SHA256"
    else
        download_image
        verify_image
    fi
fi

# ============================================================
# Bundle presence (before destructive ops). The bundle is what
# stage_sd_card.sh copies onto bootfs after dd.
# ============================================================

BUNDLE="${OPENMARQUEE_BUNDLE:-$REPO_ROOT/dist/openmarquee-sd-bundle.tar.zst}"
if [ ! -f "$BUNDLE" ]; then
    die "missing bundle: $BUNDLE. Run \`bash scripts/build_sd_bundle.sh\` first."
fi
info "bundle found: $BUNDLE"

# ============================================================
# Pre-dd: prime sudo + register cleanup trap.
# ============================================================

cleanup_on_interrupt() {
    log ""
    warn "interrupted! The SD card at $TARGET is in an UNDEFINED state."
    warn "Re-run burn_sd_card.sh from scratch; do not boot the Pi with this card as-is."
    if [ "$DRY_RUN" -eq 0 ]; then
        sudo diskutil eject "$TARGET" 2>/dev/null || true
    fi
    exit 130
}

if [ "$DRY_RUN" -eq 0 ]; then
    info "priming sudo (you may be prompted once for password)..."
    sudo -n true 2>/dev/null || sudo -v || die "sudo authentication failed"
    trap cleanup_on_interrupt INT TERM
fi

# ============================================================
# Unmount the WHOLE disk (not just one volume), then flash.
# ============================================================

info "unmounting $TARGET (full disk)..."
if [ "$DRY_RUN" -eq 1 ]; then
    log "    [DRY-RUN] would run: sudo diskutil unmountDisk $TARGET"
else
    sudo diskutil unmountDisk "$TARGET" || die "unmountDisk failed"
fi

info "flashing image to $TARGET_RDISK (raw device; ~5x faster than $TARGET)..."
log "    a Finder 'disk not recognized' dialog may pop up during dd -- ignore it."
log "    this takes 3-5 min on USB 2 / 1-2 min on USB 3."
if [ "$DRY_RUN" -eq 1 ]; then
    log "    [DRY-RUN] would run: xz -dc $CACHED_IMAGE | sudo dd of=$TARGET_RDISK bs=4M status=progress oflag=sync"
else
    xz -dc "$CACHED_IMAGE" | sudo dd of="$TARGET_RDISK" bs=4m \
        || die "flash failed (xz or dd; target may be partially written; re-flash)"
    sync
fi

# ============================================================
# Wait for bootfs auto-mount. macOS detects the FAT partition
# automatically; 5-20 seconds is typical. Force a mountDisk if
# it doesn't appear within 60s.
# ============================================================

BOOTFS=""

wait_for_bootfs() {
    local deadline=$(( $(date +%s) + 60 ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if [ -d /Volumes/bootfs ]; then
            BOOTFS="/Volumes/bootfs"
            return 0
        fi
        sleep 2
    done
    return 1
}

info "waiting for bootfs partition auto-mount (up to 60s)..."
if [ "$DRY_RUN" -eq 1 ]; then
    log "    [DRY-RUN] would wait for /Volumes/bootfs, falling back to diskutil mountDisk"
    BOOTFS="/Volumes/bootfs"
else
    if ! wait_for_bootfs; then
        warn "bootfs didn't auto-mount; trying explicit mountDisk..."
        sudo diskutil mountDisk "$TARGET" || die "mountDisk failed; aborting (card is flashed but unstaged)"
        wait_for_bootfs || die "bootfs still not at /Volumes/bootfs after mountDisk. Stage manually."
    fi
    log "    bootfs mounted: $BOOTFS"
fi

# ============================================================
# Stage the openMarquee bundle. If this fails, do NOT eject --
# leave the card mounted so the operator can re-run staging
# manually and inspect.
# ============================================================

info "staging openMarquee bundle to $BOOTFS..."
if [ "$DRY_RUN" -eq 1 ]; then
    log "    [DRY-RUN] would run: bash $SCRIPT_DIR/stage_sd_card.sh ${SSH_KEY_PATH:+--ssh-key $SSH_KEY_PATH }$BOOTFS"
else
    if ! bash "$SCRIPT_DIR/stage_sd_card.sh" ${SSH_KEY_PATH:+--ssh-key "$SSH_KEY_PATH"} "$BOOTFS"; then
        warn "staging failed. SD card is left mounted at $BOOTFS for inspection."
        warn "Re-run: bash $SCRIPT_DIR/stage_sd_card.sh $BOOTFS"
        warn "After fix, manually: sudo diskutil eject $TARGET"
        exit 1
    fi
fi

# Phase 4e-b 2026-05-15: optional WiFi pre-config via NM keyfile drop.
# When --wifi-ssid is given, write an NM keyfile to bootfs alongside
# the bundle. openmarquee-firstboot.sh detects this on first boot and
# moves it into /etc/NetworkManager/system-connections/ with the
# right perms (chmod 600, root:root). This BYPASSES cloud-init's
# network-config (which empirically doesn't translate `wifis:` blocks
# into NM keyfiles on this image — Phase 4e investigation, e092005).
if [ -n "$WIFI_SSID" ]; then
    KEYFILE="$BOOTFS/openmarquee-wifi.nmconnection"
    info "writing WiFi pre-config keyfile to $KEYFILE (SSID=$WIFI_SSID)..."
    if [ "$DRY_RUN" -eq 1 ]; then
        log "    [DRY-RUN] would write keyfile (password redacted)"
    else
        # Heredoc EOF is UNQUOTED so $WIFI_SSID + $WIFI_PASSWORD expand
        # into the file body. Bash does NOT re-interpret the resulting
        # text as shell -- $(cmd) / backticks / etc. inside the password
        # VALUE land as literal characters, not command substitution.
        # NM keyfile is INI-format; a password containing a literal
        # newline (rare) could inject extra INI sections. The
        # --wifi-password-file path strips with `head -n1 | tr -d '\r\n'`
        # upstream; --wifi-password / $OPENMARQUEE_WIFI_PASSWORD trust
        # the operator's input. NB: bootfs is FAT32 (no perms);
        # firstboot.sh chmod's the destination after copy to rootfs.
        umask 077  # FAT32 ignores; the rootfs-side chmod 600 is what matters
        cat > "$KEYFILE" <<KEYFILE_EOF
[connection]
id=openmarquee-wifi
type=wifi
interface-name=wlan0
autoconnect=true
autoconnect-priority=100

[wifi]
mode=infrastructure
ssid=$WIFI_SSID

[wifi-security]
key-mgmt=wpa-psk
psk=$WIFI_PASSWORD

[ipv4]
method=auto

[ipv6]
method=auto
addr-gen-mode=default
KEYFILE_EOF
        umask 022
        log "    wrote keyfile ($(wc -c < "$KEYFILE") bytes); password not logged"
    fi
fi

# r34 (2026-05-31): optional mgmt-WiFi pre-config via NM keyfile drop.
# When --mgmt-wifi-ssid is given, write a second NM keyfile to bootfs.
# openmarquee-firstboot.sh §5d detects this on first boot and moves
# it into /etc/NetworkManager/system-connections/ with mode 0600
# root:root, mirroring §5c for the sign-WiFi keyfile.
#
# Differences vs sign-WiFi: interface-name=wlan-dongle (the udev-
# renamed USB dongle), route-metric=50 (lower = preferred for
# default route -- mgmt path wins over sign-WiFi's 600), and the
# id= field is openmarquee-mgmt-wifi for unambiguous nmcli listing.
#
# Independent of the sign-WiFi block above: an operator can pre-burn
# both, just one, or neither. The runtime cost of writing the
# keyfile is trivial; the firstboot drop is a no-op if the keyfile
# is absent.
if [ -n "$MGMT_WIFI_SSID" ]; then
    MGMT_KEYFILE="$BOOTFS/openmarquee-mgmt-wifi.nmconnection"
    info "writing mgmt-WiFi pre-config keyfile to $MGMT_KEYFILE (SSID=$MGMT_WIFI_SSID)..."
    if [ "$DRY_RUN" -eq 1 ]; then
        log "    [DRY-RUN] would write mgmt keyfile (password redacted)"
    else
        umask 077
        cat > "$MGMT_KEYFILE" <<MGMT_KEYFILE_EOF
[connection]
id=openmarquee-mgmt-wifi
type=wifi
interface-name=wlan-dongle
autoconnect=true
autoconnect-priority=10

[wifi]
mode=infrastructure
ssid=$MGMT_WIFI_SSID

[wifi-security]
key-mgmt=wpa-psk
psk=$MGMT_WIFI_PASSWORD

[ipv4]
method=auto
route-metric=50

[ipv6]
method=auto
addr-gen-mode=default
MGMT_KEYFILE_EOF
        umask 022
        log "    wrote mgmt keyfile ($(wc -c < "$MGMT_KEYFILE") bytes); password not logged"
    fi
fi

# ============================================================
# Eject cleanly.
# ============================================================

info "ejecting $TARGET..."
if [ "$DRY_RUN" -eq 1 ]; then
    log "    [DRY-RUN] would run: sudo diskutil eject $TARGET"
else
    sudo diskutil eject "$TARGET" || warn "eject reported non-zero (card may already be ejected)"
fi

log ""
log "SD card ready. Insert into Pi + power on."
log "First boot creates AP 'MySign-XXXX' in ~1-3 min (or up to ~5 min with apt installs)."
log "Default Pi hostname during install: mysign-init.local (ssh available over ethernet)."
log ""
[ "$DRY_RUN" -eq 1 ] && log "(dry-run: nothing was actually written or ejected.)"
