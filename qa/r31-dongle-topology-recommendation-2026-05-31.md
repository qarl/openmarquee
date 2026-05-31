# r31 — Dual-radio USB-WiFi-dongle topology: shipping recommendation

**Author lane:** code2 (static-analysis only — no SSH, no prod ops, no
install.sh re-run). Same shape as
[`qa/r30-install-pip-diagnosis-recommendation-2026-05-31.md`].

**Audience:** code1 / whoever owns the on-device install.sh +
`system/` lane. This doc is the audit + handoff; code1 implements,
verifies, and owns the resulting commits.

**Why it exists.** QA hand-built a dual-radio network topology on FYS
prod tonight (2026-05-31): a Ralink RT5370 USB-WiFi dongle (USB ID
`148f:5370`) attached alongside the Pi's onboard BCM43438. The
hand-built state works live, with two roles split across the two
radios:

- **`wlan1` (dongle)** = always-reach-it management STA → joins
  the operator's home WiFi. NM profile `qarl-dongle`, bound to
  `interface-name:wlan1`, `ipv4.route-metric=50`,
  `autoconnect-priority=10`.
- **`wlan0` (built-in brcmfmac)** = "sign work" — either AP (captive
  portal) OR STA to customer's network. NM profile `nebula` (in
  tonight's test), `interface-name=wlan0`, route-metric 602.
- **`ap0` (virtual on wlan0)** = **retired tonight.** The brcmfmac
  dual-mode AP+STA path was producing
  `brcmf_cfg80211_stop_ap: setting AP mode failed -52` in prod;
  hostapd + `openmarquee-ap0.service` were stopped + masked; the
  dnsmasq `ap0-heal.conf` drop-in moved aside.

For shipping Pis, the question is: how should `scripts/install.sh`
+ `system/` make this default? This doc audits the current code,
proposes the target state, and hands off concrete diffs +
LOC estimates to code1.

---

## Section A — Current `install.sh` + `system/` state (2026-05-31)

Audit of what the inner repo does TODAY regarding network
configuration. All file references against code2 HEAD `d07ec5d`
(= origin/code2). origin/main was `f8fbda3` at the r30 cherry-
pick and is currently `621b4f3` after code1's r32 deploy.sh
emoji-font preservation work; my audit citations are stable on
both heads since the relevant install.sh + system/ files haven't
been touched in the r31..r32 window.

### A.1 Operator home-WiFi STA pre-config — `wlan0` only

- `scripts/burn_sd_card.sh:471-520` writes an NM keyfile to bootfs
  (`/boot/firmware/openmarquee-wifi.nmconnection`) when the operator
  passes `--wifi-ssid`. The keyfile body has
  `interface-name=wlan0` **hardcoded** (`burn_sd_card.sh:498`),
  `autoconnect-priority=100`, `[ipv4] method=auto`. No
  `route-metric=` set, so NM uses the default (~600 for wifi).
- `system/openmarquee-firstboot.sh:358-402` (§5c) detects
  `$BOOTFS_NM_KEYFILE` on first boot, copies it to
  `/etc/NetworkManager/system-connections/openmarquee-wifi.nmconnection`
  with chmod 600 root:root, removes the bootfs copy, and reloads NM.

No dongle / `wlan1` handling. The keyfile is single-purpose: one
home-WiFi profile pinned to one interface.

### A.2 Captive-portal AP — built-in brcmfmac via `ap0`

- `system/openmarquee-ap0.service` is a oneshot ordered
  `Before=hostapd.service NetworkManager.service
  NetworkManager-wait-online.service` and
  `After=sys-subsystem-net-devices-wlan0.device`. ExecStart runs
  `system/openmarquee-ap0-setup.sh`.
- `system/openmarquee-ap0-setup.sh` does:
  `iw dev wlan0 interface add ap0 type __ap` (creates virtual
  __ap vif on the same physical brcmfmac radio), assigns a
  locally-administered MAC (clones wlan0 MAC + sets bit 0x02 + XORs
  last octet), gives `10.0.0.1/24` to ap0, brings it up.
- `system/hostapd.conf` has `interface=ap0` (line 23), `channel=6`
  preference (forced by kernel to wlan0's channel when wlan0 is
  associated).
- `system/dnsmasq.conf` has `interface=ap0` + `bind-interfaces`
  (line 15-16), `dhcp-range=10.0.0.2,10.0.0.50,12h`,
  captive-portal `address=/#/10.0.0.1`.
- `scripts/install.sh:495-510` (§6) inserts the iptables NAT rule
  `PREROUTING -i ap0 -p tcp --dport 80 -j DNAT --to 10.0.0.1:80`
  and persists via `iptables-save` to `/etc/iptables/rules.v4`.

### A.3 Captive-portal channel constraint

`system/README.md:111-122` documents the constraint:

> both virtual interfaces share the same physical chip, so they
> **must run on the same channel**. When `wlan0` associates with
> a home WiFi on channel 11, the kernel silently forces `ap0` onto
> channel 11 too.

The recurring tonight-failure (`setting AP mode failed -52`)
appears to be a deeper instance of the same single-radio
constraint, surfaced as a brcmfmac firmware-side error rather than
a "lost AP" symptom.

