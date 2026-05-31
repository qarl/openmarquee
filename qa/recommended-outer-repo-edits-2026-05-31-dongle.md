# Recommended outer-repo v1.x catch-up edits (dongle topology + Option B)

**Author lane:** code2 (recommendation only — subordinate Jimmys
cannot edit the outer-repo canonical specs per
[[reference_repo_topology]]). Same shape as
`code/qa/recommended-outer-repo-edits-2026-05-31.md` which admin
Jimmy openmarquee applied as `da843d1`.

**Audience:** admin Jimmy openmarquee, at qarl's direction.

**Why it exists.** Since admin's `da843d1` v1.0 catch-up landed,
three additive arcs hit inner-repo `origin/main` (HEAD `d635508`)
that the outer-repo canonical specs don't yet reflect:

1. **r34 dual-radio USB-WiFi-dongle shipping topology** (commit
   `b5b9919`, +481 LOC across 7 files). Per code2's r31
   recommendation + qarl's F.1-F.6 answers. Lands the additive
   mgmt-WiFi path on a USB-WiFi dongle, keeping the single-radio
   brcmfmac AP+STA captive portal on wlan0 unchanged (Option A).
2. **Option B captive-portal audit** (commit `d635508`, my r33
   recommendation doc at
   `code/qa/r33-captive-portal-option-b-audit-2026-05-31.md`,
   937 LOC). Audits the future-path of retiring ap0 in favor of
   hostapd-direct-on-wlan0 in AP-only mode. **NOT** scoped to
   v1.x — flagged as v1.x → v2.0 trajectory candidate; gated on
   F.1 (brcmfmac AP-only soak test).
3. **Pre-push hook + deploy resilience** (r29 § through r32 §;
   commits `c1a5e0a` + `687485d` + `621b4f3` + `d06c506` +
   `3cee501`). Hook narrower-classification + aarch64 cross-
   compile gate + virtiofs guard + deploy.sh wheels-refresh + 
   emoji-font preservation + scripts/tests/ no-op
   classification. The aggregate makes the deploy story more
   robust than at v1.0.

The first two are operator-visible / spec-relevant. The third is
mostly inner-repo plumbing but the §10 deploy story (or wherever
it lives) deserves a refresh pointer.

---

## Edit A: `SYSTEM_SPEC.md` — add §4.1.1 dual-radio mgmt dongle topology

**Insertion point:** AFTER line 181 (end of the §4.1 single-radio
WiFi watchdog paragraph), BEFORE the `### 4.2 Captive Portal`
heading at line 183.

**Before:**
```markdown
**WiFi resilience watchdog**: A monitor cron job (`openmarquee-wifi-watchdog`) fires twice a minute (a cron-minute split with a sleep-offset, since cron's smallest unit is 1 minute) and escalates progressively when AP + STA wedges are detected. The first response to a deauth loop or lost-connectivity probe is a NetworkManager restart, which recovers most NM-level stuck states. When three NM restarts land inside a 600-second window — confirming a sustained chip-firmware-level wedge that NM alone can't recover (the brcmfmac long-uptime failure mode) — the watchdog issues a clean system reboot, which re-inits the chip's firmware. The escalation ledger lives in tmpfs so reboot counters can't persist into the next boot and cause loops; the ledger is also wiped explicitly before each reboot call as a belt-and-suspenders guard.

### 4.2 Captive Portal
```

