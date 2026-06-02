# r43 — brcmfmac AP-only audit (Option B critical premise A.6)

**Author lane:** code1 (research audit; doc-only; no code or
system changes).

**Scope:** statically assess whether the brcmfmac driver +
BCM43438 chipset + firmware combination shipped on Pi Zero 2 W
under Raspberry Pi OS Bookworm (kernel 6.12) reliably supports
hostapd in AP-only mode on `wlan0` directly (NO virtual `ap0`
sub-interface, NO concurrent STA), for the openMarquee captive-
portal deployment profile (~5-10 phone clients, sustained over
hours/days, WPA2-PSK).

**Origin/main HEAD at audit time:** `14adb16` (my r42).

**Background:** code2's r33 Option B audit (`d635508` /
`f53ddcd`, 937 LOC; `qa/r33-captive-portal-option-b-audit-2026-05-31.md`)
ranked retire-ap0 as a v1.x.x architecturally-clean path BUT
flagged **critical premise A.6** as the gating prerequisite —
empirical proof that AP-only on wlan0 actually works without
the dual-mode-associated `-52` failures. r43 closes A.6 via
static + community evidence (NO soak testing per
`[[feedback_no_soak_during_dev]]`).

**Recommendation (preview):** **SHIP Option B**, with the
specific conditions + rollback criteria in §C below.

---

## §A — Per-source evidence

### §A.1 — Driver code read (Linux kernel mainline + rpi-6.12.y)

Files: `drivers/net/wireless/broadcom/brcm80211/brcmfmac/{cfg80211.c,feature.c,chip.c}`

**The `-52` error origin:** `cfg80211.c:5221-5223`:

```c
err = brcmf_fil_cmd_int_set(ifp, BRCMF_C_SET_AP, 1);
if (err < 0) {
    bphy_err(drvr, "setting AP mode failed %d\n", err);
    goto exit;
}
```

`-52` = `-EOPNOTSUPP`. **NOT a driver-validation rejection** — it's
the BCM firmware returning EOPNOTSUPP to `BRCMF_C_SET_AP=1`,
propagated upward. The driver-side `-EINVAL` (-22) paths in
`start_ap` are at `:5147` (SSID IE structural issues) and `:5212`
(MBSS 11d mismatch); neither matches the observed failure
signature.

**Critical pre-flip block** at `cfg80211.c:5192-5203`: when the
chip has `!MCHAN && !RSDB` (which the 43430/43438 reports), the
driver issues `BRCMF_C_DOWN=1` → `apsta=0` (**explicitly tearing
down concurrent AP+STA firmware mode**) → `INFRA=1` → `SET_AP=1`.
**BOTH Option A (ap0 virtual) AND Option B (wlan0 direct) end at
the SAME firmware call** (`SET_AP=1`) and the SAME driver code
path. The trigger for `SET_AP=1` rejection differs between
topologies — see §B for adjudication.

**Chipset enumeration:** BCM43438 enumerates in the driver as
`chipID = 43430` (same die family; the "43438" is a Pi/Cypress/
Infineon SKU marker, not a separate brcmfmac branch). The 43430
is NOT in the MBSS-blacklist at `feature.c:321-325` (only 4330 +
43362 are blacklisted). Firmware reports `mbss` but NOT `mchan`
or `rsdb`, putting it on the `!MCHAN && !RSDB` path above.

**Recent 2024-2026 mainline maintenance:**

| Commit | Date | Subject | Relevance |
| --- | --- | --- | --- |
| `3776c685ebe5` | 2025-10-13 | brcmfmac: fix crash in standalone AP Action Frames | **Fixes a CVE in the AP-only path. rpi-6.12.y cherry-picked as `3f8ad41f42b6`** — already in current Pi kernel. |
| `aba23b0a6a0d` | 2024-11-21 | brcmfmac: fix brcmf_vif_clear_mgmt_ies stopping AP | Removes a `vndr ie set error : -52` warning on stop_ap; confirms `-52` is a generic firmware-rejection pattern. NOT in rpi-6.12.y. |
| `df56e58104b6` | 2025-06-06 | brcmfmac: don't allow arp/nd offload if ap mode exists | Orthogonal to A.6. NOT in rpi-6.12.y. |
| `d358795df908` | 2025-08-17 | brcmfmac: support AP isolation | Adds `ap_isolate` iovar. NOT in rpi-6.12.y. |
| `861cb5eb467f` | older | brcmfmac: Fix access point mode | Foundational fix; cited `-52` explicitly. Already in v6.12. |

