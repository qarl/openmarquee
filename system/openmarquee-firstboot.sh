#!/usr/bin/env bash
# openmarquee-firstboot.sh — runs once on the very first boot of a
# freshly-flashed openMarquee SD card (Phase B.4, closes sweep #5 #2).
#
# Generates a per-device MySignXXX identifier (qarl 2026-05-12) + a
# random AP passphrase, writes /var/openmarquee/identity.json (0644)
# + wifi.json (0600), sets /etc/hostname to MySignXXX (replacing the
# cloud-init random hostname), templates /etc/hostapd/hostapd.conf,
# templates the SSID + password + QR-code SVG + device_id into
# /opt/openmarquee/ui/welcome.html, then touches
# /var/openmarquee/.bootstrapped and disables itself (the service
# unit's ExecStartPost also runs systemctl disable as belt-and-braces).
#
# MySignXXX format: literal "MySign" + 3 alphanumeric chars from
# [A-Z0-9] (36 chars / position; 36^3 = 46,656 IDs). qarl-chosen
# format -- mnemonic for "this is MY sign," short enough to memorize,
# big enough namespace for any plausible fleet. Lives in identity.json
# as the SINGLE SOURCE OF TRUTH for SSID + hostname + welcome.html.
#
# Idempotency:
#   - If /var/openmarquee/wifi.json already exists with both fields
#     populated, reuse it instead of regenerating. This protects
#     against an interrupted previous run (e.g. power-cycle mid-script
#     after wifi.json was written but before hostapd.conf templating).
#   - If the script gets re-run (e.g. via `--force`), the same wifi.json
#     produces the same templated outputs.
#
# Sweep #5 #2 closure: this is the per-device AP password rotation.
# Without it every flashed device ships with the same default passphrase
# from system/hostapd.conf (`change-me-at-first-boot`) and a parking-lot
# attacker who flashes one card to learn the default has WiFi access to
# every openMarquee sign on the planet. Generating a 16-char
# alphanumeric-with-symbol passphrase at first boot closes that.

# `x` (xtrace) prints every executed line — with $VAR expansions — to
# stderr → systemd journal → persistent under the journald drop-in
# in install.sh §7c. Combined with the forensic prelude below, the
# persistent journal carries an execution trace from start through
# failure for any future restart-loop diagnosis. Tradeoff: xtrace
# echoes ${PASSPHRASE} via assignment + sed templating + WIFI_URI
# into the journal. Mitigated by journal being root-readable only
# and the same passphrase landing at /var/openmarquee/wifi.json
# (mode 0600, root-only) — same sudo-required reach, theoretical
# leak only.
set -euxo pipefail

# Redirect xtrace output to a dedicated persistent file so the full
# execution trace survives even if systemd doesn't flush the journal
# in time on a Restart=on-failure cycle. Real-device only — guarded
# so test harness runs (ROOT_PREFIX set) don't try to open /var/log.
# Default-init lets the gate evaluate before the ROOT_PREFIX line
# below; that line then explicitly reassigns ROOT_PREFIX (idempotent
# with the same value, so no race).
: "${ROOT_PREFIX:=}"
if [ -z "$ROOT_PREFIX" ]; then
    exec {XTRACE_FD}>>/var/log/openmarquee-firstboot-xtrace.log 2>/dev/null || XTRACE_FD=
    if [ -n "${XTRACE_FD:-}" ]; then
        # 0600 for consistency with wifi.json — xtrace echoes PASSPHRASE
        # via assignments + sed templating + WIFI_URI.
        chmod 600 /var/log/openmarquee-firstboot-xtrace.log 2>/dev/null || true
        BASH_XTRACEFD=$XTRACE_FD
        export BASH_XTRACEFD
    fi
fi

# EXIT trap: dump failure context to stderr (→ journal + the firstboot
# drop-in log) if firstboot.sh exits non-zero. set -e unwinds the stack
# so BASH_LINENO[0] reports the line that triggered the failure;
# BASH_COMMAND holds the command that was about to run. The `caller`
# loop walks the function-call stack so failures inside helpers
# (sed_inplace_safe et al) report the caller chain too.
_firstboot_on_exit() {
    local rc=$?
    if [ "$rc" -ne 0 ]; then
        {
            printf '\n=== firstboot.sh EXIT trap fired: rc=%s line=%s ===\n' \
                "$rc" "${BASH_LINENO[0]:-unknown}"
            printf 'BASH_COMMAND was: %s\n' "${BASH_COMMAND:-unknown}"
            printf 'PWD=%s\n' "$PWD"
            printf 'caller chain:\n'
            local i=0
            while caller "$i" 2>/dev/null; do i=$((i+1)); done
        } >&2
    fi
    return $rc
}
trap _firstboot_on_exit EXIT

