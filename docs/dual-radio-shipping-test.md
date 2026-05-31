# Dual-radio shipping topology — manual test checklist

Manual sanity sweep for `system/99-openmarquee-usb-wlan.rules` +
`burn_sd_card.sh --mgmt-wifi-ssid` + the firstboot mgmt-keyfile
drop. Run this on a physical Pi (any Pi 4 or Zero 2 W) after a
fresh SD burn — Lima/QEMU can't simulate USB-WiFi insertion.

Authored alongside r34 (2026-05-31) per code2's r31 followup.

## Prerequisites

- Physical Pi (Pi 4 or Zero 2 W)
- 1 × RT5370 USB-WiFi dongle (USB ID `148f:5370`), or any other
  rt2800usb-family dongle (RT2870 / RT3070 / RT5572)
- Mac with `burn_sd_card.sh` working (per `docs/sd-burn.md`)
- A test SSID + PSK for the mgmt network (your home WiFi or a
  bench AP)
- A test SSID + PSK for the sign network (a second SSID; can be
  the same physical AP as mgmt if you only have one)
- SD card (16 GB+)

## Sweep — 7 steps

### 1. Fresh SD with `--mgmt-wifi-ssid`, no dongle plugged

```
bash scripts/burn_sd_card.sh /dev/diskN \
    --wifi-ssid "<sign-SSID>" --wifi-password "<sign-PSK>" \
    --mgmt-wifi-ssid "<mgmt-SSID>" --mgmt-wifi-password "<mgmt-PSK>"
```

Insert SD into Pi without the dongle attached. Power on. Wait
~3 min for first boot.

**Expected:**
- Pi boots into the captive-portal AP (`openmarquee-ap0` on
  wlan0) — single-radio Option A fallback path.
- `wlan0` joins `<sign-SSID>` (if a customer-side AP is in range).
- `/etc/NetworkManager/system-connections/` contains BOTH
  `openmarquee-wifi.nmconnection` (the sign keyfile) AND
  `openmarquee-mgmt-wifi.nmconnection` (the mgmt keyfile —
  unused for now, sits waiting).

Verify:
```
ssh openmarquee@<pi>   # over sign-WiFi if joined, else AP
ls /etc/NetworkManager/system-connections/
nmcli connection show
ip link show wlan-dongle 2>&1 | grep -q 'does not exist' \
    && echo "ok: no dongle, no wlan-dongle"
```

### 2. Hot-plug dongle after first boot

Continuing from step 1, plug the dongle in. Wait ~5s for udev
+ NM to enumerate.

**Expected:**
- Kernel sees new USB-WiFi device.
- udev rule fires → device named `wlan-dongle` (NOT `wlan1`).
- NM sees `wlan-dongle`, matches the pre-burned mgmt keyfile,
  autoconnects to `<mgmt-SSID>`.

Verify:
```
ip link show wlan-dongle              # interface exists
nmcli connection show --active        # openmarquee-mgmt-wifi: wlan-dongle
ip route                              # default via wlan-dongle, metric 50 wins
                                      # default via wlan0,       metric ~600 (NM default for wifi, backup)
ping -c2 1.1.1.1                      # mgmt outbound works
```

### 3. Reboot with dongle plugged at boot time

`sudo reboot`. Wait ~2 min.

**Expected:**
- Dongle present at boot → udev fires during kernel init →
  `wlan-dongle` exists before NM starts.
- NM autoconnects mgmt-WiFi cleanly (no race).
- Captive portal still up on wlan0 if no sign-WiFi associated.

**Why this step matters:** the first-flash-WITH-dongle path
needs a reboot to converge. install.sh runs via cloud-init
runcmd AFTER NM has already brought the kernel-named `wlan1`
up; the kernel rejects `NAME=` udev rename on an `IFF_UP`
interface (EBUSY). The rule lands in `/etc/udev/rules.d/` and
takes effect on the NEXT boot. After this first reboot, all
subsequent boots have the rule in place at kernel init and the
rename is clean. See `system/README.md` hot-plug section.

Verify (same as step 2):
```
ip link show wlan-dongle
nmcli connection show --active
```

