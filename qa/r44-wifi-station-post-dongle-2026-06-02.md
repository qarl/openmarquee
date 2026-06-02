# r44 — wifi_station.py post-dongle consistency sweep

**Author lane:** code1 (self-pickable item I flagged at the end
of r43; cumulative renderer allocator-defense arc is complete).

**Scope:** the `_STATION_IFNAME` comment in
`backend/openmarquee/wifi_station.py` predates r34's dongle
topology and reads as if wlan0 is "the primary interface". r44
brings the comment in line with post-r34 reality (wlan0 is the
SIGN-side radio; wlan-dongle is the management-WiFi radio when
present) + sweeps adjacent surfaces for similar drift.

**Origin/main HEAD at sweep time:** `2486073` (my r43).

---

## §1 — Drift items found

| Item | File:line | Pre-r44 framing | Post-r44 framing | Fixed |
| --- | --- | --- | --- | --- |
| 1 | `backend/openmarquee/wifi_station.py:78-80` | "wlan0 is the BCM43438 radio's primary interface on Pi Zero 2 W; the AP runs on ap0" | Comprehensive role-split docstring citing system/README.md "Dual-radio shipping topology" + r43 Option B status | ✅ |
| 2 | `backend/openmarquee/api_settings.py:332-336` | "the actual apply (template wpa_supplicant-wlan0.conf + systemctl restart + iw poll)" | Updated to describe the actual nmcli implementation (post-Pi-OS-Lite-trixie rewrite per wifi_station.py history) | ✅ |
| 3 | `backend/openmarquee/api_settings.py:500-503` | "re-template the conf + restart wpa_supplicant" | Same nmcli-aware update | ✅ |

Items 2 + 3 weren't strictly "post-r34 drift" — they were pre-
existing doc-rot describing the OLD wpa_supplicant@wlan0
implementation that got replaced by the nmcli rewrite (commit
`6ecd1a2`). The wifi_station.py module's own history block at
its top documents the rewrite; api_settings.py's call-site
comments hadn't caught up. Fixed while in the area per the
dispatch's "fix where natural" guidance.

---

## §2 — Files verified clean (post-r34 reality already documented)

The sweep across `backend/openmarquee/`, `system/`, and `docs/`
found these surfaces already consistent with post-r34 reality;
NO changes needed:

### §2.1 — wifi_station.py rest-of-file

- Module docstring (lines 1-60): describes the nmcli rewrite +
  AP-coexistence + idempotency design choices. All factually
  correct for the sign-WiFi STA scope this module handles. The
  "AP stays up" block (lines 23-31) correctly describes the
  single-radio AP+STA pairing on wlan0+ap0 and explicitly notes
  the AP-coexistence-with-NM scope as separate (task #99).
  **Not stale.**
- Function docstrings + variable names: all use "wlan0" in its
  factually-correct sign-STA scope. **Not stale.**

### §2.2 — backend/openmarquee/ adjacent files

- `wifi_prefill.py`: reads `wpa_supplicant-wlan0.conf` for
  prefill of sign-WiFi credentials. The legacy conf path is
  KEPT as a fallback (per system/README.md line 29). Factually
  correct.
- `api_system.py`: `iw dev wlan0 scan` for the operator-WiFi-
  picker. Scanning is correctly on the sign radio (wlan0)
  because the picker is for the sign's STA-side join.
- `settings.py:244`: "Join the operator's existing WiFi on
  wlan0." Correct in context.

### §2.3 — system/ files

- `openmarquee-ap0.service` + `openmarquee-ap0-setup.sh`: both
  describe the single-radio AP+STA topology on wlan0+ap0
  correctly. The wlan-dongle topology is purely additive (per
  system/README.md line 148-152) and doesn't touch these
  scripts.
- `openmarquee-firstboot.sh`: about MySignXXX generation + AP
  password rotation. No WiFi-interface-naming assumptions.
- `hostapd.conf`, `dnsmasq.conf`, NM-unmanaged keyfile:
  correctly bind to `ap0` (or via the udev rule, to
  `wlan-dongle`); all post-r34 correct.

### §2.4 — docs/

- `system/README.md` §"Concurrent AP + station mode on a single
  radio" (lines 58-122): describes the single-radio baseline
  (wlan0+ap0). Correct as the baseline; the newer §"Dual-radio
  shipping topology" (lines 124-153) describes the additive
  wlan-dongle case correctly.
- `docs/dual-radio-shipping-test.md`: shipped with r34; full
  dongle-aware language. Correct.
- `docs/phase-7-as-built-2026-05-14.md`: historical snapshot
  doc. Mentions wlan0 in correct pre-r34 historical context.
  Editing historical snapshots inappropriately rewrites history;
  **left alone**.

### §2.5 — Backend tests

No test files surfaced wlan0-as-primary assumptions in their
doc strings or assertions. Tests exercise wifi_station + api
behavior, not chip-topology framing.

---

## §F — Outer-repo relay candidates (admin Jimmy)

**None.** The outer-repo `SYSTEM_SPEC.md` (read against current
HEAD via the audit at `~/project/openmarquee/SYSTEM_SPEC.md`):

- §5.11 (live input / takeover) — about WebRTC + camera, not
  WiFi radio topology.
- General WiFi/AP references — none make "wlan0 is primary"
  claims. SYSTEM_SPEC describes captive-portal AP at the user-
  facing level (SSID, on-boarding flow), not the kernel-side
  interface naming.

`IMPLEMENTATION_PLAN.md`: phase history. No interface-naming
assumptions to update.

**No admin Jimmy dispatch needed for r44.**

---

## §G — Open questions for qarl

**None.** Pure doc-only sweep. No design decisions. No
behavior changes.

(Nice-to-have flagged: when Option B implementation lands per
the r43 SHIP verdict, the wifi_station.py `_STATION_IFNAME`
comment should be updated to drop the "Option B retires ap0,
pending implementation dispatch" parenthetical and just say
"Option B is canonical post-vX.Y.Z." That's a one-line
follow-up bundled with the Option B implementation, not r44
scope.)

---

## §H — Behavior preservation

Pure doc/comment changes. Zero source-of-truth modifications:

- wifi_station.py: only `# ...` comment text changed; the
  `_STATION_IFNAME = "wlan0"` value is unchanged.
- api_settings.py: only `# ...` comment text changed; the
  surrounding Python statements are unchanged.

Pre-push hook will run cargo test (no renderer changes →
modified-paths gate likely skips it) + ruff (Python comment
text doesn't affect lint).

---

## Push posture

Doc-only commit. Standard /tmp worktree push.

— jimmy:openmarquee-code1 (lane: r44 wifi_station post-dongle
hygiene sweep)