WIFI_JSON="${WIFI_JSON:-/var/openmarquee/wifi.json}"
IDENTITY_JSON="${IDENTITY_JSON:-/var/openmarquee/identity.json}"
HOSTAPD_CONF="${HOSTAPD_CONF:-/etc/hostapd/hostapd.conf}"
WELCOME_HTML="${WELCOME_HTML:-/opt/openmarquee/ui/welcome.html}"
BOOTSTRAP_MARKER="${BOOTSTRAP_MARKER:-/var/openmarquee/.bootstrapped}"
ETC_HOSTNAME="${ETC_HOSTNAME:-/etc/hostname}"
ETC_HOSTS="${ETC_HOSTS:-/etc/hosts}"
PHY_IFACE="${PHY_IFACE:-wlan0}"
# Phase 4e-b 2026-05-15: optional WiFi pre-config path. burn_sd_card.sh
# --wifi-ssid drops an NM keyfile to /boot/firmware/. On first boot we
# move it into NetworkManager's system-connections/ with the right perms
# (NM rejects keyfiles with looser perms than 600).
BOOTFS_NM_KEYFILE="${BOOTFS_NM_KEYFILE:-/boot/firmware/openmarquee-wifi.nmconnection}"
# r34 (2026-05-31): mgmt-WiFi keyfile drop, mirrors the sign-WiFi
# path above. burn_sd_card.sh --mgmt-wifi-ssid drops this onto
# bootfs alongside the sign keyfile; firstboot §5d moves it to
# system-connections/. Independent of the sign keyfile — either,
# both, or neither may be present.
BOOTFS_MGMT_NM_KEYFILE="${BOOTFS_MGMT_NM_KEYFILE:-/boot/firmware/openmarquee-mgmt-wifi.nmconnection}"
NM_SYSTEM_CONNECTIONS="${NM_SYSTEM_CONNECTIONS:-/etc/NetworkManager/system-connections}"

# Allow tests to redirect everything under a tmpdir.
ROOT_PREFIX="${ROOT_PREFIX:-}"
WIFI_JSON="${ROOT_PREFIX}${WIFI_JSON}"
IDENTITY_JSON="${ROOT_PREFIX}${IDENTITY_JSON}"
HOSTAPD_CONF="${ROOT_PREFIX}${HOSTAPD_CONF}"
WELCOME_HTML="${ROOT_PREFIX}${WELCOME_HTML}"
BOOTSTRAP_MARKER="${ROOT_PREFIX}${BOOTSTRAP_MARKER}"
ETC_HOSTNAME="${ROOT_PREFIX}${ETC_HOSTNAME}"
ETC_HOSTS="${ROOT_PREFIX}${ETC_HOSTS}"
BOOTFS_NM_KEYFILE="${ROOT_PREFIX}${BOOTFS_NM_KEYFILE}"
BOOTFS_MGMT_NM_KEYFILE="${ROOT_PREFIX}${BOOTFS_MGMT_NM_KEYFILE}"
NM_SYSTEM_CONNECTIONS="${ROOT_PREFIX}${NM_SYSTEM_CONNECTIONS}"

