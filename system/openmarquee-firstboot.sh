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

set -euo pipefail

WIFI_JSON="${WIFI_JSON:-/var/openmarquee/wifi.json}"
IDENTITY_JSON="${IDENTITY_JSON:-/var/openmarquee/identity.json}"
HOSTAPD_CONF="${HOSTAPD_CONF:-/etc/hostapd/hostapd.conf}"
WELCOME_HTML="${WELCOME_HTML:-/opt/openmarquee/ui/welcome.html}"
BOOTSTRAP_MARKER="${BOOTSTRAP_MARKER:-/var/openmarquee/.bootstrapped}"
ETC_HOSTNAME="${ETC_HOSTNAME:-/etc/hostname}"
ETC_HOSTS="${ETC_HOSTS:-/etc/hosts}"
PHY_IFACE="${PHY_IFACE:-wlan0}"

# Allow tests to redirect everything under a tmpdir.
ROOT_PREFIX="${ROOT_PREFIX:-}"
WIFI_JSON="${ROOT_PREFIX}${WIFI_JSON}"
IDENTITY_JSON="${ROOT_PREFIX}${IDENTITY_JSON}"
HOSTAPD_CONF="${ROOT_PREFIX}${HOSTAPD_CONF}"
WELCOME_HTML="${ROOT_PREFIX}${WELCOME_HTML}"
BOOTSTRAP_MARKER="${ROOT_PREFIX}${BOOTSTRAP_MARKER}"
ETC_HOSTNAME="${ROOT_PREFIX}${ETC_HOSTNAME}"
ETC_HOSTS="${ROOT_PREFIX}${ETC_HOSTS}"

say() { printf '==> %s\n' "$*"; }
fatal() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

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
if [ -f "$ETC_HOSTS" ]; then
    if grep -q "^127\.0\.1\.1" "$ETC_HOSTS"; then
        sed -i.bak "s/^127\.0\.1\.1.*/127.0.1.1\t${DEVICE_ID}/" "$ETC_HOSTS"
        rm -f "${ETC_HOSTS}.bak"
    else
        printf '127.0.1.1\t%s\n' "${DEVICE_ID}" >> "$ETC_HOSTS"
    fi
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

# --- 6. Touch bootstrap marker ----------------------------------------------

say "Marking device as bootstrapped"
touch "$BOOTSTRAP_MARKER"

# --- 7. systemctl disable (best-effort; the unit's ExecStartPost also runs this)
if command -v systemctl >/dev/null 2>&1 && [ -z "$ROOT_PREFIX" ]; then
    systemctl disable openmarquee-firstboot.service 2>/dev/null || true
fi

say "First-boot config done."
say "  SSID: ${SSID}"
say "  (passphrase + QR templated into hostapd.conf + welcome.html)"
