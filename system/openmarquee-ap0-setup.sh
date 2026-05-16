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
#    virtual interfaces on the same radio can't share a MAC; flipping
#    the locally-administered bit on wlan0's MAC is the canonical
#    choice (RFC: bit 0x02 of the first octet signals "this MAC was
#    not assigned by the IEEE OUI registry"). 19.4 / sweep #10 #9:
#    the previous code did `m[0] |= 0x02; m[0] ^= 0x02` which is a
#    no-op when the bit was already 0. Wlan0's onboard MAC has bit
#    0x02 clear (it IS IEEE-assigned), so the previous code left
#    bit 0x02 clear too -- the new MAC was still "globally unique"
#    in the IEEE sense, and most kernels accept it anyway since
#    only the bit-1 multicast flag is rejected, but the intent of
#    the script was always "set the LA bit" and the code didn't.
WLAN0_MAC=$(cat /sys/class/net/"$PHY_IFACE"/address)
AP0_MAC=$(python3 -c "
import sys
m = [int(o, 16) for o in sys.argv[1].split(':')]
m[0] |= 0x02   # set locally-administered bit (RFC 802; was a no-op before 19.4)
m[5] ^= 0x01   # differ from wlan0 in last octet so the two MACs aren't identical
print(':'.join(f'{o:02x}' for o in m))
" "$WLAN0_MAC")
ip link set dev "$AP_IFACE" address "$AP0_MAC"

# 3. Assign the captive-portal IP and bring the interface up. dnsmasq
#    will serve DHCP from this address range (see dnsmasq.conf).
ip addr flush dev "$AP_IFACE"
ip addr add "$AP_IPV4" dev "$AP_IFACE"
ip link set dev "$AP_IFACE" up

# 4. (Phase 4u, 2026-05-16) Hiding ap0 from NetworkManager is now handled
#    by /etc/NetworkManager/conf.d/openmarquee-unmanaged.conf (installed
#    by install.sh §5a from system/NetworkManager-openmarquee-unmanaged.conf).
#    The previous in-script `nmcli dev set ap0 managed no` ran while
#    Before=NetworkManager and was silently lost — NM hadn't started yet
#    to receive the command. The conf.d drop-in is read at NM startup
#    so the directive is in place before NM ever sees ap0.

echo "ap0 up: $AP_IPV4 on MAC $AP0_MAC (parent $PHY_IFACE mac $WLAN0_MAC)"