say() { printf '==> %s\n' "$*"; }
fatal() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# Forensic prelude — dump environment state to stderr so journalctl captures
# it. Boot 7 failed somewhere in this script but the journal was runtime-only
# (lost on reboot) and the SD-card forensics showed only the aftermath
# (orphan inodes from mid-write yank), not the trigger. This prelude is
# cheap (~few syscalls) and is the diagnostic anchor if a future boot stalls
# in a restart loop.
{
    printf 'forensic: sed version: '; sed --version 2>/dev/null | head -1 || echo "(busybox sed?)"
    if sed_path=$(command -v sed) && [ -n "$sed_path" ]; then
        printf 'forensic: sed binary: %s sha=%s\n' "$sed_path" \
            "$(sha256sum "$sed_path" 2>/dev/null | awk '{print $1}')"
    fi
    printf 'forensic: PWD=%s UID=%s EUID=%s\n' "$PWD" "${UID:-$(id -ru 2>/dev/null || echo unknown)}" "${EUID:-$(id -u 2>/dev/null || echo unknown)}"
    printf 'forensic: mounts relevant:\n'
    findmnt -no TARGET,SOURCE,FSTYPE,OPTIONS / /etc /opt/openmarquee/ui /var/openmarquee /boot/firmware 2>/dev/null \
        || mount 2>/dev/null \
        || echo "  (no findmnt/mount available)"
    printf 'forensic: disk free:\n'
    df -h / /boot/firmware 2>/dev/null || df -h 2>/dev/null || echo "  (no df available)"
    printf 'forensic: file sizes pre-templating:\n'
    for f in "$ETC_HOSTS" "$HOSTAPD_CONF" "$WELCOME_HTML" "$BOOTFS_NM_KEYFILE" "$BOOTFS_MGMT_NM_KEYFILE"; do
        if [ -e "$f" ]; then
            printf '  %s: %s bytes, perms %s\n' "$f" \
                "$(stat -c '%s' "$f" 2>/dev/null || wc -c < "$f" 2>/dev/null || echo unknown)" \
                "$(stat -c '%a' "$f" 2>/dev/null || echo unknown)"
        else
            printf '  %s: ABSENT\n' "$f"
        fi
    done
} >&2

# --- 1a. Generate or read MySignXXX device identifier -----------------------

# MySign + 3 alphanumeric [A-Z0-9]. 36^3 = 46,656 IDs -- plenty for any
# plausible openMarquee fleet. Used as: AP SSID, /etc/hostname,
# Tailscale magic-DNS name, the operator-facing "what's my sign called"
# string in welcome.html.
generate_device_id() {
    local suffix
    # tr -dc filters /dev/urandom to our alphabet; head -c 3 caps.
    # head -c works because the alphabet is single-byte ASCII.
    suffix=$(LC_ALL=C tr -dc 'A-Z0-9' < /dev/urandom | head -c 3)
    printf 'MySign%s' "$suffix"
}

# --- 1b. Generate or read AP credentials ------------------------------------

# Passphrase: 16-char alphanumeric + a few safe symbols (no quotes,
# no backslash, no ampersand -- those break shell + hostapd.conf
# parsing). Selected set: A-Z a-z 0-9 + - _ . @
#
# 16 chars * log2(64) = ~96 bits of entropy -- well above what an
# attacker can brute-force against a WPA2 4-way handshake.
generate_passphrase() {
    local pw=""
    while [ "${#pw}" -lt 16 ]; do
        # Read raw bytes, filter to our charset, append until length 16.
        pw+="$(LC_ALL=C tr -dc 'A-Za-z0-9+_.@-' < /dev/urandom | head -c 16)"
    done
    printf '%s' "${pw:0:16}"
}