### A.4 NetworkManager dropin landscape

- `system/NetworkManager-openmarquee-unmanaged.conf` →
  `/etc/NetworkManager/conf.d/openmarquee-unmanaged.conf`. Content:
  `[keyfile] unmanaged-devices=interface-name:ap0` (line 30). This
  drop-in is the only structural NM directive shipped; it keeps NM
  from trying to manage ap0 (since hostapd owns it).
- `install.sh:418-420` (§5a) is the install step that places it.

### A.5 Periodic NM recovery jobs

Two cron-driven helpers under `scripts/`:

- `scripts/wifi-watchdog.sh` — pings default gateway every 1 min via
  `/etc/cron.d/openmarquee-wifi-watchdog`. On 3 consecutive failures
  restarts NetworkManager. Recovers from brcmfmac firmware wedges
  (the `-110` pattern observed on FYS 2026-05-18).
- `scripts/wifi-preemptive-reload.sh` — restarts NetworkManager
  daily at 03:00 via `/etc/cron.d/openmarquee-wifi-preemptive-reload`.
  Documents (line 8-18) why kernel-module reload isn't used: would
  tear down hostapd + ap0.

These jobs are radio-agnostic (operate on NM, not on
`brcmfmac`/`rt2x00usb`). They keep working unchanged after a
dongle-topology migration. They become *more reliable* if mgmt-STA
moves to a non-brcmfmac dongle (NM restart no longer disrupts the
captive portal's hostapd).

### A.6 USB-WiFi dongle handling

**None.** No udev rule, no NM keyfile, no install.sh detection, no
predictable-naming wrapper, no auto-mount of a dongle profile.

The literal grep
`grep -rni "wlan1\|usb.*wifi\|dongle\|rt5370\|148f:5370" scripts/ system/ docs/`
returns ZERO matches.

For completeness, the related `interface-name` / `wlan0` references that
DO exist (these are context, NOT direct matches for the grep above):

```
scripts/burn_sd_card.sh:498:interface-name=wlan0     # operator home-WiFi keyfile, wlan0-pinned
scripts/install.sh:408:# has no `interface-name=` pin to wlan0 ... (NM-unmanaged comment)
system/NetworkManager-openmarquee-unmanaged.conf:12:# ... `interface-name=` pin to wlan0
system/NetworkManager-openmarquee-unmanaged.conf:30:unmanaged-devices=interface-name:ap0
```

### A.7 Station-mode applier — `backend/openmarquee/wifi_station.py`

- `_STATION_IFNAME = "wlan0"` (line 81) is **hardcoded**, with the
  explicit comment: `"future multi-radio devices would parameterize"`.
- All nmcli subcommands route through this single ifname:
  rescan (line 261: `nmcli device wifi rescan ifname wlan0`),
  connect (`nmcli device wifi connect ... ifname wlan0`),
  device-state queries (line 211: `nmcli device show wlan0`),
  active-connection lookup (line 199: `device == "wlan0"`).
- `_active_connection_for_device()`, `_device_state()`,
  `_is_device_connected()` all key on `_STATION_IFNAME`.

A dongle-topology migration must either (a) keep this pinned to
`wlan0` (so the Settings UI "home WiFi" knob still drives the
*sign* interface) and add a parallel mgmt-dongle path that's
configured pre-boot only, or (b) parameterize across two ifnames
+ add UI to distinguish "sign network" from "mgmt network".

Option (a) is the lower-risk shipping path. Section B below
assumes (a) unless the sub-section is explicitly about Option B.

### A.8 Service masking + start sequence on install

`install.sh:464` masks `hostapd.service dnsmasq.service` *before*
the apt install (so dpkg postinst doesn't auto-start them too
early), then `install.sh:853-857` unmasks + enables + starts:

```bash
systemctl unmask hostapd.service dnsmasq.service || true
systemctl enable openmarquee-backend.service \
                openmarquee-ap0.service \
                hostapd.service \
                dnsmasq.service
...
systemctl reset-failed hostapd.service dnsmasq.service || true
systemctl start openmarquee-ap0.service
systemctl restart hostapd.service
systemctl restart dnsmasq.service
```

This unconditionally brings up the brcmfmac captive-portal AP. The
tonight-FYS state ran counter to this — `hostapd` + `openmarquee-ap0`
were both masked + stopped. **Any auto-detection-of-dongle path
needs to gate this start sequence on "no dongle detected,"** OR
keep the AP on wlan0 even when a dongle is present (the dispatch's
"either AP or STA" framing).

### A.9 Summary of A — what changes for shipping

The audit footprint that needs modification to land the dual-radio
topology:

| Concern | File(s) | Change shape |
| --- | --- | --- |
| Predictable dongle name | NEW `system/99-openmarquee-usb-wlan.rules` + `install.sh` §5b (new) | Add udev rule + copy on install |
| Mgmt-side NM keyfile drop | `scripts/burn_sd_card.sh:471-520`, `system/openmarquee-firstboot.sh:358-402` | Second optional keyfile via `--mgmt-wifi-ssid` flag |
| Auto-detect dongle on first boot | `system/openmarquee-firstboot.sh` (NEW section) | `iw dev` enum + decide topology |
| AP enable/disable gating | `install.sh:853-857` (§8 start sequence) | Conditional on dongle presence |
| Dnsmasq / hostapd no-change | `system/dnsmasq.conf`, `system/hostapd.conf` | Stay bound to ap0 — Option A; only changes under Option B |
| Service masking when dongle present | NEW logic in install.sh §8 | Mask `openmarquee-ap0.service` + hostapd when dongle is detected, OR keep them up but on wlan0-as-AP |
| Documentation | `system/README.md`, `docs/network-topology.md` (NEW) | Document two supported topologies |

---

## Section B — Target state: dual-radio shipping config

### B.1 udev rule for predictable USB-WiFi naming

The dispatch flags the concern: `wlan1` is enumeration-ordering-
dependent. The same physical dongle may show up as `wlan1`,
`wlan2`, or `wlan3` depending on USB enumeration timing and
whether other USB-WiFi devices are present.

**Approach.** Ship a udev rule that renames USB-WiFi devices
matching a known-supported chipset table to `wlan-dongle` (or
`mgmt0`). The rule keys on `KERNEL=="wlan*"`,
`SUBSYSTEM=="net"`, `DRIVERS=="<driver-name>"`, and/or
`ATTRS{idVendor}=="<vid>" ATTRS{idProduct}=="<pid>"`.

For the RT5370 in tonight's setup (USB `148f:5370`, driver
`rt2800usb`):