**After:**
```markdown
**WiFi resilience watchdog**: A monitor cron job (`openmarquee-wifi-watchdog`) fires twice a minute (a cron-minute split with a sleep-offset, since cron's smallest unit is 1 minute) and escalates progressively when AP + STA wedges are detected. The first response to a deauth loop or lost-connectivity probe is a NetworkManager restart, which recovers most NM-level stuck states. When three NM restarts land inside a 600-second window — confirming a sustained chip-firmware-level wedge that NM alone can't recover (the brcmfmac long-uptime failure mode) — the watchdog issues a clean system reboot, which re-inits the chip's firmware. The escalation ledger lives in tmpfs so reboot counters can't persist into the next boot and cause loops; the ledger is also wiped explicitly before each reboot call as a belt-and-suspenders guard.

#### 4.1.1 Optional dual-radio: USB-WiFi dongle as mgmt path

An optional USB-WiFi dongle (rt2x00usb chipset family — RT5370/2870/3070/5572) attached to the Pi acts as a second physical radio dedicated to the operator's mgmt-WiFi network, leaving the onboard BCM43438's `wlan0`+`ap0` pair free for sign-side roles.

- **`wlan-dongle`** (mgmt STA) — joins the operator's home WiFi. NM keyfile `openmarquee-mgmt-wifi` pinned to `interface-name:wlan-dongle`, `ipv4.route-metric=50`, `autoconnect-priority=10`. Tailscale outbound prefers this; remote management stays reachable even while the operator reconfigures sign-side networks.
- **`wlan0`** (sign STA) — joins customer-site WiFi (Option A topology unchanged).
- **`ap0`** (captive-portal AP) — created by `openmarquee-ap0.service` (Option A topology unchanged).

A udev rule (`system/99-openmarquee-usb-wlan.rules`, installed by `scripts/install.sh` §5b to `/etc/udev/rules.d/`) renames the dongle from kernel-default `wlan1` to the predictable `wlan-dongle` name. The rule matches `DRIVERS=="rt2800usb"`, covering the rt2x00usb chipset family in a single rule.

Operators pre-configure the mgmt-WiFi credentials at burn time via `scripts/burn_sd_card.sh --mgmt-wifi-ssid <ssid> --mgmt-wifi-password <psk>`. The flag mirrors the existing `--wifi-ssid` for sign-WiFi, dropping a second NM keyfile to bootfs that `openmarquee-firstboot.sh §5d` then moves into `/etc/NetworkManager/system-connections/` with mode 0600.

**No-dongle fallback**: when no rt2800usb dongle is attached, the udev rule never fires; the Pi runs single-radio brcmfmac AP+STA exactly as before. Zero behavior change for the existing customer base.

**Hot-plug**: a dongle plugged in after first boot triggers udev, NM picks up the matching mgmt-keyfile from `system-connections/` and autoconnects.

**Hot-unplug**: `wlan-dongle` disappears; Tailscale falls back to the `wlan0`-STA route (or the captive-portal AP if `wlan0` has no STA association). Sign continues operating.

**Out-of-scope for v1.x**: multi-dongle support, non-rt2x00usb chipsets (Realtek RTL88x2BU, Mediatek MT76), and an end-customer UI for mgmt-WiFi configuration. Burn-time `--mgmt-wifi-ssid` is the only configuration surface. The audit in `code/qa/r31-dongle-topology-recommendation-2026-05-31.md` (commit `015cc3c`) carries the per-question rationale; the implementation lands in `code/qa/r33-captive-portal-option-b-audit-2026-05-31.md`'s F.5 + the inner-repo commit `b5b9919`.

### 4.2 Captive Portal
```

**LOC delta:** +27 lines (the new `#### 4.1.1` subsection).

**Rationale:** the dongle topology is operator-relevant (operators
who want always-reach-it Tailscale buy a $5 dongle), so it belongs
in SYSTEM_SPEC.md. The fact that it's **optional + additive**
keeps the existing §4.1 single-radio shipping language correct as-
is for no-dongle Pis. New sub-section §4.1.1 is the lowest-friction
insertion shape.

---

## Edit B: `IMPLEMENTATION_PLAN.md` — milestone status refresh

**Location:** line 163 (the `**Status as of 2026-05-31**` bullet
inside the Critical-path-for-the-demo block at §5).

Admin's `da843d1` already updated line 163 with the v1.0.0 mention.
The current text needs a single-paragraph extension to reflect the
post-v1.0 dongle + Option B work.