# Read existing identity.json (idempotent re-run) or generate fresh.
# Order matters: device_id is established BEFORE wifi creds so the
# SSID can derive from it.
if [ -f "$IDENTITY_JSON" ]; then
    say "Reading existing $IDENTITY_JSON (idempotent re-run)"
    DEVICE_ID=$(sed -n 's/.*"device_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$IDENTITY_JSON")
    [ -n "$DEVICE_ID" ] || fatal "identity.json missing device_id"
else
    say "Generating per-device identifier"
    DEVICE_ID=$(generate_device_id)
fi

WIFI_JSON_REGENERATED=0
if [ -f "$WIFI_JSON" ]; then
    say "Reading existing $WIFI_JSON (idempotent re-run)"
    SSID=$(sed -n 's/.*"ssid"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$WIFI_JSON")
    PASSPHRASE=$(sed -n 's/.*"passphrase"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$WIFI_JSON")
    [ -n "$SSID" ] || fatal "wifi.json missing ssid"
    [ -n "$PASSPHRASE" ] || fatal "wifi.json missing passphrase"
else
    say "Generating per-device AP credentials"
    # SSID is now the device_id verbatim (qarl 2026-05-12). Replaces the
    # MAC-derived openMarquee-<suffix> form; single source of truth in
    # identity.json. WPA2 SSID limit is 32 chars; "MySign" + 3 = 9.
    SSID="${DEVICE_ID}"
    PASSPHRASE=$(generate_passphrase)
    WIFI_JSON_REGENERATED=1
fi

# --- 2a. Write identity.json (0644 -- public ID, not a secret) --------------

if [ ! -f "$IDENTITY_JSON" ]; then
    say "Writing $IDENTITY_JSON (0644)"
    mkdir -p "$(dirname "$IDENTITY_JSON")"
    TMP_ID="${IDENTITY_JSON}.tmp"
    cat > "$TMP_ID" <<EOF
{
  "device_id": "${DEVICE_ID}"
}
EOF
    chmod 0644 "$TMP_ID"
    mv "$TMP_ID" "$IDENTITY_JSON"
fi

# --- 2b. Write wifi.json (0600) only when we generated new creds ------------

if [ "$WIFI_JSON_REGENERATED" -eq 1 ]; then
    say "Writing $WIFI_JSON (0600)"
    mkdir -p "$(dirname "$WIFI_JSON")"
    # Atomic-ish: write to .tmp then mv. JSON-encode the values manually --
    # they're constrained to our charset so no escaping needed.
    TMP_JSON="${WIFI_JSON}.tmp"
    cat > "$TMP_JSON" <<EOF
{
  "ssid": "${SSID}",
  "passphrase": "${PASSPHRASE}"
}
EOF
    chmod 0600 "$TMP_JSON"
    mv "$TMP_JSON" "$WIFI_JSON"
fi

# --- 2c. Set /etc/hostname (overrides cloud-init's random openmarquee-<hex>)

say "Setting hostname to ${DEVICE_ID}"
# Write /etc/hostname directly + update /etc/hosts 127.0.1.1 line so
# `sudo` doesn't warn about an unresolvable hostname. hostnamectl
# requires systemd which may not be running yet in test environments;
# call it best-effort. Real devices have it.
echo "${DEVICE_ID}" > "$ETC_HOSTNAME"
sync
if [ -f "$ETC_HOSTS" ]; then
    if grep -q "^127\.0\.1\.1" "$ETC_HOSTS"; then
        sed -i.bak "s/^127\.0\.1\.1.*/127.0.1.1\t${DEVICE_ID}/" "$ETC_HOSTS"
        rm -f "${ETC_HOSTS}.bak"
    else
        printf '127.0.1.1\t%s\n' "${DEVICE_ID}" >> "$ETC_HOSTS"
    fi
    sync
fi
if command -v hostnamectl >/dev/null 2>&1 && [ -z "$ROOT_PREFIX" ]; then
    hostnamectl set-hostname "${DEVICE_ID}" 2>/dev/null || true
fi

# --- 3. Template hostapd.conf -----------------------------------------------

say "Templating $HOSTAPD_CONF"
# Replace the ssid= and wpa_passphrase= lines. The source file
# (system/hostapd.conf) ships with `ssid=openMarquee-SETUP` and
# `wpa_passphrase=change-me-at-first-boot`. Use sed in-place; the
# substitution patterns match the line BEGINNING so we don't touch
# comments mentioning those strings elsewhere.
if [ ! -f "$HOSTAPD_CONF" ]; then
    fatal "hostapd.conf not found at $HOSTAPD_CONF"
fi
sed -i.bak \
    -e "s/^ssid=.*/ssid=${SSID}/" \
    -e "s|^wpa_passphrase=.*|wpa_passphrase=${PASSPHRASE}|" \
    "$HOSTAPD_CONF"
rm -f "${HOSTAPD_CONF}.bak"
sync

# --- 4. Template welcome.html -----------------------------------------------

say "Templating $WELCOME_HTML"
if [ ! -f "$WELCOME_HTML" ]; then
    fatal "welcome.html not found at $WELCOME_HTML"
fi

# Substitute {{AP_SSID}}, {{AP_PASSWORD}}, {{DEVICE_ID}} placeholders.
# Use a delimiter (|) that won't appear in our charset (no pipe).
sed -i.bak \
    -e "s|{{AP_SSID}}|${SSID}|g" \
    -e "s|{{AP_PASSWORD}}|${PASSPHRASE}|g" \
    -e "s|{{DEVICE_ID}}|${DEVICE_ID}|g" \
    "$WELCOME_HTML"
# sync happens after the QR-injection python step below — that's the
# actual final state of welcome.html; syncing here would just flush an
# intermediate state that's about to be rewritten.

# --- 5. Generate QR-code SVG and substitute the fallback ---------------------

# WIFI: URI format -- iOS/Android cameras recognize it and offer to join.
# Format: WIFI:T:WPA;S:<ssid>;P:<password>;;
#
# Note: the spec requires escaping ';' ',' '"' '\' ':' in field values.
# Our passphrase alphabet ('A-Za-z0-9+_.@-') contains none. SSID format
# is openMarquee-<hex> -- also safe. If SSID ever gains operator-supplied
# customization, this line will need an escape pass.
WIFI_URI="WIFI:T:WPA;S:${SSID};P:${PASSPHRASE};;"

if command -v qrencode >/dev/null 2>&1; then
    say "Generating QR-code SVG fallback"
    # qrencode -t SVG outputs a stand-alone SVG. Strip the <?xml?>
    # declaration so it can inline cleanly into HTML.
    QR_SVG=$(qrencode -t SVG -o - "$WIFI_URI" | sed '/<?xml/d')
    # Write the SVG to a temp file and use sed's r command to inject
    # at the {{AP_PASSWORD_QR}} marker. sed in-place + multi-line
    # replacement is awkward; use python instead for safety.
    python3 - "$WELCOME_HTML" "$QR_SVG" <<'PY'
import sys
path = sys.argv[1]
qr_svg = sys.argv[2]
with open(path) as f:
    html = f.read()
html = html.replace("{{AP_PASSWORD_QR}}", qr_svg)
# Also strip the qr-placeholder class so the "PLACEHOLDER" watermark
# stops rendering once we've baked a real QR.
html = html.replace("qr qr-placeholder", "qr")
with open(path, "w") as f:
    f.write(html)
PY
else
    say "qrencode not available; leaving QR placeholder (welcome.js generates QR dynamically)"
fi
rm -f "${WELCOME_HTML}.bak"
sync

# --- 5b. Widen wpa_supplicant.conf read access for wifi-prefill --------------

# Phase C closure: backend/openmarquee/wifi_prefill.py reads
# /etc/wpa_supplicant/wpa_supplicant.conf to fold pre-flash SSID/PSK
# into settings.json on first GET /api/settings. pi-gen lays the file
# down 600 root:root; the openmarquee service user can't read it. Widen
# to 644 here so the prefill works without operator intervention.
#
# Safe: file contents are pi-gen-baked at build time (operator chose
# WPA_ESSID + WPA_PASSWORD when generating the image). The passphrase
# is already in /etc/hostapd/hostapd.conf (also 644-readable by design)
# and in /var/openmarquee/wifi.json (0600). 644 on wpa_supplicant.conf
# matches the symmetry of the AP creds path.
WPA_CONF="${ROOT_PREFIX}/etc/wpa_supplicant/wpa_supplicant.conf"
if [ -f "$WPA_CONF" ]; then
    say "Widening $WPA_CONF to 644 for wifi-prefill read access"
    chmod 644 "$WPA_CONF" || true
fi

# --- 5c. Phase 4e-b: operator WiFi pre-config via NM keyfile ----------------

# burn_sd_card.sh --wifi-ssid drops /boot/firmware/openmarquee-wifi.
# nmconnection onto bootfs (FAT32, no perms). Move it into
# /etc/NetworkManager/system-connections/ with the right perms; NM
# silently rejects keyfiles that aren't chmod 600 + chown root:root.
# Once moved, delete the bootfs copy so the plaintext psk doesn't
# linger on a partition any other host can mount.
if [ -f "$BOOTFS_NM_KEYFILE" ]; then
    DST="${NM_SYSTEM_CONNECTIONS}/openmarquee-wifi.nmconnection"
    say "Operator pre-configured WiFi keyfile found; moving to $DST"
    # Extract SSID for logging (do NOT log the psk; that line lives in
    # the keyfile and ends up readable to root via the destination
    # path, but we never echo it to stdout / journalctl).
    KEY_SSID="$(grep '^ssid=' "$BOOTFS_NM_KEYFILE" | head -1 | cut -d= -f2- || true)"
    say "  SSID: ${KEY_SSID:-<unparseable>}"
    mkdir -p "$NM_SYSTEM_CONNECTIONS"
    cp "$BOOTFS_NM_KEYFILE" "$DST"
    chmod 600 "$DST" || say "  WARN: chmod 600 failed (test mode?)"
    # chown only when running as root on a real device; test mode
    # (ROOT_PREFIX set) skips because the tmpdir is operator-owned.
    if [ -z "$ROOT_PREFIX" ] && [ "$(id -u)" -eq 0 ]; then
        chown root:root "$DST"
        # Tell NM to re-read system-connections/ so the new keyfile
        # gets picked up without waiting for the next boot. Best-effort:
        # NM may not be active yet on first boot (we run before it per
        # the Before= ordering in the service unit); failure is fine.
        if command -v nmcli >/dev/null 2>&1; then
            nmcli connection reload 2>/dev/null || true
        fi
    fi
    # Pi OS Lite mounts /boot/firmware (FAT32 bootfs) rw by default, but
    # some configurations or systemd auto-mount-units leave it ro. If the
    # bootfs is ro, leave the keyfile in place — the wifi.json idempotency
    # check protects subsequent runs from re-templating, and a leftover
    # keyfile on bootfs is not a security concern (the same keyfile already
    # exists at /etc/NetworkManager/system-connections/ with mode 0600).
    # DO NOT fail firstboot for this cleanup step.
    if rm -f "$BOOTFS_NM_KEYFILE" 2>/dev/null; then
        say "  applied; bootfs copy removed"
    else
        say "  applied; could not remove bootfs copy (likely ro bootfs); leaving in place"
    fi
fi

# --- 5d. r34: operator mgmt-WiFi pre-config via NM keyfile ------------------
#
# burn_sd_card.sh --mgmt-wifi-ssid drops /boot/firmware/openmarquee-
# mgmt-wifi.nmconnection onto bootfs. Same move + chmod + chown +
# remove-bootfs-copy dance as §5c above, but for the mgmt-WiFi
# keyfile pinned to interface-name=wlan-dongle (the udev-renamed
# USB dongle). No-op when the keyfile is absent — the §5d block
# short-circuits at the `-f` guard, preserving the no-dongle
# fallback path (Option A of qa/r31-dongle-topology-recommendation
# -2026-05-31.md §B.4): without a dongle keyfile, the brcmfmac
# AP+STA captive-portal path on wlan0 is unchanged.
#
# Independent of §5c: the sign-WiFi and mgmt-WiFi keyfiles ship
# as separate burn-time options. An operator can pre-burn both,
# either, or neither.
if [ -f "$BOOTFS_MGMT_NM_KEYFILE" ]; then
    MGMT_DST="${NM_SYSTEM_CONNECTIONS}/openmarquee-mgmt-wifi.nmconnection"
    say "Operator pre-configured mgmt-WiFi keyfile found; moving to $MGMT_DST"
    MGMT_KEY_SSID="$(grep '^ssid=' "$BOOTFS_MGMT_NM_KEYFILE" | head -1 | cut -d= -f2- || true)"
    say "  SSID: ${MGMT_KEY_SSID:-<unparseable>}"
    mkdir -p "$NM_SYSTEM_CONNECTIONS"
    cp "$BOOTFS_MGMT_NM_KEYFILE" "$MGMT_DST"
    chmod 600 "$MGMT_DST" || say "  WARN: chmod 600 failed (test mode?)"
    if [ -z "$ROOT_PREFIX" ] && [ "$(id -u)" -eq 0 ]; then
        chown root:root "$MGMT_DST"
        if command -v nmcli >/dev/null 2>&1; then
            nmcli connection reload 2>/dev/null || true
        fi
    fi
    if rm -f "$BOOTFS_MGMT_NM_KEYFILE" 2>/dev/null; then
        say "  applied; bootfs copy removed"
    else
        say "  applied; could not remove bootfs copy (likely ro bootfs); leaving in place"
    fi
fi
sync

# --- 6. Touch bootstrap marker ----------------------------------------------

say "Marking device as bootstrapped"
touch "$BOOTSTRAP_MARKER"
sync

# --- 7. systemctl disable (best-effort; the unit's ExecStartPost also runs this)
if command -v systemctl >/dev/null 2>&1 && [ -z "$ROOT_PREFIX" ]; then
    systemctl disable openmarquee-firstboot.service 2>/dev/null || true
fi

say "First-boot config done."
say "  SSID: ${SSID}"
say "  (passphrase + QR templated into hostapd.conf + welcome.html)"