```udev
# /etc/udev/rules.d/99-openmarquee-usb-wlan.rules
SUBSYSTEM=="net", ACTION=="add", DRIVERS=="rt2800usb", NAME="wlan-dongle"
```

The `DRIVERS==` match is broader than `ATTRS{idProduct}==` and
covers all rt2x00usb-based dongles in one rule (RT5370, RT2870,
RT3070, RT5572, etc. — the same kernel driver covers a large
chunk of the cheap USB-WiFi market). Trading off:

- `DRIVERS==` may also match a second RT-chipset dongle if the
  operator plugged in two — both would race for `wlan-dongle`,
  losing one (udev rejects duplicate names). Mitigation: scope
  shipping to "one dongle assumed" and document; second-dongle
  audit is out-of-scope of v1.x.
- A more conservative match (`ATTRS{idVendor}=="148f"
  ATTRS{idProduct}=="5370"`) is one-dongle-model-only. Easier to
  predict but locks shipping to a specific dongle SKU; the
  documented support matrix would constrain.

**Recommendation:** ship `DRIVERS=="rt2800usb"` first because:

1. The dispatch's hand-built FYS state uses RT5370 today.
2. It covers the wider rt2x00usb chipset family without needing
   per-product rules.
3. A future expansion to other chipsets (Mediatek MT76, Realtek
   RTL88x2BU) adds rules per-driver, NOT per-PID — minimal
   maintenance load.

systemd 257.x (Pi OS Lite trixie per
`[[reference_pi_os_lite_trixie_packages]]`) ships nl80211 +
predictable naming with udev rule support; this is
well-supported on the target image.

### B.2 NM connection profile structure

The shipping defaults need to differentiate mgmt-WiFi from
sign-WiFi unambiguously. Proposed split:

**A. operator-home-WiFi (mgmt) profile** — `wlan-dongle`-pinned

```ini
# /etc/NetworkManager/system-connections/openmarquee-mgmt-wifi.nmconnection
[connection]
id=openmarquee-mgmt-wifi
type=wifi
interface-name=wlan-dongle
autoconnect=true
autoconnect-priority=10
[wifi]
mode=infrastructure
ssid=<operator SSID — burn-time templated>
[wifi-security]
key-mgmt=wpa-psk
psk=<operator PSK — burn-time templated>
[ipv4]
method=auto
route-metric=50
[ipv6]
method=auto
addr-gen-mode=default
```

`route-metric=50` matches the dispatch's tonight FYS state and
ensures Tailscale outbound traffic prefers the mgmt-dongle path.

**B. customer-sign-WiFi profile** — `wlan0`-pinned

```ini
# /etc/NetworkManager/system-connections/openmarquee-sign-wifi.nmconnection
[connection]
id=openmarquee-sign-wifi
type=wifi
interface-name=wlan0
autoconnect=true
autoconnect-priority=10
[wifi]
mode=infrastructure
ssid=<sign-network SSID — operator sets via captive portal Settings UI>
[wifi-security]
key-mgmt=wpa-psk
psk=<operator-set PSK>
[ipv4]
method=auto
route-metric=600
[ipv6]
method=auto
addr-gen-mode=default
```

This is the keyfile `wifi_station.py` writes via `nmcli` when the
operator enters home-WiFi credentials in the Settings UI.
Today's hardcoded `_STATION_IFNAME = "wlan0"` (line 81) keeps
working: this profile *is* the wlan0 STA path.

`route-metric=600` keeps the sign-side WiFi as backup egress;
mgmt path wins via `metric=50` whenever the dongle is associated.

### B.3 NetworkManager.conf drop-in to enforce interface-name binding

