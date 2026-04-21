#!/usr/bin/env bash
# Bring up a second virtual WiFi interface (`ap0`) alongside the physical
# `wlan0` so the Pi Zero 2 W can host the captive-portal AP AND join a
# home WiFi at the same time on its single onboard radio.
#
# Both virtual interfaces share the one physical chip (BCM43438) and must
# use the same channel — when wlan0 connects to the operator's home WiFi
# on e.g. channel 11, hostapd on ap0 ends up broadcasting on channel 11
# too. Documented in system/README.md.
#
# This script is invoked by `openmarquee-ap0.service` (oneshot) before
# `hostapd.service` starts. Running it twice is safe: the `iw dev` add
# idempotently 17's if the iface already exists, which we tolerate.

set -euo pipefail

AP_IFACE="${AP_IFACE:-ap0}"
PHY_IFACE="${PHY_IFACE:-wlan0}"
AP_IPV4="${AP_IPV4:-10.0.0.1/24}"

# 1. Add the virtual __ap-type interface if it isn't already present.
#    `iw dev wlan0 interface add ap0 type __ap` spawns ap0 on the same
#    physical radio. The brcmfmac driver on BCM43438 accepts this; other
#    chips may not.
if ! ip link show "$AP_IFACE" >/dev/null 2>&1; then
    iw dev "$PHY_IFACE" interface add "$AP_IFACE" type __ap
fi

# 2. Give ap0 a locally-administered MAC distinct from wlan0's. Two
#    virtual interfaces on the same radio can't share a MAC; flipping the
#    locally-administered bit on wlan0's MAC is the canonical choice.
WLAN0_MAC=$(cat /sys/class/net/"$PHY_IFACE"/address)
AP0_MAC=$(python3 -c "
import sys
m = [int(o, 16) for o in sys.argv[1].split(':')]
m[0] |= 0x02   # locally administered
m[0] ^= 0x02   # … then flip back, then differ in a lower octet
m[5] ^= 0x01
print(':'.join(f'{o:02x}' for o in m))
" "$WLAN0_MAC")
ip link set dev "$AP_IFACE" address "$AP0_MAC"

# 3. Assign the captive-portal IP and bring the interface up. dnsmasq
#    will serve DHCP from this address range (see dnsmasq.conf).
ip addr flush dev "$AP_IFACE"
ip addr add "$AP_IPV4" dev "$AP_IFACE"
ip link set dev "$AP_IFACE" up

# 4. Hide ap0 from NetworkManager so NM only manages wlan0 (client mode).
#    `nmcli` is present on Bookworm; older images with dhcpcd only won't
#    have it and this is a no-op.
if command -v nmcli >/dev/null 2>&1; then
    nmcli dev set "$AP_IFACE" managed no || true
fi

echo "ap0 up: $AP_IPV4 on MAC $AP0_MAC (parent $PHY_IFACE mac $WLAN0_MAC)"