The active maintenance signal is positive — AP-mode is being
actively patched in 2024-2026, not abandoned.

**Sources:**
- [v6.12 cfg80211.c](https://github.com/torvalds/linux/blob/v6.12/drivers/net/wireless/broadcom/brcm80211/brcmfmac/cfg80211.c)
- [rpi-6.12.y cfg80211.c](https://github.com/raspberrypi/linux/blob/rpi-6.12.y/drivers/net/wireless/broadcom/brcm80211/brcmfmac/cfg80211.c)
- [Commit 3776c685ebe5](https://github.com/torvalds/linux/commit/3776c685ebe5)
- [Commit 861cb5eb467f](https://github.com/torvalds/linux/commit/861cb5eb467f5e38dce1aabe4e8db379255bd89b)

### §A.2 — Hardware datasheet + Pi Foundation evidence

**Cypress / Infineon CYW43438 datasheet** (doc 002-14796):
- PDF binary; AP-mode-specific spec extraction not possible from
  public sources via WebFetch.
- Infineon's modern WHD (Wi-Fi Host Driver) does NOT cover CYW43438
  — this chip is supported via upstream brcmfmac only.
- A vendor-forum thread reports concurrent STA+AP on CYW43438
  works (single-channel only), implying AP-mode is functional.

**Pi Foundation product page:** lists only "2.4GHz 802.11 b/g/n
wireless LAN". No chipset part number. No AP-mode mention. **No
"validated for AP-mode production use" statement exists.** Absence
of vendor endorsement is itself a finding.

**Firmware shipping on Pi OS Bookworm:**
- `/lib/firmware/brcm/brcmfmac43436-sdio.bin` — firmware 9.88.4.77,
  dated 2022-03-31.
- `/lib/firmware/brcm/brcmfmac43436s-sdio.bin` — firmware 7.45.96.s1
  (gf031a129), dated 2023-06-14 (newer Zero 2 W silicon).
- **Both are AP-capable** (advertised via `iw phy` mode flags). No
  firmware bump required for Option B; the AP-mode capability is
  already in-tree.

**Practical capacity limits:**
- No firmware-side client-count cap documented.
- Practical reports on Pi 3B+ class (similar SDIO bandwidth) show
  ~20-client wedges.
- Target (5-10 phones) is well under documented ceilings.

**Capability gaps:**
- brcmfmac AP does NOT support 802.11w (MFP/PMF). Clients requiring
  MFP fail to handshake.
- brcmfmac AP does NOT support ACS (auto channel selection) on this
  chip — channel must be hard-coded (1/6/11).
- No HT40 (capped 20 MHz channel width).

**Sources:**
- [CYW43438 product page](https://www.infineon.com/part/CYW43438)
- [CYW43438 datasheet PDF](https://www.infineon.com/dgdl/Infineon-YW43438_Single-Chip_IEEE_802_11_b_g_n_MAC_Baseband_Radio_with_Integrated_Bluetooth_5-DataSheet-v16_00-EN.pdf)
- [Pi Zero 2 W product page](https://www.raspberrypi.com/products/raspberry-pi-zero-2-w/)
- [RPi-Distro/firmware-nonfree changelog](https://github.com/RPi-Distro/firmware-nonfree/blob/bullseye/debian/changelog)

### §A.3 — Community evidence

**Legacy `-52`:** fixed in 2018 (`861cb5e` — missing `mgmt_stypes`
for AP after `WIPHY_FLAG_HAVE_AP_SME`). Modern `-52` reports are
scattered across channel/regdomain edges, not AP-mode-general.

**Active 2025-2026 brcmfmac AP-only crashes** (all open in rpi-
kernel 6.12 unless noted; primarily diagnosed on BCM43455 [Pi 4/5],
NOT BCM43438):

- **#7033** (Sep 2025): NULL-ptr in `brcmf_p2p_send_action_frame`
  triggered by **iOS 18.6+ tapping SSID info icon**. AP-only on
  Pi 4B BCM43455, kernels 6.6-6.12.34. Upstream fix exists
  (CVE-2025-40321 / `3776c685ebe5`), in 6.17-stable; **already in
  rpi-6.12.y as `3f8ad41f42b6`**. Risk to BCM43438 deployments:
  same code path, same trigger.
- **#6885** (Jun 2025): hostapd segfault, AP-only on Pi 5,
  kernel 6.12.25. Same class as #7033.
- **#7111** (Oct 2025): brcmfmac drops low-ACK IoT clients in
  AP bridge mode. Driver-level, no workaround. NOT phone-relevant
  (modern phones don't hit this).
- **#6876** (May 2025): stale-station retention 27-70s after
  ungraceful power loss. AP-mode general.
- **#7359** (May 2026): WPA3/SAE association rejection. NOT
  WPA2-PSK relevant.
- **#7092**: BCM43455 firmware crash in **concurrent STA+AP** on
  kernel 6.12 ONLY. Workaround: downgrade to Bookworm. **Helps
  Option B's case** — confirms concurrent-mode is the unstable
  surface; single-mode is unaffected on Bookworm.

**RaspAP project** (dominant Pi-AP project): default topology is
**AP-only on `wlan0` in routed mode** — "rigorously tested and
validated." AP+STA dual is explicitly experimental.
**Strong implicit endorsement of AP-only as the safe default.**

**Canonical hostapd-on-Pi pattern:** `wlan0` direct (no `ap0`),
`driver=nl80211` in hostapd.conf (NOT `driver=brcmfmac`), kernel
≥ 6.6.

**Pi Zero 2 W AP-only specific evidence:** **sparse**. Forum
signal dominated by dual-mode failures and STA-mode flakiness.
No long-uptime AP-only datapoints specifically for this chipset.

**Sources:** (full list at end)

---

## §B — Adversarial independent review

The §D dispatch requirement: a subagent ran an INDEPENDENT
verdict from the same §A.1/§A.2/§A.3 inputs **without seeing
my synthesis**, then we compared.

**Subagent's verdict: SHIP.** Same as mine.

**H1 vs H2 adjudication** (per subagent): H1 wins on the
balance, but not decisively. The load-bearing evidence is the
`cfg80211.c:5192-5203` block — the driver explicitly serializes
`DOWN → apsta=0 → INFRA=1 → SET_AP=1` *because* the chip lacks
MCHAN/RSDB. That sequence is a concurrency-fixup the driver
only needs when STA state exists alongside the AP request — i.e.
exactly the dual-mode-specific code path. Option B doesn't
eliminate the `SET_AP=1` call (the line at `:5221` still runs),
but it removes the most common firmware-rejection trigger: stale
STA association/channel state at the moment of mode-flip.

H2-as-applied-to-the-`-52` specifically fails for lack of
evidence. H2-as-applied-to-AP-mode-reliability-overall (the
#7033 crash class, #7111 IoT, etc.) HAS merit — Option B
inherits those — but those are crash classes, not the `-52`
rejection.

### §B.1 — Material disagreements with the consensus (4)

The adversarial review surfaced 4 sharpenings that my lane
synthesis had missed or hedged:

1. **"Track CVE-2025-40321 backport" is theater, not
   mitigation.** Lane B + Lane C both listed it; **but it's
   already in rpi-6.12.y as `3f8ad41f42b6`** (§A.1 table) —
   so this item is moot for our specific kernel target. The
   adversarial framing forced the verify step.
2. **"Monitor dmesg" is aspirational without rollback
   criteria.** "Monitor for crashes" is not a control unless
   we name what observation rolls Option B back. Adopted in
   §C.3 below.
3. **Lanes silent on the 5-10 *phone* clients framing.**
   Phone-heavy = iOS-heavy = #7033 exposure-heavy. The
   mitigation set should be phone-specific, not generic AP.
   Adopted in §C.2 below.
4. **Lane A hedges more than the code warrants.** The
   `cfg80211.c:5192-5203` block is *prima facie* evidence
   that dual-mode is the path-of-most-resistance for the
   firmware. Option B removes that specific resistance
   directly. State this sharply.

### §B.2 — Areas of agreement (the "SHIP with conditions" core)

- All 4 reviews (3 lanes + adversarial) recommend SHIP.
- All agree that Option B is mechanistically a real fix vs the
  most-common `-52` trigger, NOT theater.
- All agree the residual AP-mode-general risks (crash classes
  #7033 / #6885 / #7111) are NOT Option-B-specific — they apply
  to any AP-mode topology on this driver family. Staying on
  Option A doesn't help.

---

## §C — Recommendation: SHIP Option B with conditions

**Verdict: SHIP.** The mechanistic case for Option B closing the
most-common `-52` trigger is sound, residual risks are
topology-agnostic, and the alternative (staying on Option A
with the dual-mode `-52` exposure) is strictly worse on the
same evidence.

### §C.1 — Pre-ship conditions (all must hold)

1. **Kernel: rpi-6.12.y or newer.** Specifically, the kernel
   must contain commit `3f8ad41f42b6` (rpi cherry-pick of
   torvalds `3776c685ebe5`). Confirmed already in current
   Bookworm rpi-6.12 trees.
2. **Firmware: brcmfmac43436[s]-sdio.bin from current Bookworm
   `firmware-nonfree`.** Already shipping; no version bump
   required.
3. **`hostapd.conf` settings:**
   - `driver=nl80211` (not `driver=brcmfmac`; per canonical
     RaspAP + community pattern).
   - `channel=1`, `6`, or `11` HARD-CODED (no `channel=acs_survey`
     or `channel=0` — brcmfmac AP on this chip does NOT support
     ACS per §A.2).
   - `ieee80211w=0` (no MFP/PMF; brcmfmac AP doesn't support per
     §A.2; clients requiring MFP will fail to handshake).
   - `wpa=2` + `wpa_key_mgmt=WPA-PSK` (no WPA3-SAE per §A.3
     #7359).
   - `ht_capab=[SHORT-GI-20]` (NO HT40 per §A.2).
4. **NO concurrent STA on wlan0.** This IS Option B by
   definition. The dispatch's "retire ap0" framing requires
   this — wlan0 is AP-only after the change.
5. **Captive-portal client load profile: ≤ 10 simultaneous
   phones.** Per §A.2 the practical ceiling (Pi 3B+ class) is
   ~20; target is well under, but document the cap so future
   Phase 8 fleet sizing doesn't push it.

### §C.2 — Phone-specific mitigations (per §B.1 disagreement #3)

The 5-10 *phone* client profile is the highest-risk surface
because of #7033 (NULL-ptr from iOS 18.6+ ANQP query on SSID
info tap). Even though the upstream fix is in rpi-6.12.y, the
crash class isn't fully ruled out for BCM43438 (different
chipset, same driver family).

Mitigations:
- **systemd `Restart=on-failure`** on hostapd unit with
  `RestartSec=2s` and `StartLimitBurst=10` so a single firmware
  trap doesn't permanently kill the captive portal. **This is
  the actionable form of "monitor dmesg"** — autorecovery,
  not just observation.
- **`disassoc_low_ack=1`** in `hostapd.conf` — proactively
  disassociates clients with poor reception (helps with the
  #7111 IoT-disassoc class even if phones aren't directly
  affected).

### §C.3 — Rollback criteria (per §B.1 disagreement #2)

Roll back to Option A IF any of the following observed in field
telemetry over 7 days post-deploy:

1. **`setting AP mode failed -52`** appears in journalctl ≥ 1×
   per day across ≥ 3 distinct deployments. (One-off after
   power-cycle is acceptable; sustained pattern means Option B
   isn't the structural fix we expected.)
2. **hostapd OOMs, SIGSEGVs, or NULL-pointer traps** in dmesg
   ≥ 2× per day on any single deployment. (Single trap on
   iOS ANQP is the known #7033 class; sustained pattern means
   the rpi-6.12 backport isn't sufficient for BCM43438.)
3. **Customer reports** of captive-portal failure
   (operator-visible: "I can't connect to MySignXXX"). ≥ 3
   distinct customers in 7 days.

If any threshold trips, code1 + code2 + qarl decide whether to
roll back (revert to Option A topology) or apply a kernel-level
mitigation (cherry-pick more of the §A.1 2024-2026 fixes into
rpi-6.12.y, or surface a kernel-team bug report).

### §C.4 — What would change this recommendation later

The recommendation could be **revised to a stronger or weaker
position** by:

- **Empirical soak data** (not done here per
  `[[feedback_no_soak_during_dev]]`). A 100-cycle test on a
  bench Pi Zero 2 W with iOS-18.6 phone ANQP probes would
  convert the #7033 risk from "extrapolated from BCM43455" to
  "verified on BCM43438".
- **A second Pi Foundation chipset deployment** (different
  flock member with the same BCM43438) showing long-uptime
  AP-only stability OR failure data.
- **Cypress/Infineon publishing an AP-mode capability
  statement** for CYW43438 — unlikely; the chip is end-of-line
  outside the WHD ecosystem.

---

## §D — Recommendation comparison

| Source | Verdict | One-line rationale |
| --- | --- | --- |
| §A.1 driver code | SHIP with caveats | Option B removes the dual-mode `apsta` enable + concurrent state churn that's the most plausible `-52` trigger; `SET_AP=1` call still happens. |
| §A.2 datasheet + Pi | SHIP with risk-acceptance | Pi Foundation ships AP-capable firmware as default; target client count under documented ceiling; recommended mitigations apply. |
| §A.3 community | SHIP — conditional approval | Canonical hostapd-on-wlan0 pattern; legacy `-52` long-fixed; active 2025-2026 bug evidence is BCM43455 not BCM43438; mitigations track. |
| §B adversarial | SHIP | Mechanistically real fix; residual risks topology-agnostic; alternative is strictly worse; with 4 sharpenings (§B.1). |
| **r43 synthesis** | **SHIP** | **Mechanistic case sound; conditions in §C.1; phone-specific mitigations §C.2; rollback criteria §C.3.** |

**No disagreements between independent reviews on the SHIP
verdict.** The adversarial review surfaced 4 sharpenings to
the lanes' soft framing; all 4 adopted into §C above.

---

## §F — Outer-repo relay candidates (for admin Jimmy)

**One candidate, MEDIUM priority.**

### §F.1 — `SYSTEM_SPEC.md §4.1.1` (dual-radio section)

If `SYSTEM_SPEC.md §4.1.1` (or the equivalent section covering
the captive-portal AP topology) currently describes Option A as
the canonical topology, it should be updated to:
- Document Option B as the **shippable v1.x.y direction** (AP-
  only on wlan0; ap0 retired) with the §C conditions inline.
- Annotate the deferred dongle path as an orthogonal capability
  (added when an operator wants management WiFi as a separate
  radio).

Code1 cannot edit the outer repo per the topology rule. **Admin
Jimmy dispatch needed.** Suggested edit shape:

> §4.1.1 Captive-portal topology: Option B (single-radio AP-
> only on wlan0) is the v1.x.y canonical. The legacy ap0
> dual-mode topology is retired pending fleet rollout. Per
> `code/qa/r43-brcmfmac-ap-only-audit-2026-06-02.md` §C,
> conditions are: kernel rpi-6.12.y or newer with
> `3f8ad41f42b6`, firmware-nonfree current, `hostapd.conf`
> `driver=nl80211` + hard-coded channel, no MFP, target ≤10
> phone clients. Rollback criteria documented in audit §C.3.

Recommend: admin Jimmy applies the §F.1 outer-repo edit AFTER
the inner-repo Option B implementation lands (so the spec
reflects shipping reality, not pending intent).

---

## §G — Open questions for qarl

1. **Implementation ordering.** Code2's r33 §B has the concrete
   inner-repo diffs for Option B (DELETE
   `system/openmarquee-ap0.service` etc). Does qarl want to
   dispatch the Option B implementation NOW (since A.6 has
   confirmed SHIP), or hold until the next architectural
   release window?
2. **Pre-flight fleet test.** Before fleet rollout, do we want
   ONE bench-Pi soak (rules-of-engagement-permitting) with
   iOS-18.6 phone ANQP probes to verify the #7033 class is
   actually closed on BCM43438? Per
   `[[feedback_no_soak_during_dev]]` this isn't part of r43,
   but a Phase 8 hardware-test pass might cover it.
3. **Rollback authority.** §C.3 names thresholds but doesn't
   name a decision-maker. Should the rollback decision be
   code1+code2+qarl consensus, or qarl alone, or QA-mediated?
4. **Telemetry surface.** §C.3 requires journalctl + dmesg
   monitoring across fleet deployments. Is there an existing
   telemetry pipeline that surfaces these per-deployment logs
   to a central place, or does Option B ship require building
   that pipeline first?

These are deployment-process questions, not technical blockers.
The audit's SHIP verdict stands regardless of how they resolve.

---

## Push posture

Doc-only commit. Pre-push hook may run cargo test (modified-
paths gate; if `renderer/` isn't touched, cargo step is skipped
per .githooks/pre-push). Standard /tmp worktree push.

— jimmy:openmarquee-code1 (lane: r43 brcmfmac AP-only static
audit; allocator-defense arc complete, this is research not
defense)
