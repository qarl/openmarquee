# r33 — Captive-portal Option B: retire ap0, run hostapd directly on wlan0

**Author lane:** code2 (static-analysis only — no SSH, no prod ops, no
install.sh changes). Same shape as
[`qa/r30-install-pip-diagnosis-recommendation-2026-05-31.md`] and
[`qa/r31-dongle-topology-recommendation-2026-05-31.md`].

**Audience:** code1 / whoever owns the on-device install.sh +
`system/` lane, in a FUTURE dispatch. **NOT a v1.x ship.**

**Status:** recommendation-only, not for application by code1 in
r34. qarl picked Option A as the v1.x shipping topology (additive:
keep ap0 captive-portal AP on wlan0, add dongle as reliable mgmt
STA via code1's r34 dual-radio impl). Option B is the deferred
structural fix for the brcmfmac single-radio AP+STA failure class
(`brcmf_cfg80211_stop_ap: setting AP mode failed -52`). This doc
audits the migration shape so a future dispatch can implement
when qarl green-lights timing.

**Why it exists.** Option A keeps the ap0 captive portal on the
brcmfmac single-radio AP+STA dual-mode that empirically produces
the `-52` errors. The dongle topology reroutes mgmt access around
the failure but does NOT structurally fix the captive portal's
underlying brcmfmac vulnerability. Option B retires ap0 and runs
hostapd directly on `wlan0` in **AP-only mode** — no dual-mode,
no `-52` class — at the cost of giving up wlan0's STA capability.

**Constraints encoded in this doc:**

- Static-analysis only. Every file:line citation against code2
  HEAD `c0b5fdd` (= origin/code2 + my r32 cherry-pick `d06c506`
  on origin/main).
- Doc-only commit on code2; cherry-pick to main. Zero runtime
  code touched in r33.
- Lane discipline: every operational step explicitly tagged with
  "code1's lane" or "out-of-scope of r33".
- r34 NOT assumed landed. The doc audits today's plumbing; r34's
  `--mgmt-wifi-ssid` + udev `wlan-dongle` are referenced as
  predicates that change the Option B's required-precondition
  surface, but the diffs in Section B work against TODAY's
  install.sh / system/.

---

## Section A — Option A → Option B transition risks

What's in flight on a sign when an installer flips from Option A
(current shipping config) to Option B?

### A.1 Captive-portal session drop (LOW risk, acceptable)

Today's Option A flow:
- Operator's phone joins `ap0` (SSID `MySignXXX`), receives DHCP
  from `dnsmasq` in `10.0.0.x` per
  `system/dnsmasq.conf:25` (`dhcp-range=10.0.0.2,10.0.0.50,12h`).
- Captive-portal DNS intercept (`address=/#/10.0.0.1` per
  `system/dnsmasq.conf:32`) triggers OS-level captive-portal
  detection → phone pops the setup UI.

In Option B:
- `ap0` disappears. `wlan0` becomes the AP.
- Phone's WiFi state machine sees the `ap0` SSID drop and (if
  cached) auto-reconnects to the same SSID NOW on `wlan0` —
  because `system/openmarquee-firstboot.sh:267-281` writes the
  same `MySignXXX` SSID into hostapd.conf regardless of which
  interface hostapd binds.
- DHCP comes back from dnsmasq on wlan0 (same `10.0.0.x` range).

**Net effect:** if the phone caches the SSID, the transition is
nearly transparent. Sub-second drop, automatic reconnect.

**If the phone does NOT cache:** the operator manually reconnects
to the same `MySignXXX` SSID. Acceptable — setup is a one-shot
flow.

**Risk: LOW.** Document the brief disconnect in the migration
notes; no engineering mitigation needed.

### A.2 wlan0-as-STA capability loss (HIGH risk — gates the whole migration)

This is the dominant transition risk.

Today's Option A behavior (verified against `system/README.md:60-122`
+ `backend/openmarquee/wifi_station.py:81`):
- `wlan0` is JOINED to the operator's home WiFi via NM
  (NetworkManager-managed STA).
- The captive portal lets the operator enter customer-WiFi
  credentials; `wifi_station.py` runs
  `nmcli device wifi connect ... ifname wlan0` to associate.
- Customer-WiFi connection remains the device's working network
  connection (the LAN that the renderer / backend / Tailscale
  ride over).

In Option B:
- `wlan0` is AP-only. Cannot simultaneously serve as a STA to
  customer WiFi.
