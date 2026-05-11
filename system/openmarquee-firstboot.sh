#!/usr/bin/env bash
# openmarquee-firstboot.sh — runs once on the very first boot of a
# freshly-flashed openMarquee SD card (Phase B.4, closes sweep #5 #2).
#
# Generates a per-device random AP passphrase + MAC-derived SSID
# suffix, writes /var/openmarquee/wifi.json (0600), templates
# /etc/hostapd/hostapd.conf, templates the SSID + password +
# QR-code SVG into /opt/openmarquee/ui/welcome.html, then touches
# /var/openmarquee/.bootstrapped and disables itself (the service
# unit's ExecStartPost also runs systemctl disable as belt-and-braces).
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
HOSTAPD_CONF="${HOSTAPD_CONF:-/etc/hostapd/hostapd.conf}"
WELCOME_HTML="${WELCOME_HTML:-/opt/openmarquee/ui/welcome.html}"
BOOTSTRAP_MARKER="${BOOTSTRAP_MARKER:-/var/openmarquee/.bootstrapped}"
PHY_IFACE="${PHY_IFACE:-wlan0}"

# Allow tests to redirect everything under a tmpdir.
ROOT_PREFIX="${ROOT_PREFIX:-}"
WIFI_JSON="${ROOT_PREFIX}${WIFI_JSON}"
HOSTAPD_CONF="${ROOT_PREFIX}${HOSTAPD_CONF}"
WELCOME_HTML="${ROOT_PREFIX}${WELCOME_HTML}"
BOOTSTRAP_MARKER="${ROOT_PREFIX}${BOOTSTRAP_MARKER}"

say() { printf '==> %s\n' "$*"; }
fatal() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# --- 1. Generate or read AP credentials -------------------------------------

# SSID: openMarquee-<MAC-suffix>. The suffix is the last two bytes of
# wlan0's MAC (e.g. "A3F7"), uppercased. Matches the convention in
# system/hostapd.conf comments + system/README.md feedback memo about
# sign name + AP SSID staying in sync.
derive_ssid_suffix() {
    local mac_path="/sys/class/net/${PHY_IFACE}/address"
    if [ -r "$mac_path" ]; then
        # MAC format: aa:bb:cc:dd:ee:ff -- take last two octets, strip colons, uppercase.
        local mac
        mac=$(cat "$mac_path")
        printf '%s' "${mac##*:}${mac:$((${#mac}-5)):2}" | tr -d ':' | tr 'a-f' 'A-F'
    else
        # Off-device test / WLAN0 absent -- generate a deterministic
        # fallback so test runs don't fail noisily. Real devices
        # always have wlan0.
        printf '%s' "$(od -An -tx1 -N2 /dev/urandom | tr -d ' \n' | tr 'a-f' 'A-F')"
    fi
}

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

WIFI_JSON_REGENERATED=0
if [ -f "$WIFI_JSON" ]; then
    say "Reading existing $WIFI_JSON (idempotent re-run)"
    # Tolerant parse: extract "ssid" and "passphrase" via grep+sed.
    # Cleaner with `jq` but we'd add a system dep just for this.
    SSID=$(sed -n 's/.*"ssid"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$WIFI_JSON")
    PASSPHRASE=$(sed -n 's/.*"passphrase"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$WIFI_JSON")
    [ -n "$SSID" ] || fatal "wifi.json missing ssid"
    [ -n "$PASSPHRASE" ] || fatal "wifi.json missing passphrase"
else
    say "Generating per-device AP credentials"
    SSID_SUFFIX=$(derive_ssid_suffix)
    SSID="openMarquee-${SSID_SUFFIX}"
    PASSPHRASE=$(generate_passphrase)
    WIFI_JSON_REGENERATED=1
fi

# --- 2. Write wifi.json (0600) only when we generated new creds ----------

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

# Substitute {{AP_SSID}} and {{AP_PASSWORD}} placeholders. Use a
# delimiter (|) that won't appear in our charset (which has no pipe).
sed -i.bak \
    -e "s|{{AP_SSID}}|${SSID}|g" \
    -e "s|{{AP_PASSWORD}}|${PASSPHRASE}|g" \
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