**Before:**
```markdown
**Status as of 2026-05-31**: v0.9.0 shipped 2026-05-30 (tag `a50e928`) — single-device sign controller, fully-shipped Phases 0-7 + 9 plus the post-demo work (HUB75/WS2812B paths still pending Rust port). Text-layer chrome triad (anchor + visible-at-save + weight wire) closed `c38e64d` 2026-05-31. v1.0 is takeable on qarl greenlight; qarl is holding the tag while a few small product calls play out. The demo described below was hit + sailed past months ago — the live system is well beyond it. Phase-by-phase landed-status is captured in each phase section below.
```

**After:**
```markdown
**Status as of 2026-05-31**: v0.9.0 shipped 2026-05-30 (tag `a50e928`) — single-device sign controller, fully-shipped Phases 0-7 + 9 plus the post-demo work (HUB75/WS2812B paths still pending Rust port). Text-layer chrome triad (anchor + visible-at-save + weight wire) closed `c38e64d` 2026-05-31. **v1.0.0 shipped 2026-05-31 (tag `v1.0.0`, commit `57d95db`)** and is deployed on FYS prod. **v1.x track:** dual-radio USB-WiFi-dongle mgmt path landed `b5b9919` (per `code/qa/r31-dongle-topology-recommendation-2026-05-31.md`); Option B captive-portal future-path audited at `code/qa/r33-captive-portal-option-b-audit-2026-05-31.md` (commit `d635508`) and flagged as v1.x → v2.0 trajectory candidate, gated on the F.1 brcmfmac AP-only-mode 7-day soak test. Install + deploy story hardened across r29 → r32 (deploy.sh wheels refresh, emoji-font preservation, pre-push hook narrower classification + aarch64 cross-compile gate + virtiofs guard). The demo described below was hit + sailed past months ago — the live system is well beyond it. Phase-by-phase landed-status is captured in each phase section below.
```

**LOC delta:** +4 lines (~3 new sentences folded into the existing
paragraph; net +4 visible lines after wrap).

**Rationale:** keeps the §5 milestone block as the live-status
anchor. Calls out the dongle topology as v1.x landed work + the
Option B audit as the v2.0 candidate without committing to a ship
timing.

---

## Edit C: `DESIGN_BRIEF.md` — SKIP (no operator-facing change)

**Recommendation: NO EDIT.**

**Rationale:** The dual-radio dongle is an installer-only feature.
The operator-facing experience of openMarquee — "phone connects to
captive portal, types content, sees it on the sign" — is unchanged
by r34. The dongle is invisible to the end-user; it only affects
the **installer's** burn-time flags (`--mgmt-wifi-ssid`) and the
**support tech's** experience (Tailscale stays reachable while the
operator reconfigures sign-WiFi).

The DESIGN_BRIEF.md marketing pitch sells:

- "Plug it into your sign. Connect your phone. Captive portal
  opens." (line 17) — unchanged.
- "Tailscale for remote access from anywhere." (line 21) —
  unchanged. The dongle makes this more reliable but isn't a
  marketing differentiator at this scale.
- "Cost: $55-$115 depending on output mode." (line 73) — the dongle
  is a $5 add-on; not bundled into the base-cost math.

**If qarl WANTS** a marketing surface for the dongle, the right
shape would be a future "**Pro install** — dongle add-on" line
under "What's Next" at line 79. Not necessary for v1.x ship.

**Optionally**: change line 21 from `"Tailscale on the board"` to
`"Tailscale on the board for secure access from anywhere — even
more reliably with an optional USB-WiFi dongle for always-on
management."` adds one sentence + a footnote-shape reference to
the dongle without inflating the pitch. **OPTIONAL.** Default
recommendation: no edit.

---

## Edit D (bonus): `SYSTEM_SPEC.md` §4.1 — stale SSID rotation language

While auditing §4.1 for Edit A's insertion point, I noticed the
following stale claim adjacent to my edit. Same shape as admin's
last bonus sweep ($3.2 HDMI table refresh in `da843d1`).

**Location:** line 174.