### 4. udev rename works on a SECOND rt2x00usb chipset

If you have a non-RT5370 rt2x00usb dongle (e.g. RT2870, RT3070,
RT5572), unplug the RT5370 and plug in the other one.

**Expected:** still named `wlan-dongle` (rule keys on the
shared `rt2800usb` driver, not the PID).

Verify:
```
ip link show wlan-dongle
udevadm info /sys/class/net/wlan-dongle | grep -E 'ID_VENDOR|ID_MODEL'
```

If you only have ONE rt2x00usb chipset on hand, skip this
step and note the gap in your verification log.

### 5. ap0 captive portal still works (Option A preservation)

From a phone or laptop, disassociate from your home WiFi and
look for the `openMarquee-Setup` SSID (or whatever the
captive-portal SSID resolves to per `hostapd.conf`).

**Expected:** captive portal AP visible + joinable + the
Settings UI loads. Same behavior as today (no dongle scenario).

This step CONFIRMS that the dongle topology is purely additive:
the brcmfmac AP+STA dual-mode path on wlan0 is unchanged.

### 6. Tailscale outbound prefers mgmt interface

On the Pi:
```
tailscale netcheck
tailscale ping <some-other-tailnet-node>
```

**Expected:** the netcheck output names `wlan-dongle` as the
preferred egress (not `wlan0`), per `route-metric=50` on the
mgmt profile.

If a fielded sign has both dongle + sign-WiFi simultaneously,
mgmt is the management path and sign is the work path — they
should NOT compete.

### 7. Dongle unplugged at runtime (graceful fallback)

Unplug the dongle. Wait ~5s.

**Expected:**
- `wlan-dongle` disappears.
- NM marks the mgmt profile disconnected.
- Default route falls back to wlan0 (sign-WiFi if associated,
  ap0 captive portal otherwise).
- ssh / Tailscale connections still work over wlan0 if the
  Pi is on a routable network.

Verify:
```
ip link show wlan-dongle 2>&1 | grep 'does not exist'
nmcli connection show --active        # no openmarquee-mgmt-wifi
ip route                              # default via wlan0 only
```

Replug to restore.

## Notes

- The rule keys on `DRIVERS=="rt2800usb"`. Non-rt2x00usb dongles
  (Mediatek MT76, Realtek RTL88x2BU) won't be renamed; they'll
  show up as `wlan1` and the mgmt keyfile (pinned to
  `interface-name=wlan-dongle`) won't match. See
  `qa/r31-dongle-topology-recommendation-2026-05-31.md` §F.4 for
  the chipset-matrix discussion.
- Two rt2x00usb dongles plugged simultaneously will race for
  `NAME=wlan-dongle`; udev rejects the second. v1.x assumes
  one dongle. See §F.3.
- `/etc/NetworkManager/system-connections/openmarquee-mgmt-wifi.nmconnection`
  is mode 0600 root:root after firstboot — `nmcli connection
  reload` won't accept it any other way.

## Failure modes + likely causes

| Symptom | Likely cause | Check |
| --- | --- | --- |
| `wlan-dongle` doesn't appear | udev rule didn't install OR not-rt2x00usb dongle | `cat /etc/udev/rules.d/99-openmarquee-usb-wlan.rules`; `udevadm info /sys/class/net/wlan1` |
| `wlan-dongle` exists but mgmt-WiFi doesn't connect | mgmt keyfile not in system-connections/ OR wrong perms | `ls -l /etc/NetworkManager/system-connections/openmarquee-mgmt-wifi.nmconnection` |
| Tailscale prefers wlan0 over wlan-dongle | mgmt keyfile has wrong route-metric | `grep route-metric /etc/NetworkManager/system-connections/openmarquee-mgmt-wifi.nmconnection` (expect 50) |
| Captive portal disappears when dongle plugged | Option A invariant broken; ap0 / hostapd got masked | `systemctl status openmarquee-ap0 hostapd` |
| New NetworkManager.conf drop-in needed | `interface-name=` pin not enforced | `nmcli connection show openmarquee-mgmt-wifi \| grep interface` |