- The "operator enters customer-WiFi creds → sign joins
  customer WiFi" flow MUST migrate to the dongle path
  (`wlan-dongle` from code1's r34 udev rule, presumed landed).
- Customers with no dongle have NO way for the sign to reach
  the internet. The sign is permanently in captive-portal mode.

**Operational consequence:** Option B is gated on "dongle
present." Without a dongle, Option B leaves the sign isolated.

**Decision tree for the future-dispatch implementor:**

| Customer state | Option A (today) | Option B (this audit) |
| --- | --- | --- |
| No dongle, no customer-WiFi | Captive portal only | Captive portal only |
| No dongle, customer-WiFi configured | Sign joins customer-WiFi via wlan0-STA. Captive portal still up on ap0. | **Sign cannot join customer-WiFi.** Captive portal up on wlan0. ❌ regression |
| Dongle present, customer-WiFi configured | Sign joins customer-WiFi via wlan0-STA (sign net) + dongle (mgmt). Captive portal on ap0. | Sign joins customer-WiFi via dongle. Captive portal on wlan0. No regression. |

**Verdict:** Option B is shippable ONLY if dongle adoption is
the operational norm (every customer ships with a dongle, or the
installer enforces dongle presence). See Section D.

### A.3 Tailscale reachability shift (MEDIUM risk — already addressed by r34)

Tailscale rides over whatever interface has the working internet
route. In Option A, this is `wlan0` (STA leg). In Option B, this
is the dongle (`wlan-dongle`) — because wlan0 is now AP-only.

Code1's r34 already establishes the dongle as the always-reach-it
mgmt path with `route-metric=50` (priority over wlan0). So
Tailscale reachability in Option B is THE SAME shape as the
mgmt-WiFi-on-dongle in Option A.

**Net effect:** post-r34, Tailscale reachability is dongle-borne
regardless of Option A or B. No additional migration risk.

### A.4 NM management surface flip

Today's NetworkManager dropin
(`system/NetworkManager-openmarquee-unmanaged.conf:30`):
```
unmanaged-devices=interface-name:ap0
```
Keeps `ap0` unmanaged by NM (hostapd owns ap0). `wlan0` IS managed
by NM (it's the STA leg).

In Option B:
- `wlan0` becomes hostapd's. NM must NOT manage `wlan0`.
- The drop-in needs to flip: `interface-name:wlan0`.
- `ap0` is gone — drop the ap0 entry.

**Risk:** if NM still manages `wlan0` when hostapd tries to bind
it, the same NM-vs-hostapd race that motivated the original Phase
4u (`Before=NetworkManager.service` in
`system/openmarquee-ap0.service:13`) re-emerges. The fix:
unmanage `wlan0` via the dropin BEFORE hostapd starts. Same
shape, different interface name.

**Migration step required:** edit the drop-in in lockstep with
the hostapd.conf interface change. Failing to update the drop-in
leaves NM fighting hostapd for wlan0 → likely manifests as
hostapd `Could not read interface wlan0 flags: No such device`
because NM grabbed it first.

### A.5 iptables persistence

`scripts/install.sh:495-510` (§6) installs a NAT redirect:
```
-t nat -A PREROUTING -i ap0 -p tcp --dport 80 \
    -j DNAT --to-destination 10.0.0.1:80
```
and persists to `/etc/iptables/rules.v4` via `iptables-save`
(`install.sh:516-523`).

In Option B:
- The rule must change to `-i wlan0`.
- The OLD `-i ap0` rule must be purged from `/etc/iptables/rules.v4`
  (otherwise both rules will load at reboot — the ap0 rule
  silently no-ops on a missing iface but accumulates).

**Migration step required:** during the Option B install, run
`iptables -F -t nat` to clear pre-existing NAT rules, then add
the new `-i wlan0` rule, then re-persist. This is a "blast
radius widens" change (the flush hits all NAT rules, not just
ours); needs careful surgery (only flush our rule by `-C`
check-and-delete, then add the new one).

**Surgical alternative:**
```bash
iptables -t nat -C PREROUTING -i ap0 -p tcp --dport 80 \
    -j DNAT --to-destination 10.0.0.1:80 2>/dev/null \
    && iptables -t nat -D PREROUTING -i ap0 -p tcp --dport 80 \
        -j DNAT --to-destination 10.0.0.1:80
iptables -t nat -A PREROUTING -i wlan0 -p tcp --dport 80 \
    -j DNAT --to-destination 10.0.0.1:80
iptables-save > /etc/iptables/rules.v4
```

### A.6 brcmfmac AP-only mode — empirically proven? (CRITICAL GAP)

**This is the largest open question for Option B's viability.**

Today's `-52` failure (`setting AP mode failed -52`) is associated
with the brcmfmac DUAL-MODE AP+STA topology. The diagnosis
implicitly attributes it to the dual-mode race condition. Option
B's structural fix premise — "remove dual-mode → no `-52`" —
assumes the `-52` is unique to dual-mode.

**No evidence in the repo confirms that hostapd-on-wlan0-AP-only
on brcmfmac actually works without the `-52` class.** The
tonight-FYS state masked hostapd + ap0 entirely; it did NOT run
hostapd-on-wlan0-AP-only. Code1's r34 doesn't introduce that
configuration either (r34 keeps Option A's ap0 captive portal).

**Hypothesis to verify before Option B ships:**

H1. The `-52` is exclusive to dual-mode AP+STA on brcmfmac
    → Option B's structural fix is real.

H2. The `-52` is brcmfmac AP-mode-side, regardless of dual or
    single → Option B inherits the same failure class on wlan0
    and changes nothing materially.

**Test required:** on a clean Pi with NO ap0 vif created and
hostapd.conf binding `wlan0` directly, run hostapd + a phone
association cycle 100+ times. If `-52` never fires, H1 confirmed.
If it fires, H2 confirmed → Option B is NOT a structural fix
and the dispatch's framing is wrong.

**No ship without empirical confirmation.** This is the F.1 in
Section F below.

### A.7 Channel selection in AP-only mode

`system/hostapd.conf:27` sets `channel=6` as a preference. In
Option A, this is forced by the kernel to wlan0's STA-side
channel (because both ap0 + wlan0-STA share the radio).

In Option B, wlan0 has no STA association — so `channel=6` is
actually honored. This is a side-benefit of Option B: stable
known channel.

But the channel-selection logic should probably scan for an
unused channel at first boot rather than hard-code 6 (which may
be congested). System/README.md:120-122 already mentions this as
a Phase 9 open item. **Adjacent to Option B but not blocking.**

### A.8 Summary of transition risks

| Risk class | Severity | Owner | Mitigation |
| --- | --- | --- | --- |
| Captive-portal session drop | LOW | Operator-facing | Document; ~1s reconnect |
| wlan0-as-STA capability loss | HIGH (gates ship) | Topology | Require dongle for Option B |
| Tailscale reachability shift | resolved by r34 | None | n/a |
| NM management flip | MEDIUM | Install ordering | Unmanage wlan0 before hostapd |
| iptables persistence drift | MEDIUM | Install logic | Surgical -C/-D/-A migration |
| brcmfmac AP-only proof | **CRITICAL** | Empirical test | Pre-ship hostapd-on-wlan0 cycle test |
| Channel selection | LOW (already deferred) | Phase 9 | n/a (existing scope) |

---

## Section B — Concrete diffs

For each, the exact before/after blocks. LOC estimates per
change. Code1's lane to apply.

### B.1 DELETE `system/openmarquee-ap0.service`

```
git rm system/openmarquee-ap0.service
```

Whole file (29 LOC). Service no longer needed — wlan0 doesn't
need a virtual `__ap` vif because wlan0 IS the AP.

**LOC:** -29 (file deletion).

### B.2 DELETE `system/openmarquee-ap0-setup.sh`

```
git rm system/openmarquee-ap0-setup.sh
```

Whole file (65 LOC). The `iw dev wlan0 interface add ap0 type __ap`
+ MAC assignment + IP assignment logic is all ap0-specific and
obsolete in Option B.

**LOC:** -65 (file deletion).

### B.3 DELETE `system/openmarquee-ap0.service.d/log-to-file.conf`

```
git rm system/openmarquee-ap0.service.d/log-to-file.conf
rmdir system/openmarquee-ap0.service.d
```

ap0 service drop-in for journal logging — obsolete with the
service gone.

**LOC:** -1 file + 1 dir.

### B.4 EDIT `system/hostapd.conf` — bind to wlan0

```diff
- interface=ap0
+ interface=wlan0
```
Line 23 in current file. Comment block at lines 11-22 also needs
a rewrite — it documents the single-radio dual-mode rationale
that no longer applies.

Suggested new comment block:
```
# Phase B (Option B 2026-XX-XX): hostapd binds directly to the
# physical wlan0 radio in AP-only mode. Replaces the earlier
# dual-mode ap0 vif topology that hit brcmfmac
# `setting AP mode failed -52` errors. wlan0-as-STA is no
# longer available; sign-WiFi must use the mgmt dongle path
# from code1's r34 (`wlan-dongle` per the
# `99-openmarquee-usb-wlan.rules` udev rule). See
# qa/r33-captive-portal-option-b-audit-2026-05-31.md for the
# transition rationale.
```

**LOC:** ~15 net (1 line interface change + ~12 lines of
comment-block rewrite + drop a few stale lines).

### B.5 EDIT `system/dnsmasq.conf` — bind to wlan0

```diff
- # Bind only to the AP interface (ap0, the virtual one created by
- # openmarquee-ap0.service). `wlan0` — the physical radio — may be joined
- # to the operator's home WiFi for Tailscale / remote management, and we
- # emphatically don't want to serve DHCP / intercept DNS over there.
- interface=ap0
+ # Bind only to the AP interface (wlan0 in AP-only mode per
+ # Option B). The mgmt-WiFi path runs on a separate `wlan-dongle`
+ # interface (code1's r34); we emphatically don't want to serve
+ # DHCP / intercept DNS over there.
+ interface=wlan0
```
Lines 11-16 in current file.

`bind-interfaces` directive at line 16 is unchanged — it still
means "bind only to the named interface" + ignore everything
else. The interface change is sufficient.

**LOC:** ~6 net.

### B.6 EDIT `system/NetworkManager-openmarquee-unmanaged.conf` — flip to wlan0

```diff
- [keyfile]
- unmanaged-devices=interface-name:ap0
+ [keyfile]
+ unmanaged-devices=interface-name:wlan0
```
Line 30 in current file. Comment block at lines 1-27 needs a
rewrite to drop the ap0 vif rationale (irrelevant under Option
B) and document the NM-vs-hostapd-on-wlan0 ordering rationale.

Suggested new comment:
```
# NetworkManager drop-in: keep wlan0 out of NM's hands entirely.
#
# Under Option B captive-portal topology, wlan0 is hostapd's
# AP-mode interface — NM must not touch it. The mgmt-WiFi STA
# runs on a separate `wlan-dongle` USB-WiFi interface (code1's
# r34); NM manages that one freely.
#
# Without this drop-in, NM tries to bring wlan0 up as a STA
# (matching the legacy openmarquee-wifi.nmconnection keyfile —
# which must be either deleted or repointed to wlan-dongle in
# the Option B migration). hostapd then can't bind: kernel
# refuses two managers on the same iface.
```

**LOC:** ~15 net.

### B.7 EDIT `scripts/install.sh` — drop ap0 plumbing

Multiple sections:

**§3 (line 330) — unit install loop:**

```diff
- for unit in openmarquee-backend.service openmarquee-ap0.service openmarquee-tailscale.service; do
+ for unit in openmarquee-backend.service openmarquee-tailscale.service; do
```

**§3a (line 352) — chmod loop:**

```diff
- for sh_helper in openmarquee-ap0-setup.sh openmarquee-firstboot.sh openmarquee-tailscale.sh; do
+ for sh_helper in openmarquee-firstboot.sh openmarquee-tailscale.sh; do
```

**§6 (line 500-510) — iptables rule:**

Replace the `-i ap0` block with a surgical migration:

```bash
# Option B: NAT redirect now on wlan0 (AP-mode interface). Migrate
# any pre-existing `-i ap0` rule (left over from Option A
# install) by check-and-delete + re-add on wlan0. Idempotent on
# repeat invocation (the check matches the now-wlan0 rule and
# the delete is short-circuited).
IPT_OLD=(
    -t nat -A PREROUTING -i ap0 -p tcp --dport 80
    -j DNAT --to-destination 10.0.0.1:80
)
IPT_OLD_CHECK=(-t nat -C PREROUTING -i ap0 -p tcp --dport 80
               -j DNAT --to-destination 10.0.0.1:80)
if already_done iptables "${IPT_OLD_CHECK[@]}" 2>/dev/null; then
    run iptables -t nat -D PREROUTING -i ap0 -p tcp --dport 80 \
        -j DNAT --to-destination 10.0.0.1:80
fi
IPT_RULE=(
    -t nat -A PREROUTING -i wlan0 -p tcp --dport 80
    -j DNAT --to-destination 10.0.0.1:80
)
IPT_CHECK=(-t nat -C PREROUTING -i wlan0 -p tcp --dport 80
           -j DNAT --to-destination 10.0.0.1:80)
if already_done iptables "${IPT_CHECK[@]}" 2>/dev/null; then
    say "  rule already present; skip"
else
    run iptables "${IPT_RULE[@]}"
fi
```

**§8 (lines 853-875) — enable + start sequence:**

```diff
  run systemctl enable openmarquee-backend.service \
-                     openmarquee-ap0.service \
                      hostapd.service \
                      dnsmasq.service
  ...
  run systemctl reset-failed hostapd.service dnsmasq.service || true
  run ip link set wlan0 up || true
- run systemctl start openmarquee-ap0.service
  run systemctl restart hostapd.service
  run systemctl restart dnsmasq.service
```

The `ip link set wlan0 up` becomes the sole interface bring-up
(was previously belt-and-braces alongside `openmarquee-ap0.service`).

**LOC:** ~25 net (mostly the iptables migration logic).

### B.8 EDIT `system/openmarquee-firstboot.sh` — no change needed (verified)

The hostapd.conf templating at lines 276-281 only substitutes
`ssid=` and `wpa_passphrase=` lines. The `interface=` line is
NOT touched. After Option B's hostapd.conf interface flip, the
firstboot.sh templating still applies cleanly — same sed patterns,
different `interface=` value baked into the source file.

The welcome.html templating at lines 285-298 substitutes
`{{AP_SSID}}`, `{{AP_PASSWORD}}`, `{{DEVICE_ID}}`,
`{{AP_PASSWORD_QR}}` — all of which are interface-agnostic.

**LOC:** 0.

### B.9 NEW migration step — delete sign-WiFi keyfile (optional)

If the device was previously provisioned under Option A with a
sign-WiFi keyfile pinned to `wlan0` (per
`scripts/burn_sd_card.sh:498` writing the `interface-name=wlan0`
keyfile to `system-connections/`), the file is meaningless under
Option B (wlan0 is AP-only; NM cannot drive a STA association
on it).

**Recommendation:** install.sh's Option B migration deletes the
file if present. Best-effort; non-blocking.

```bash
LEGACY_KEYFILE="${ROOT_PREFIX}/etc/NetworkManager/system-connections/openmarquee-wifi.nmconnection"
if [ -f "$LEGACY_KEYFILE" ]; then
    say "Option B migration: removing legacy wlan0-STA keyfile"
    run rm -f "$LEGACY_KEYFILE"
fi
```

Hand-off note: code1's r34 introduces a new
`openmarquee-mgmt-wifi.nmconnection` keyfile for `wlan-dongle`.
That one IS kept under Option B. Only the `openmarquee-wifi.nmconnection`
(legacy Option A sign-WiFi) gets purged.

**LOC:** ~8 LOC.

### B.10 Migration assertion — fail loudly if no dongle present

Option B without a dongle leaves the sign isolated (see Section
A.2). Suggested fail-loud at install time:

```bash
if ! ip link show wlan-dongle >/dev/null 2>&1; then
    fail "Option B requires the USB-WiFi mgmt dongle. Either attach a"
    fail "dongle + reboot, or revert to Option A (Option A install"
    fail "shape preserved on this bundle as a side-by-side install path."
fi
```

Or, softer, a warning + sentinel file so the operator can finish
provisioning under captive-portal-only mode and attach a dongle
later:

```bash
if ! ip link show wlan-dongle >/dev/null 2>&1; then
    say "WARNING: Option B captive portal active without a mgmt dongle"
    say "         Sign will be isolated until a USB-WiFi dongle is plugged in."
    touch /var/openmarquee/.option-b-no-dongle-warning
fi
```

**Recommendation:** ship the warning shape (softer), document the
prerequisite. Hard-fail is operator-hostile; warn-and-document
gives recovery path.

**LOC:** ~12 LOC.

### B.11 DELETE FYS-runtime dnsmasq ap0-heal drop-in (out-of-repo)

The dispatch mentioned the QA-built FYS tonight had:
```
/etc/systemd/system/dnsmasq.service.d/openmarquee-ap0-heal.conf.disabled
```

This file is NOT in the inner repo (`system/dnsmasq.service.d/`
doesn't exist; grep returns zero). It's either:
1. FYS-only ad-hoc (qarl created on-device for the dual-radio
   experiment); won't ship from the repo
2. Added by code1's r34 outside this branch's HEAD; will appear
   on origin/main once r34 lands
3. A planned r35+ artifact

Option B's migration MUST handle this drop-in if it exists on
the target Pi — either delete (no ap0 to heal) or leave the
`.disabled` form in place. **Audit hand-off:** code1 to
re-survey `dnsmasq.service.d/` once r34 lands and update this
list. Out-of-repo today; flag for next-cycle re-audit.

**LOC:** ~5 LOC.

### B.12 Order-of-operations summary

The right Option B migration commit shape:

1. EDIT `system/hostapd.conf` interface + comment (B.4)
2. EDIT `system/dnsmasq.conf` interface + comment (B.5)
3. EDIT `system/NetworkManager-openmarquee-unmanaged.conf`
   interface + comment (B.6)
4. DELETE `system/openmarquee-ap0.service` (B.1)
5. DELETE `system/openmarquee-ap0-setup.sh` (B.2)
6. DELETE `system/openmarquee-ap0.service.d/log-to-file.conf` (B.3)
7. EDIT `scripts/install.sh` — drop ap0 from §3 + §3a + §8 +
   migrate §6 iptables (B.7)
8. EDIT `scripts/install.sh` — add B.9 legacy keyfile cleanup
9. EDIT `scripts/install.sh` — add B.10 no-dongle warning
10. EDIT `scripts/install.sh` — handle B.11 dnsmasq.service.d
    cleanup if needed (per r34's surface)
11. EDIT `system/README.md` to document Option B topology (~80
    LOC of markdown)

**Total LOC delta:** roughly -100 deletions (ap0 plumbing) + ~110
edits/additions (interface flips + iptables migration + warnings
+ docs) = net ~+10 LOC. Big-picture: a SIMPLIFICATION (less
plumbing) once the wlan0 STA loss is accepted.

---

## Section C — Provisioning-flow consequences

### C.1 First-boot operator flow under Option B (with dongle)

1. Operator burns SD card with `--mgmt-wifi-ssid <home>` (per
   code1's r34) + plugs in dongle BEFORE boot.
2. Sign boots. Plymouth splash shows.
3. firstboot.sh runs:
   - Generates MySignXXX identifier
   - Generates AP passphrase
   - Templates hostapd.conf (binds wlan0) + welcome.html + QR
4. install.sh runs:
   - udev rule detects rt2800usb dongle → renames to `wlan-dongle`
   - NM picks up the operator's pre-burned mgmt-WiFi keyfile →
     joins home WiFi on `wlan-dongle`
   - hostapd starts on wlan0 in AP-only mode → broadcasts
     `MySignXXX` SSID
   - dnsmasq starts on wlan0 → serves DHCP in 10.0.0.x range
5. Operator's phone joins `MySignXXX` → captive portal opens →
   operator confirms sign name + (optionally) sign-WiFi
   credentials.

### C.2 Sign-WiFi flag semantics

In Option A, `--sign-wifi-ssid` configures `wlan0` for the sign's
STA. In Option B, wlan0 is AP-only — sign-WiFi STA must go to
the dongle.

But code1's r34 already adds `--mgmt-wifi-ssid` for the dongle.
What's the relationship between `--sign-wifi-ssid` and
`--mgmt-wifi-ssid` under Option B?

**Three options:**

(a) **Merge them.** Under Option B, sign-WiFi and mgmt-WiFi are
the same path (both the dongle). Drop `--sign-wifi-ssid`,
rename `--mgmt-wifi-ssid` to `--wifi-ssid` (the original Option
A name).

(b) **Keep both for forward-compat.** `--mgmt-wifi-ssid`
configures the dongle; `--sign-wifi-ssid` is silently ignored
under Option B (or warns if both given). Useful if Option C
later introduces a second STA interface.

(c) **Add `--sign-wifi-via-dongle` mode-flag.** Operator
explicitly chooses dongle vs (future) other STA paths.

**Recommendation:** (a) for v2.0 simplicity. (b) for a phased
v1.x → v2.0 migration. (c) is over-engineering for current scope.

### C.3 What if the operator entered customer-WiFi creds in the captive portal?

In Option A, the captive portal's "join home WiFi" flow calls
`wifi_station.py.apply()` which runs
`nmcli device wifi connect ... ifname wlan0`. Under Option B,
this fails because wlan0 is AP-only.

**Migration of `wifi_station.py`:**

```diff
- _STATION_IFNAME = "wlan0"
+ _STATION_IFNAME = "wlan-dongle"
```

Single-line change (line 81 in current source). All downstream
nmcli operations now target the dongle.

**Risk:** if `wlan-dongle` doesn't exist (no dongle attached),
all `wifi_station.py` calls fail loudly. The Settings UI surfaces
this via the existing error-state machine. Acceptable per
Section A.2's "Option B requires dongle" framing.

**Out-of-scope of r33 — not a code change in this commit.** Flag
for the Option-B-shipping dispatch.

### C.4 The captive portal's "sign-WiFi" knob

Today, the captive portal exposes a "join home WiFi" form that
configures `wlan0`-STA. Under Option B, this form now configures
the **dongle**-STA — which is also the mgmt path.

This is confusing UX: operators may expect "sign WiFi" to be
distinct from "mgmt WiFi." If they're the same in Option B,
either:

- Rename the field to "WiFi network" (collapse two concepts to
  one).
- Or expose the dongle's interface name explicitly to the operator
  so they understand the topology.

**Recommendation:** rename to "WiFi network" or "Internet
connection." Lowest cognitive load. UX-team's call.

---

## Section D — When to ship Option B

### D.1 v1.x.x patch release

**NO.** Option B is an operator-visible topology change with a
HIGH risk of leaving no-dongle customers isolated. Not a patch-
release shape.

### D.2 v1.x minor release

**POSSIBLE.** If dongle adoption is the operational norm (e.g.,
>80% of shipped signs include a dongle, or all new burns are
dongle-equipped), Option B can ship as a minor-release default
with Option A preserved as a fallback opt-in flag.

Prerequisites:
- Empirical confirmation that hostapd-on-wlan0-AP-only on
  brcmfmac is `-52`-free (Section F.1).
- A clean Option A → Option B in-place migration path (the
  install.sh edits above).
- Per-device feature flag: `--legacy-topology=option-a` or
  similar to allow individual customers to stay on Option A.

### D.3 v2.0 major release

**MOST LIKELY.** v2.0 is the natural shape for an
operator-visible topology shift. By then:
- Dongle adoption has matured.
- The `-52` failure-class field data is accumulated.
- Customer expectations can reset.

Recommendation: target v2.0 unless field data forces an earlier
ship.

### D.4 Field data needed before commit

The dispatch already asked for these. Concretely:

- **% of shipped signs with a dongle attached.** Source:
  fleet-rollup query or support-ticket sampling. Threshold for
  ship: probably >50% before v1.x minor; >90% before deprecating
  Option A.

- **Frequency of brcmfmac `-52` errors.** Source: support
  tickets + journal-log forwarding (if any flock telemetry
  exists). If `-52` is rare (<1% of signs), Option B's
  structural-fix value is low; defer further. If common
  (>10%), urgent.

- **Captive-portal session-drop tolerance.** Hardest to
  measure pre-ship. Could be inferred from setup-flow
  abandonment rate (does the operator complete the setup
  flow in one session, or do they reconnect mid-way?).
  Likely small concern given the < 1s drop.

### D.5 Decision matrix

| Dongle adoption | `-52` frequency | Ship Option B in... |
| --- | --- | --- |
| < 50% | any | v2.0 (don't risk no-dongle isolation) |
| 50-80% | rare (<1%) | v2.0 (low structural-fix value) |
| 50-80% | common (>10%) | v1.x minor with Option A opt-out flag |
| > 80% | rare | v1.x minor as default; deprecate Option A in v2.0 |
| > 80% | common | v1.x minor URGENT |

---

## Section E — Test plan

For when Option B does ship, the manual test plan code1 will
execute. Mirrors the r31 Section E.3 shape.

### E.1 Pre-flight

- A physical Pi (Pi 4 or Pi Zero 2 W).
- An rt2800usb USB-WiFi dongle (e.g. RT5370 at USB ID 148f:5370,
  same as code1's r34 reference hardware).
- A fresh SD card burned with:
  ```
  bash scripts/burn_sd_card.sh /dev/diskN \
      --mgmt-wifi-ssid <operator-home-WiFi-SSID> \
      --mgmt-wifi-password <password>
  ```
  (Or whichever flag(s) the Option-B-shipping dispatch settles
  on per Section C.2.)
- Dongle attached BEFORE boot.

### E.2 First-boot verification

1. Sign boots. Plymouth splash shows.
2. After ~30s, sign broadcasts `MySignXXX` SSID on wlan0
   (channel 6 or kernel-default).
3. `iw dev wlan0 info` shows `type AP`.
4. `iw dev` shows ONLY wlan0 + wlan-dongle (no ap0).
5. `nmcli connection show --active` shows
   `openmarquee-mgmt-wifi (wlan-dongle)` as the only managed STA.
6. `tailscale ping <other-tailnet-node>` works (routes via
   dongle).

### E.3 Captive-portal flow

7. Operator's phone joins `MySignXXX` → captive portal opens.
8. DHCP lease in 10.0.0.x range.
9. Welcome page loads at http://10.0.0.1/.
10. Operator can complete setup flow (sign name,
    optional sign-WiFi creds).

### E.4 Steady-state verification

11. Sign serves renderer + backend + Tailscale management.
12. Pull the captive-portal phone away; sign continues running.
13. NO `brcmf_cfg80211_stop_ap` errors in
    `journalctl -b -u hostapd` over 24h soak.
14. `cat /var/log/openmarquee-debug.log | grep -i "stop_ap\|-52"`
    returns no matches.

### E.5 Stress / fault-injection

15. Unplug dongle mid-operation. Verify:
    - Captive portal remains accessible (wlan0 AP unaffected).
    - Tailscale connectivity lost.
    - Setting UI's "join WiFi" flow surfaces the dongle-missing
      error.
    - Sign returns to mgmt-WiFi reachability when dongle
      re-attached.
16. Re-plug dongle. Verify NM picks up the mgmt keyfile +
    rejoins home WiFi.
17. Soak hostapd on wlan0 alone for 7 days. Verify NO `-52`
    failures (the H1 hypothesis from Section A.6).

### E.6 No-dongle test

18. Burn a fresh card with NO `--mgmt-wifi-ssid` flag, NO
    dongle attached.
19. Boot. Verify install.sh's B.10 warning fires + sentinel
    file at `/var/openmarquee/.option-b-no-dongle-warning`.
20. Captive portal still accessible.
21. Operator can complete setup flow BUT sign cannot reach
    internet.

### E.7 Migration test (Option A → Option B in place)

22. Provision a Pi under Option A (current shipping config).
    Get to steady-state with sign-WiFi configured.
23. Run a hypothetical `migrate-to-option-b.sh` script (which
    would be the install.sh equivalent post-r33-implementation).
24. Verify:
    - ap0 vif removed.
    - hostapd now binds wlan0.
    - iptables NAT rule migrated from `-i ap0` to `-i wlan0`.
    - Legacy `openmarquee-wifi.nmconnection` keyfile deleted.
    - Captive portal remains accessible throughout the
      migration (or at most briefly disconnects).
    - Sign reconnects to customer WiFi via dongle.

### E.8 Acceptance criteria

- All E.2-E.4 pass.
- E.5 stress tests survive 7-day soak with no `-52` errors.
- E.6 documents the no-dongle isolation behavior matches the
  install.sh warning text.
- E.7 in-place migration completes in < 60s with < 5s captive-
  portal downtime.

---

## Section F — Open questions for qarl / QA

### F.1 brcmfmac AP-only viability (CRITICAL)

**The whole audit's premise rests on this.** Section A.6's H1
hypothesis: the `-52` errors are exclusive to brcmfmac
dual-mode AP+STA. If `-52` also fires on AP-only-on-wlan0,
Option B is NOT a structural fix.

**Question to qarl:** before scoping Option B's ship, run the
E.5/E.5/17 soak test on a Pi with hostapd-on-wlan0-AP-only
(no ap0, no STA). If clean for 7 days, H1 confirmed. If not,
this audit's premise is wrong and the whole Option B path
needs re-evaluation.

### F.2 `--sign-wifi-ssid` vs `--mgmt-wifi-ssid` semantics

Per Section C.2: under Option B, sign-WiFi and mgmt-WiFi are
the same path (both go to the dongle). Three migration options
proposed (merge, keep-both, mode-flag).

**Question to qarl:** which shape for the burn-time flag UX?

### F.3 No-dongle behavior

Per Section A.2 + B.10: Option B without a dongle leaves the
sign isolated. Fail-loud at install vs warn-and-document?

**Question to qarl:** what's the operator experience target?
"Hard requirement; sign refuses to install without a dongle" or
"Soft requirement; sign installs but warns of isolation"?

### F.4 In-place migration vs fresh-burn

Option B can be applied via in-place migration of an Option-A
sign (in-place install.sh re-run), or by re-burning the SD card.

**Question to qarl:** which migration path is supported?
- (a) Re-burn only — clean state, simpler installer
- (b) In-place — preserves customer's existing settings, more
  complex installer

### F.5 dnsmasq.service.d/openmarquee-ap0-heal.conf provenance

The dispatch mentioned this file at runtime on FYS; not in the
inner repo today. Per Section B.11.

**Question to qarl + code1:** is this file (a) ad-hoc FYS-only,
(b) part of r34, (c) planned r35+? Option B migration list
needs to know to include or skip.

### F.6 Channel selection under Option B

Per Section A.7: `channel=6` is currently a preference forced
by the kernel; under Option B it's actually honored. Hard-coded
channel may be congested. Phase 9 was supposed to add scan +
pick-unused logic.

**Question to qarl:** is Phase 9 channel-auto-selection a
prerequisite for Option B, or shippable separately?

### F.7 Option-B's effect on the `openmarquee-firstboot.sh` UX

Under Option A, the firstboot welcome.html says "connect your
phone to MySignXXX and enter your home WiFi creds." Under
Option B, "your home WiFi" might be either the dongle's mgmt
WiFi (already configured at burn time) or a new sign-WiFi
(same dongle, different config). The UI copy needs revision.

**Question to qarl:** what's the welcome.html copy strategy?
Single "WiFi network" field, or two ("mgmt WiFi" + "sign WiFi"
— though they collapse to one in Option B)?

---

## Hand-off shape

1. **qarl reviews this doc** + answers F.1 (the critical
   prerequisite empirical question).
2. **F.1 verification step.** Code1 (or QA) runs the
   hostapd-on-wlan0-AP-only soak test for 7 days. If clean,
   proceed.
3. **qarl decides** the v1.x-minor vs v2.0 timing per Section D.
4. **A future code1 dispatch** applies Section B's diff bundle
   AND `wifi_station.py:81` interface rename to wlan-dongle (per
   Section C.3) AND welcome.html UX revision (per F.7).
5. **Code1 burns a fresh SD bundle** from the Option B branch +
   cross-builds the renderer + tests with a real dongle attached
   per Section E.
6. **QA verifies** all Section E acceptance criteria.
7. **Admin Jimmy** updates outer-repo specs to document the
   Option A → Option B topology shift.

---

## Out-of-scope items flagged for follow-up

- **F.1 empirical soak test** is THE blocking prerequisite.
  Without it, this audit's premise is unverified.
- **`wifi_station.py:81` interface rename** to `wlan-dongle`
  belongs in the Option-B-shipping dispatch.
- **welcome.html UX copy** revision (per F.7).
- **Channel auto-selection** (per A.7 + F.6) — Phase 9 open
  item, adjacent.
- **In-place migration shell script** (vs re-burn) per F.4.
- **Outer-repo IMPLEMENTATION_PLAN milestone** for Option B
  ship — admin Jimmy's lane.

— jimmy:openmarquee-code2 (lane: code2 static analysis)