**Before:**
```markdown
- **`ap0`** (AP) hosts the captive-portal network phones connect to for setup. `hostapd` binds here; `dnsmasq` serves DHCP in the 10.0.0.x range (device itself at 10.0.0.1; lease pool .2–.50). Default SSID is `openMarquee-XXXX` where `XXXX` is the last four hex chars of the MAC address; default passphrase is `openmarquee` (rotated per-device at first boot).
```

**After:**
```markdown
- **`ap0`** (AP) hosts the captive-portal network phones connect to for setup. `hostapd` binds here; `dnsmasq` serves DHCP in the 10.0.0.x range (device itself at 10.0.0.1; lease pool .2–.50). Cold-boot factory-fresh SSID is `openMarquee-SETUP` with passphrase `change-me-at-first-boot`; `openmarquee-firstboot.service` rotates the SSID to per-device `MySignXXX` (derived from the canonical `device_id` per `code/system/openmarquee-firstboot.sh`, the same identifier used as the Tailscale hostname + sign_name default) and the passphrase to a 16-character alphanumeric+symbol random string on the very first boot.
```

**Rationale:** the prior language said the SSID was `openMarquee-XXXX` (MAC-derived) with passphrase `openmarquee`. The actual current behavior per `code/system/openmarquee-firstboot.sh:150-208`:

- Cold-boot pre-rotation SSID is `openMarquee-SETUP` (from `code/system/hostapd.conf:32` — NOT MAC-derived).
- Cold-boot pre-rotation passphrase is `change-me-at-first-boot` (from `code/system/hostapd.conf:46` — NOT `openmarquee`).
- Post-rotation SSID is `MySign{3 alphanumeric chars}` from `device_id` (per `firstboot.sh:160-162` and `:204-205` — NOT MAC-derived).
- Post-rotation passphrase is 16-char `A-Za-z0-9+_.@-` random (per `firstboot.sh:174-179`).

This stale language predates the `device_id`-as-source-of-truth
arc that landed alongside the per-device SSID/sign_name/Tailscale-
hostname sync principle (see
`code/system/README.md:217-237`).

**LOC delta:** +1 line (one paragraph rewrite, no net line change
after wrap).

---

## Apply order + dependencies

No inter-edit dependencies. Apply order is editor convenience.
Recommended:

1. **Edit B** first (IMPLEMENTATION_PLAN milestone — least
   invasive, single-paragraph extension).
2. **Edit A** (SYSTEM_SPEC §4.1.1 insertion — new sub-section,
   bigger but localized).
3. **Edit D** (SYSTEM_SPEC §4.1 SSID-rotation bonus — touches the
   same §4.1 paragraph as Edit A, easier to do in the same pass).
4. **Edit C** is SKIP unless qarl directs otherwise.

**Total LOC:** ~+32 lines across both files.

**Total apply time estimate:** ~10 min mechanical apply + commit.

---

## Hand-off

`git add SYSTEM_SPEC.md IMPLEMENTATION_PLAN.md` after applying
Edits A + B + D. Commit message suggestion:

```
docs: outer-repo v1.x catch-up — dongle topology + Option B + SSID-rotation polish

Per code2's r34 recommendation doc at code/qa/recommended-outer-
repo-edits-2026-05-31-dongle.md (commit <code2 SHA>).

Edits:
- SYSTEM_SPEC §4.1.1 NEW dual-radio dongle subsection
- IMPLEMENTATION_PLAN §5 milestone refresh — v1.0.0 ship +
  v1.x track items (dongle landed, Option B audited)
- SYSTEM_SPEC §4.1 bonus drift: SSID rotation language now
  matches the firstboot.sh device_id-source-of-truth behavior
```

Out-of-scope: any deeper outer-repo audit (DESIGN_BRIEF.md scope,
phase-by-phase status checkmark sweeps, IMPL_PLAN's Phase 7 vs §5
status reconciliation). Recommend as a separate dispatch if qarl
wants a full freshness pass.

— jimmy:openmarquee-code2 (lane: code2 outer-repo recommendation)