Already in place at `system/NetworkManager-openmarquee-unmanaged.conf`
(ap0 stays unmanaged). NO change needed for the dongle topology —
both `wlan-dongle` and `wlan0` are NM-managed by default with their
respective `interface-name=` pins.

If we want to defensively prevent NM from cross-applying a profile
to the wrong interface, add a second entry to the same drop-in's
`unmanaged-devices=` (it's a comma-separated list per NM docs).
Not necessary for correctness — the keyfile `interface-name=` is
sufficient — but Option C below covers it.

### B.4 dnsmasq / hostapd binding choice (Option A vs Option B)

**Option A — keep ap0 as captive-portal AP on wlan0 (recommended for v1.x).**

- `system/dnsmasq.conf`: NO change. Stays bound to ap0
  (`interface=ap0` + `bind-interfaces`).
- `system/hostapd.conf`: NO change. Stays bound to ap0
  (`interface=ap0`).
- `system/openmarquee-ap0-setup.sh`: NO change. Stays
  `iw dev wlan0 interface add ap0 type __ap`.
- AP+STA dual-mode on brcmfmac wlan0 remains the captive-portal
  path. **Implication:** the `setting AP mode failed -52` issue
  is NOT structurally fixed — the dongle topology buys
  mgmt-reliability via the second radio, but the captive portal
  still runs on brcmfmac dual-mode.

**Option B — retire ap0; run captive portal directly on wlan0 in AP-only mode.**

- `system/hostapd.conf`: change `interface=ap0` to
  `interface=wlan0`.
- `system/dnsmasq.conf`: change `interface=ap0` to
  `interface=wlan0`.
- `system/openmarquee-ap0-setup.sh`: deprecated.
- `system/openmarquee-ap0.service`: deprecated.
- `install.sh:6` (iptables rule): change `-i ap0` to
  `-i wlan0`.
- wlan0 becomes EITHER AP OR STA (no dual-mode). The state machine
  needs to flip wlan0 mode based on "are we provisioning?" vs
  "are we operating?".
- **Risk:** the operator can no longer configure a sign-side WiFi
  from the captive portal AND have the captive portal stay up.
  Once they enter sign-WiFi creds, wlan0 has to flip from AP to
  STA, the captive portal goes down, the operator's phone
  disassociates, the sign joins customer WiFi. This is the
  *correct* end-state for normal operation but requires UX to
  guide the operator (probably the existing Settings UI flow
  already covers this implicitly).

Option B is the structurally cleaner shipping topology BUT is a
larger change with new failure modes (e.g. wlan0 mode-flip race
during provisioning). It eliminates the `-52` error class
entirely, which is the long-term win.

**Recommendation:** ship Option A first as a low-risk landing
that solves the mgmt-path reliability via the dongle, defer
Option B to a separate dispatch.

### B.5 Predictable AP-detection logic in firstboot

`system/openmarquee-firstboot.sh` runs on first boot before any
service starts. Proposed extension: detect dongle presence
**after** udev's rule fires, then write the matching mgmt-WiFi
keyfile if both `--mgmt-wifi-ssid` was given at burn time AND
the `wlan-dongle` interface exists.

Detection sketch:

```bash
DONGLE_PRESENT=0
if ip link show wlan-dongle >/dev/null 2>&1; then
    DONGLE_PRESENT=1
fi
```

This run happens after `systemctl daemon-reload` + the udev rule
has fired (udev rules trigger on kernel `add` events; for a
dongle plugged at boot, this is `before` userspace fully
initializes). The udev rule must be in place BEFORE the dongle
appears — but install.sh runs only once we're already past
that point. Bootstrapping order:

- **First flash + first boot WITH dongle attached:** dongle
  appears before install.sh runs; udev fires the default rule
  (`wlan1` name); install.sh then drops the rule + a fresh
  `udevadm trigger --subsystem-match=net` renames to
  `wlan-dongle`. firstboot.sh sees `wlan-dongle`.
- **First boot WITHOUT dongle, dongle hot-plugged later:** udev
  rule already in place; dongle insertion fires udev which
  applies `NAME=wlan-dongle` on first appearance. firstboot.sh
  has long-since run; the *backend's* nmcli reload picks up the
  pre-burned keyfile if present.

### B.6 dnsmasq local DNS fork (tonight FYS state observation)

The dispatch notes:

> dnsmasq still running for local DNS (loopback 127.0.0.1:53)
> tailscale managing /etc/resolv.conf (the DNS fight; parked
> per qarl 2026-05-31)

This is orthogonal to the dongle topology and out-of-scope here.
Flag for a separate audit:
**`dnsmasq`-on-127.0.0.1 vs `tailscale`-managing-`/etc/resolv.conf`**
is a documented open item per the dispatch's parked note. If a
dnsmasq-local-DNS path lands in shipping, it'll need
`system/dnsmasq.conf` rework — but it does NOT block the dongle
topology audit.

---

## Section C — Fallback behavior (no dongle)

The shipping question: many customers will burn a card without
attaching a dongle. Two failure-mode candidates:

### C.1 No-dongle silent fallback (recommended)

- udev rule fires only if `DRIVERS=="rt2800usb"` matches; no
  dongle means no rule applies; no `wlan-dongle` interface
  appears.
- firstboot.sh's `if ip link show wlan-dongle ...` check fails;
  the mgmt-keyfile-drop logic short-circuits.
- install.sh §8's `systemctl enable openmarquee-ap0.service
  hostapd.service dnsmasq.service` plus start sequence runs
  unchanged — the brcmfmac AP+STA captive portal comes up
  exactly as today.
- The pre-existing operator-WiFi keyfile path (drop to
  `system-connections/openmarquee-wifi.nmconnection` with
  `interface-name=wlan0`) keeps working unchanged.

**Net effect for no-dongle Pis:** identical to today's behavior.
Zero regression risk for the existing customer base.

### C.2 Explicit `--with-dongle` flag (NOT recommended)

Adding a flag to `burn_sd_card.sh` (`--mgmt-wifi-ssid` or
`--with-dongle`) is the obvious explicit-opt-in shape. Pros:
operator knows exactly what they're getting. Cons: cardinality of
test matrix doubles, support burden goes up, and the dispatch's
target ("fresh-burn customer signs get it by default") implies
auto-detect, not flag.

**Recommendation:** C.1 is the shipping default. C.2 is the
*configuration shape* — `--mgmt-wifi-ssid <SSID>` at burn time
drops the mgmt-keyfile to bootfs, but the *runtime topology
decision* (use dongle when present) is auto-detect. The two are
orthogonal: an operator can burn with `--mgmt-wifi-ssid` AND
forget the dongle, and the keyfile sits in
`system-connections/` unused until they later plug in a dongle.

### C.3 Hot-plug behavior

If a dongle is plugged in AFTER first boot:

- udev rule fires (rule was installed on first boot).
- NM sees a new `wlan-dongle` interface.
- If a matching mgmt keyfile is in `system-connections/`, NM
  autoconnects (matches `interface-name=wlan-dongle`). Otherwise,
  the interface stays "disconnected" until the operator drops a
  keyfile manually (or runs `nmcli device wifi connect ifname
  wlan-dongle ...`).

No explicit shipping work needed for hot-plug — NM handles it
natively given the keyfile + udev rule are in place.

### C.4 Dongle removed during operation

If the dongle is unplugged at runtime:

- `wlan-dongle` disappears.
- NM marks the mgmt profile as disconnected.
- Tailscale, ssh, etc. fall back to wlan0 (sign-WiFi if associated,
  ap0 captive portal otherwise) — the route table's `metric=600`
  wlan0 default route was always there as backup.
- No explicit handling needed in shipping code. Documents in
  README.

---

## Section D — Concrete diffs / structures

LOC estimates per change; code1 to apply.

### D.1 NEW `system/99-openmarquee-usb-wlan.rules`

```udev
# Predictable USB-WiFi naming for openMarquee mgmt-dongle path.
#
# Rationale: with two WiFi radios attached (built-in BCM43438 +
# USB dongle), the USB dongle's kernel name is
# enumeration-ordering-dependent (wlan1, wlan2, wlan3 depending
# on USB init timing). Pin it to a stable name so NM keyfiles
# can match on interface-name= reliably.
#
# Rule scope: rt2800usb-driver dongles only (RT5370/2870/3070/5572
# family). The hand-tested FYS topology uses an RT5370 at
# USB ID 148f:5370.
#
# Out-of-scope: second-dongle handling. If two rt2800usb dongles
# are present, both would race for wlan-dongle and udev would
# reject the second. Shipping assumes one dongle.
#
# To extend to other chipset families: add a SUBSYSTEM=="net"
# DRIVERS=="<driver>" line per family. Common candidates:
#   - mt76_usb     (MT7601U, MT7610U, MT7612U)
#   - rtl88x2bu    (RTL8812BU / RTL8822BU)
#   - rtl8xxxu     (RTL8188CU, RTL8192CU, etc.)

SUBSYSTEM=="net", ACTION=="add", DRIVERS=="rt2800usb", NAME="wlan-dongle"
```

**LOC:** ~15 (including comments). New file.

### D.2 EDIT `scripts/install.sh` — add §5b udev-rule install

Insert between §5a (NM unmanaged drop-in) and §5.5 (vendored
trixie packages):

```bash
# --- 5b. udev rule for predictable USB-WiFi dongle naming -------------------
#
# Pin rt2800usb-driver dongles to NAME=wlan-dongle so the dual-radio
# mgmt-WiFi NM keyfile can match on interface-name= reliably. Section
# B.1 of qa/r31-dongle-topology-recommendation-2026-05-31.md has the
# rationale.

say "Stage USB-WiFi-dongle udev rule"
UDEV_DST="${ROOT_PREFIX}/etc/udev/rules.d/99-openmarquee-usb-wlan.rules"
run mkdir -p "$(dirname "$UDEV_DST")"
run cp "${OPT_DIR}/system/99-openmarquee-usb-wlan.rules" "$UDEV_DST"

# Reload + trigger so an already-attached dongle gets renamed without
# a reboot. Idempotent: udev's "applied this rule already" check is a
# no-op on re-run.
if [ -z "$ROOT_PREFIX" ] && [ "$DRY_RUN" -eq 0 ]; then
    udevadm control --reload-rules || true
    udevadm trigger --subsystem-match=net --action=add || true
fi
```

**LOC:** ~20.

### D.3 EDIT `scripts/burn_sd_card.sh` — `--mgmt-wifi-ssid` flag

Mirror the existing `--wifi-ssid` flag (lines 101-108) with a
new `--mgmt-wifi-ssid` / `--mgmt-wifi-password` pair. The
keyfile body uses `interface-name=wlan-dongle`:

```bash
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
```

Written to `$BOOTFS/openmarquee-mgmt-wifi.nmconnection` (alongside
the existing `openmarquee-wifi.nmconnection`).

**LOC:** ~60 — flag parsing + heredoc + dry-run paths. Roughly
double the existing `--wifi-ssid` block, since the new flag is
independent.

### D.4 EDIT `system/openmarquee-firstboot.sh` — mgmt-keyfile move

Mirror §5c (`BOOTFS_NM_KEYFILE` → `system-connections/`) with a
new `BOOTFS_MGMT_NM_KEYFILE` path:

```bash
BOOTFS_MGMT_NM_KEYFILE="${BOOTFS_MGMT_NM_KEYFILE:-/boot/firmware/openmarquee-mgmt-wifi.nmconnection}"
# ... (same ROOT_PREFIX prefixing as siblings)

# --- 5d. Phase 4e-b extension: mgmt-WiFi keyfile drop -----------------------
if [ -f "$BOOTFS_MGMT_NM_KEYFILE" ]; then
    DST="${NM_SYSTEM_CONNECTIONS}/openmarquee-mgmt-wifi.nmconnection"
    say "Operator pre-configured mgmt-WiFi keyfile found; moving to $DST"
    # ... (copy + chmod 600 + chown root:root + remove bootfs copy + nmcli reload)
fi
```

**LOC:** ~50, copy-paste of the existing §5c block adjusted for
mgmt naming.

### D.5 EDIT `system/README.md` — document dual-radio topology

A new "Dual-radio shipping topology" section explaining:

- when to attach a dongle (operator who wants always-on mgmt)
- the role split (mgmt-STA on dongle, sign-AP+STA on brcmfmac)
- the burn-time flags (`--mgmt-wifi-ssid` + `--wifi-ssid` for two
  separate networks)
- the fallback (no dongle = today's single-radio topology unchanged)
- the udev rule scope (rt2800usb family today; expanding requires
  a rule addition)

**LOC:** ~80 of markdown.

### D.6 NO change to `system/dnsmasq.conf`, `system/hostapd.conf`, `system/openmarquee-ap0-setup.sh`

Under the recommended Option A (Section B.4), the ap0-based
captive portal stays as the AP path on wlan0. These files do not
change.

### D.7 NO change to `backend/openmarquee/wifi_station.py` (initially)

`_STATION_IFNAME = "wlan0"` continues to drive sign-side WiFi from
the Settings UI. Mgmt-side WiFi is burn-time-configured only;
operators do not adjust mgmt-WiFi via the Settings UI in v1.x
(it would require a parallel UI flow + lock mechanism — out of
scope).

Future-state: parameterize across two ifnames + add UI to
distinguish "sign network" from "mgmt network". Separate
dispatch.

### D.8 install.sh §8 conditional masking — OPTIONAL

The dispatch describes tonight's FYS state as
`hostapd + openmarquee-ap0.service` masked + stopped. Under the
recommended Option A this is NOT necessary — wlan0 hosts both ap0
(for captive portal) and the sign-STA when joined to customer
WiFi. **But** if the `-52` brcmfmac error keeps biting (it bit
tonight on prod), and the operator's sign is already joined to
customer WiFi, the captive portal is dead-weight that may also
destabilize wlan0-STA.

Option: when a dongle is present AND the sign-WiFi profile is
already configured, mask `openmarquee-ap0.service` + hostapd at
install time:

```bash
if ip link show wlan-dongle >/dev/null 2>&1 && \
   [ -f /etc/NetworkManager/system-connections/openmarquee-sign-wifi.nmconnection ]; then
    say "Dongle + sign-WiFi profile present — masking ap0 captive portal"
    run systemctl mask openmarquee-ap0.service hostapd.service
fi
```

**LOC:** ~10.

This is a *judgment call.* Pro: eliminates the `-52` failure
class for the post-provisioning steady-state. Con: removes the
captive-portal reset path if the operator wants to change
sign-WiFi creds later (they'd need to `systemctl unmask
hostapd.service openmarquee-ap0.service` first or reach the sign
via the dongle's mgmt-WiFi).

**Recommendation:** NOT in v1.x. Defer until the Option B audit
(B.4) decides between dual-mode-keep vs ap0-retire.

### D.9 EDIT `scripts/build_sd_bundle.sh` — bundle the udev rule

The bundle script copies `system/` into the bundle root. Confirm
`99-openmarquee-usb-wlan.rules` is picked up by the existing
glob (likely is via `system/*`). Spot-check:

```bash
grep -A5 'cp.*system/' scripts/build_sd_bundle.sh
```

If the file is selected by a glob like `system/*`, no change. If
it's selected by an explicit allow-list, add the rule file. Audit
hand-off to code1 since I haven't read build_sd_bundle.sh in this
session.

**LOC:** 0 if glob, ~3 if allow-list.

### D.10 Order-of-operations summary

The right shipping commit shape, code1's lane:

1. Add `system/99-openmarquee-usb-wlan.rules` (D.1)
2. Edit `scripts/install.sh` §5b to copy the rule + reload udev (D.2)
3. Edit `scripts/burn_sd_card.sh` to support `--mgmt-wifi-ssid` (D.3)
4. Edit `system/openmarquee-firstboot.sh` §5d for the mgmt-keyfile
   drop (D.4)
5. Edit `system/README.md` to document the dual-radio topology (D.5)
6. Verify `scripts/build_sd_bundle.sh` includes the new rule (D.9)
7. Re-burn an SD bundle from main + cross-build the renderer +
   reverify on a Pi with a dongle attached (Section E)

---

## Section E — Shipping consequences

### E.1 SD bundle size impact

Trivial. The udev rule is ~1KB. The optional mgmt-WiFi keyfile
sits in bootfs at ~500B. README addition ~5KB. Net delta to the
SD bundle is **< 10KB**, vs the existing 152.1 MiB bundle. No
shipping-size concern.

### E.2 Documentation surface

- `system/README.md` — required edit (D.5).
- A new `docs/network-topology.md` could capture the design space
  (single-radio vs dual-radio, Option A vs Option B). Optional.
- `IMPLEMENTATION_PLAN.md` in the outer repo — recommend admin-
  Jimmy fold in a "Phase 7.5: dual-radio mgmt topology" milestone
  after this lands. Out-of-lane for me to edit.

### E.3 Testing constraints

The dispatch flags: the Lima/QEMU VM workflow code1 uses for
install.sh sanity-checks **cannot simulate USB dongle insertion**.
A fresh-burn dongle test needs:

- A physical Pi (any Pi 4 or Zero 2 W).
- A physical RT5370 (or any rt2800usb dongle).
- A burn-time `--mgmt-wifi-ssid <ssid>` on a card.
- A boot + sequence verification:
  1. `ip link show wlan-dongle` shows the interface (udev rule fired)
  2. `nmcli connection show --active` shows
     `openmarquee-mgmt-wifi (wlan-dongle)` connected
  3. `nmcli connection show --active` shows
     `openmarquee-sign-wifi (wlan0)` if operator pre-configured
     OR `openmarquee-ap0` AP if not
  4. Tailscale outbound via dongle (`tailscale ping <node>` shows
     a route via wlan-dongle, not wlan0)
  5. Captive portal accessible on the brcmfmac wlan0 AP if no
     sign-WiFi pre-configured

This is a manual test, not an automated CI gate. Document the
checklist in `docs/dual-radio-shipping-test.md` (out-of-scope
for r31 — code1's lane during implementation).

### E.4 Customer-support consequence

If the customer plugs in an unsupported dongle (e.g. a Mediatek
MT76 dongle), the udev rule scoped to `DRIVERS=="rt2800usb"`
will NOT match and the dongle will appear as `wlan1` (kernel
default). The pre-burned `interface-name=wlan-dongle` keyfile
won't match `wlan1`, so the mgmt-WiFi connection silently
fails to autoconnect.

**Mitigation options:**

1. Document the support matrix explicitly: "rt2800usb-family
   dongles only in v1.x."
2. Expand the udev rule over time to cover more chipsets as
   they're validated (one rule per family).
3. Add an out-of-band diagnostic: install.sh detects unmatched
   USB-WiFi devices + logs a warning to
   `/var/log/openmarquee-debug.log`. Cheap to add (~10 LOC).

**Recommendation:** ship (1) + (3). (2) accretes naturally per
chipset request.

### E.5 Renderer + backend regression risk

**Zero.** The dongle topology touches NO Python/Rust code. The
backend's `wifi_station.py` keeps its `_STATION_IFNAME = "wlan0"`
contract intact (sign-side STA). The renderer is unaffected.
The installer changes are additive — fallback to single-radio is
unchanged.

### E.6 Idempotency

All proposed install.sh changes are idempotent (cp + udev reload
+ trigger). All firstboot.sh changes guard on file existence
(`-f $BOOTFS_MGMT_NM_KEYFILE`). All burn_sd_card.sh changes are
flag-gated.

---

## Section F — Open questions for qarl / QA

These can't be resolved from static analysis and need explicit
guidance before code1 implements:

### F.1 Option A vs Option B for the captive portal

This dispatch (Section B.4) recommends Option A: keep ap0 as the
captive-portal AP on wlan0, defer the wlan0-as-AP retire to a
separate audit. **But** tonight's FYS state masked
`openmarquee-ap0.service` + `hostapd.service` entirely. If the
operational intent is "no more ap0," shipping should encode
Option B from the outset and skip the ap0 install path entirely
when a dongle is present.

**Question to qarl:** is the mask of `openmarquee-ap0.service`
+ `hostapd` tonight a *temporary* state for the experiment, or
the *intended shipping configuration* for dongle-equipped signs?

### F.2 Mgmt-WiFi UI surface

Today: operators configure sign-WiFi via the Settings UI; mgmt-WiFi
is pre-burned only. If the operator wants to change the mgmt-WiFi
after first boot, they must either:

- SSH in via the current mgmt-WiFi network and rewrite the keyfile
- Re-burn the SD card with `--mgmt-wifi-ssid <new>`
- Plug in a known-good USB serial console and edit live

This is acceptable for a "support tech sets it once" model. NOT
acceptable for "end-customer reconfigures often."

**Question to qarl:** is mgmt-WiFi a once-set-by-installer
config, or does it need a UI surface for end-customers?

### F.3 Multi-dongle shipping support

The proposed udev rule (`DRIVERS=="rt2800usb"`) breaks if two
rt2800usb dongles are plugged in (race for `NAME=wlan-dongle`).
The shipping promise assumes one dongle.

**Question to qarl:** is "one dongle assumed, second is silently
ignored" acceptable for v1.x? Or do we need a more specific
match (per-PID) and an explicit second-dongle UI?

### F.4 Chipset support matrix

The dispatch's FYS dongle is RT5370. The proposed rule covers
rt2800usb-family (a few hundred SKUs). The next-most-common cheap
dongles are Realtek RTL8812BU (driver `rtl88x2bu`, an out-of-tree
module) and Mediatek MT76 (driver `mt76_usb`, in-tree on trixie).

**Question to qarl:** do shipping Pis bundle out-of-tree drivers
(RTL88x2BU specifically) via .deb in `/opt/openmarquee/debs/`?
Or constrain shipping to in-tree-only chipsets?

### F.5 Backend nmcli applier parameterization

The dispatch frames the dongle as **always-reach-it management**.
If the mgmt path goes down (dongle removed, mgmt-AP turned off),
the sign-side `wifi_station.py` Settings UI flow still works —
but only over the sign-AP's captive portal (wlan0 in AP mode) or
LAN cable (none on Pi Zero 2 W).

**Question to qarl:** if a fielded sign loses the mgmt path AND
isn't joined to a sign-WiFi network, is the captive portal AP
the recovery surface? If yes, Option A is the right v1.x path
(captive portal always available). If no, Option B is simpler.

### F.6 Tonight FYS state — keep or normalize?

The dispatch describes:

- `qarl-dongle` profile uuid `dbda853a` on `wlan1`, priority 10,
  metric 50
- `qarl` profile uuid `adda300e` on `wlan0`, priority -5 (inactive)
- `nebula` profile uuid `3c4b2a12` on `wlan0`, priority 10,
  metric 602 (active for sign work)
- `hostapd` masked, `openmarquee-ap0.service` masked
- dnsmasq still running on 127.0.0.1:53 (local DNS, not captive
  portal)

The shipping target presumably renames `qarl-dongle` →
`openmarquee-mgmt-wifi` + `nebula` → `openmarquee-sign-wifi` so
the customer-facing identifiers are consistent. **Question to
qarl:** should code1's r32 (or wherever this lands) re-burn
FYS with the canonical shipping profiles, or leave the
hand-built UUIDs in place because "it works"? The names today
are operator-recognizable but un-shippable as defaults.

---

## Hand-off shape

1. **Code1 reads this doc** + asks any of the F.x questions back
   to qarl that block their implementation.
2. **Code1 commits the bundle** matching Section D's order:
   udev rule + install.sh §5b + burn_sd_card.sh flag +
   firstboot.sh §5d + README. Single PR / commit-batch on `code1`
   lane; cherry-pick to main.
3. **Code1 burns a fresh SD bundle from main** + cross-builds the
   renderer (per `[[feedback_cross_build_before_deploy]]`) +
   tests with a real dongle attached per Section E.3.
4. **Code1 normalizes FYS to the canonical shipping profile names**
   (if qarl green-lights F.6).
5. **QA verifies** mgmt-path reachability + captive portal still
   accessible on wlan0 + sign-WiFi STA still works + dongle
   hot-plug + dongle hot-unplug.
6. **Admin Jimmy** updates the outer-repo specs to fold in the
   dual-radio topology — likely a new Phase 7.5 milestone in
   `IMPLEMENTATION_PLAN.md`.

---

## Out-of-scope items flagged for follow-up

- **dnsmasq-on-127.0.0.1 vs tailscale-resolv.conf fight** (B.6). The
  dispatch's parked-per-qarl-2026-05-31 note describes this; orthogonal
  to dongle topology, deserves its own static-analysis audit.
- **Option B audit** (B.4): retire ap0, run hostapd directly on
  wlan0 in AP-only mode. Solves the `-52` failure class but
  requires UX rework for the mode-flip.
- **wifi_station.py multi-radio parameterization** (D.7): a UI
  surface for mgmt-WiFi configuration. Not in v1.x.
- **Multi-dongle support** (F.3). Not in v1.x.
- **RTL88x2BU + MT76 support** (F.4). Per-chipset audit per
  customer request.
- **SD-bundle rebuild** including the new udev rule. Adjacent to
  r30's "v1.0.0 SD-bundle rebuild" follow-up.
- **`docs/dual-radio-shipping-test.md`** — manual-test checklist
  for code1 to author during implementation.

— jimmy:openmarquee-code2 (lane: code2 static analysis)
