# Onboarding rework plan — AP+STA concurrency on Pi Zero 2 W

**Status:** P0 plan, awaiting QA review-in-parallel; P1 work proceeds.
**Canonical spec:** `~/project/openmarquee/qa/spec-onboarding-ap-sta-concurrent-2026-06-10.md` (qarl-direct, 2026-06-10).
**Author:** jimmy:openmarquee-code2 (acting on QA-DISPATCH onboarding-P0).

This plan exists in the code repo so review history lives next to
the implementation. The canonical spec is the source of truth for
architecture decisions; this plan is the implementation strategy.

---

## §A — Audit: spec "likely contributors" vs current code

The spec lists 6 likely contributors to today's intermittent
AP+STA failures. Mapped against the code at main `395c4c2`:

| # | Contributor | Status | Evidence |
|---|---|---|---|
| 1 | Channel mismatch | **PRESENT (high)** | `system/hostapd.conf:27` hard-codes `channel=6`. No supervisor reads `iw dev wlan0 link`/`wpa_supplicant STATUS` to derive AP channel. The comments at `hostapd.conf:17-22` + `openmarquee-ap0-setup.sh:7-8` *acknowledge* the same-channel constraint but rely on undocumented kernel-side channel forcing rather than explicit channel-follow. This is the spec's #1 root cause and is unmitigated. |
| 2 | WiFi power saving | **PRESENT (warned only)** | `scripts/wifi-watchdog.sh:326-341` greps `iw dev wlan0 get power_save`; on ON it WARNs but does NOT enforce off. There is no boot-time service that calls `iw dev wlan0 set power_save off`. Watchdog is only installed via cron and is reactive, not proactive. |
| 3 | NetworkManager contention | **PARTIAL** | `system/NetworkManager-openmarquee-unmanaged.conf` unmanages `ap0` but `wlan0` is STILL NM-managed. The current architecture is a hybrid: NM owns wlan0 STA (via `backend/openmarquee/wifi_station.py` → nmcli) + hostapd owns ap0 AP. Spec wants BOTH interfaces taken from NM with wpa_supplicant directly on wlan0. The hybrid is closer to the comitup pattern than the supervisor-owned-radio pattern the spec recommends. |
| 4 | Identical MACs | **HANDLED** | `openmarquee-ap0-setup.sh:30-44` sets the locally-administered bit on the first octet + flips the last octet bit — `m[0] \|= 0x02; m[5] ^= 0x01`. Distinct from wlan0's MAC. Spec's concern is addressed. |
| 5 | Missing regulatory domain | **INCOMPLETE** | `wpa_supplicant-openmarquee.conf:18` has `country=US` ✓ but `hostapd.conf` has NO `country_code=` line. Spec wants both. Symptom: hostapd may fail to start or beacon on a channel wlan0 chose. |
| 6 | Stale firmware | N/A | Code-repo concern only via `scripts/install.sh` packaging. Not directly addressable from the repo; document the dependency. |

### What also exists today (relevant to the plan)

- `system/openmarquee-ap0.service` + `openmarquee-ap0-setup.sh` —
  creates `ap0` virtual interface via `iw dev wlan0 interface add
  ap0 type __ap`. Matches the spec's `uap0` shape (only the name
  differs).
- `system/dnsmasq.conf` — `address=/#/10.0.0.1` wildcard +
  `interface=ap0 bind-interfaces`. Matches spec's captive-portal
  DNS intercept design. IP range is 10.0.0.0/24 vs spec's 192.168.4.0/24
  (cosmetic).
- `system/dnsmasq.service.d/openmarquee-ap0-heal.conf` — re-runs
  ap0 setup before dnsmasq if ap0's IP got flushed. Defensive,
  retain as-is.
- `backend/openmarquee/wifi_station.py` — nmcli-based STA-mode
  applier with connection-level (not device-level) ops to avoid
  cascading into ap0. Will be replaced by the supervisor in P1.
- `backend/openmarquee/settings.py:216,242,246,250` —
  `wifi_ap_enabled`, `wifi_station_enabled`, `wifi_station_ssid`,
  `wifi_station_password`. Currently `wifi_ap_enabled=true` by
  default. Spec wants `ONLINE = AP off` as steady state.
- `system/openmarquee-best-wifi.sh` (r60) — scans known SSIDs +
  picks strongest signal. Doesn't directly conflict with the spec
  but the supervisor will absorb this responsibility.
- `system/openmarquee-wifi-watchdog` — ping-default-gateway cron
  with NM restart on 2 consecutive failures. Reactive last-resort
  recovery; supervisor will replace.
- `ui/welcome.html` — first-boot screen with SSID + password + QR
  placeholder. Already designed for marquee display. Spec's "marquee
  is the onboarding UI" extends this to runtime status cards.

### Spec items NOT in code today

- Single supervisor process (the central design decision)
- Explicit channel-follow logic
- SETUP → CONNECTING → LINGER → ONLINE → DEGRADED state machine
- LINGER 2-min grace + LINGER → ONLINE AP teardown
- DEGRADED auto-bring-up of AP on STA loss
- Boot-time `iw set power_save off` service
- Proximity PIN (per-boot random)
- POST-then-associate captive portal flow (currently AP+STA both
  always on; no flow boundary)
- Marquee status surface for non-SETUP states (DEGRADED card, boot
  address card, LINGER confirmation card)
- Power-cycle pattern (3-boots = Setup, 5+-boots = factory reset)
- Crash-loop guard (separate counter from the power-cycle gesture)
- mDNS/avahi advertising `openmarquee.local` + `_http._tcp`
- Web App Manifest for "Add to Home Screen"
- Sticky DHCP client-ID
- Fallback mode flag (mutually-exclusive AP/STA — spec §"Fallback position")
- Distinct `uap0` naming (cosmetic; see §C ambiguities)
- Diagnostics ring buffer beyond default journald
- `iw event -f` capture for disconnect reason codes
- hostapd `country_code=US` + minimal `ht_capab` / 20MHz-only

---

## §B — Phased implementation plan

QA's suggested phase cut is sound. I propose adjusting two items:

- **P1 splits into P1.0 (diagnostics-first quick wins) + P1.1
  (supervisor + channel-follow).** Per the spec "Instrument before
  fixing." The diagnostics-only changes (power-save service +
  hostapd country code + channel logging in ap0 setup +
  `iw event` capture) are cheap and immediately useful even before
  the supervisor exists; they sharpen P1.1.
- **P4 has a hard dependency on the renderer's IPC surface for
  status cards.** Will need a new IPC op (or settings-trigger) for
  "render a system status card". Flag for QA to coordinate with
  code-Jimmy.

### P1.0 — Diagnostics-first quick wins (this dispatch, in parallel with plan review)

Pure-observation + low-risk hardening that doesn't depend on the
supervisor:

- `system/openmarquee-wifi-powersave-off.service` — boot-time
  + post-reassociation oneshot calling `iw dev wlan0 set power_save
  off; iw dev ap0 set power_save off`. Triggered by
  `Wants=`/`After=` ap0.service + reactivated via NM dispatcher
  hook on wlan0 reassociation events.
- Add `country_code=US` to `system/hostapd.conf` so hostapd can
  beacon on whatever channel wlan0 ends up on.
- Add channel-value logging in `openmarquee-ap0-setup.sh` and
  hostapd start: log STA channel at association event + AP channel
  at hostapd start. Spec's smoking-gun: any divergence in the logs
  IS the channel mismatch.
- Pin minimal `ht_capab` / 20MHz-only in hostapd.conf per spec
  §"hostapd.conf template" Notes — "40 MHz on 2.4 GHz buys nothing
  here and adds coexistence failures."
- Optional: `iw event -f` systemd capture service writing to a
  ring buffer at `/var/log/openmarquee/iw-events.log`, rotated.
  Logs disconnect reason codes for forensics. Spec §"Diagnostics."

**Testable on:** openMarqueeDev (`ssh openmarquee@openMarqueeDev`).
**NOT on FYS** per the hard constraint. **No supervisor needed; no
state-machine changes.** Pure low-risk additions.

### P1.1 — Supervisor skeleton + channel-follow

Per spec §"Recommended regime" + §"The channel-follow logic":

- New `backend/openmarquee/network_supervisor.py`:
  - In-process state machine living inside the FastAPI app (not a
    separate process per spec).
  - States: `SETUP`, `CONNECTING`, `LINGER`, `ONLINE`, `DEGRADED`.
  - Initial state derived from boot conditions (stored credentials
    + last-known state from `/var/lib/openmarquee/network-state.json`).
  - Listens on wpa_supplicant control socket for association
    events; falls back to polling `iw dev wlan0 link` if socket
    unavailable.
  - Channel-follow primitive: on STA association OR channel change,
    regenerate `/etc/hostapd/hostapd.conf` from a Jinja-ish template
    with the current STA channel + restart hostapd via dbus
    (`systemctl restart hostapd.service`).
- `system/NetworkManager-openmarquee-unmanaged.conf` extended to
  also unmanage `wlan0`. This is the breaking change — NM will no
  longer manage wlan0; the supervisor takes over via
  wpa_supplicant directly.
- `system/wpa_supplicant@wlan0.service` enabled (replaces NM's
  internal wpa_supplicant integration). Reads
  `/etc/wpa_supplicant/wpa_supplicant-wlan0.conf` which the
  supervisor writes from settings.
- Diagnostics ring buffer (5-min sliding window) of:
  - wpa_supplicant control events
  - dmesg `brcmfmac` lines
  - hostapd start/stop times + channels
  - State-machine transitions with timestamps
- **Migration plan for `wifi_station.py`:** the supervisor REPLACES
  the nmcli-based applier. `wifi_station.py` shrinks to a thin
  compatibility shim that proxies into the supervisor's `apply()`
  method. Settings API unchanged from operator's POV.
- **Migration plan for `best-wifi.sh`:** the supervisor absorbs
  scan-and-pick-best. The shell script is retired (kept as
  `system/openmarquee-best-wifi.sh.deprecated` for one release
  cycle).
- **Migration plan for `wifi-watchdog.sh`:** the supervisor
  absorbs ping-default-gateway + auto-recovery into the
  DEGRADED state's recovery path. Cron entry retired.

**Testable on:** openMarqueeDev. **Includes the spec's fallback
mode flag from day one** (config: `network_fallback_mutex_mode:
bool = False`; when true, supervisor treats AP and STA as
mutually exclusive — comitup pattern).

### P2 — Captive portal plumbing

Per spec §"Captive portal plumbing":

- POST-then-associate flow: portal form POSTs to
  `/api/onboarding/submit-credentials`; the API immediately returns
  a "connecting — this network may blip for a few seconds" page that
  polls `/api/onboarding/status` every 1s. The supervisor handles
  the actual STA association in the background; status endpoint
  returns the current state-machine state.
- Captive-portal probe responders: backend handlers for
  `captive.apple.com/hotspot-detect.html` + Android's
  `connectivitycheck.gstatic.com/generate_204` redirect to portal
  (currently they hit dnsmasq's wildcard but the FastAPI app needs
  to answer with 302→ portal, not 204).
- dnsmasq wildcard: already in place (verified §A).

### P3 — State machine product behavior

Per spec §"Onboarding state machine":

- LINGER 2-min grace timer
- LINGER → ONLINE transition tears down hostapd + AP-side dnsmasq
  bindings (interface stays up but unannounced; the marquee status
  card surface uses the same path to bring it back).
- DEGRADED entry: AP comes back up, retry STA with backoff.
- Auto-off timer for AP after manual re-entry (30 min default).
- Re-enter Setup Mode from the web UI (POST
  `/api/onboarding/setup-mode-on`).

### P4 — Marquee as status surface

Per spec §"The display is the onboarding UI":

- IPC surface needed from the renderer for system status cards.
  **Coordinate with code-Jimmy.** Three plausible shapes:
  - **(a) New IPC op** `RenderSystemCard { kind: SetupQR | Boot | LingerConfirm | Degraded, payload: {...} }`. Renderer overlays/replaces the active playlist briefly.
  - **(b) Reserved-playlist slot.** The settings-driven playlist
    list gets a reserved-id slot that the supervisor populates
    with a transient SystemSlide. Renderer treats it as a regular
    slide.
  - **(c) Settings-driven.** Supervisor writes a `system_card_active`
    object to settings; renderer polls + overlays.
  - **Recommendation:** (a). It's lowest-latency and the spec asks
    for boot card timing measured in seconds, not playlist-cycle
    latency.
- QR-code generator (existing `welcome.html` JS) extracted to
  backend so the marquee can render at boot before the UI is even
  served.
- Proximity PIN: per-boot random in
  `/var/lib/openmarquee/network-state.json`; supervisor passes to
  hostapd template's `wpa_passphrase=` field.

### P5 — Power-cycle pattern + crash-loop guard

Per spec §"No-button hardware":

- `system/openmarquee-boot-counter.service` — at boot, increment
  `/var/lib/openmarquee/boot-counter.json` AFTER display
  initialization succeeds (guard against crash-loop wiping its own
  config). Background timer resets to 0 after 60s of uptime.
- Supervisor reads the counter at startup:
  - 3 boots → enter SETUP without discarding credentials.
  - 5+ boots → factory reset (discard credentials, return to
    virgin SETUP).
- Crash-loop counter SEPARATE from the user-gesture counter,
  separate behavior (enter safe mode, no config wipe).
- Marquee boot-card text: "Restart 2× more for Setup Mode" when
  counter ≥ 1.

### P6 — mDNS/avahi + manifest + sticky DHCP

Per spec §"Finding the device after onboarding":

- `avahi-daemon` advertising `openmarquee.local` + `_http._tcp` service record.
- Auto-suffix `openmarquee-2.local` if name collision detected at boot.
- Web App Manifest for "Add to Home Screen" on the LINGER redirect landing.
- Stable DHCP client-ID (derived from device serial) so router lease stays sticky.

---

## §C — Ambiguities and conflicts (flag for qarl/QA)

These are spec items where the implementation choice is non-obvious
or conflicts with existing architecture. **Surfacing per dispatch's
"flag, don't guess" rule.**

### C.1 — Interface naming: `ap0` vs `uap0`

The spec uses `uap0` throughout. Existing code uses `ap0` across
12+ files (hostapd, dnsmasq, ap0-setup.sh, NM unmanaged conf,
sudoers, install.sh, backend wifi_ap_enabled docs, etc.). Both are
valid choices for an `iw dev ... interface add ... type __ap`
virtual interface; the `u` prefix conventionally signals
"userspace AP."

**Recommendation:** keep `ap0`. Renaming would touch every file in
the network stack + would break in-place upgrades on FYS-class
deployments (NM unmanaged conf keyed on interface-name).

**Plan should explicitly note** this difference so a future reader
of the spec doesn't think the implementation is non-compliant.

### C.2 — Full NM displacement vs hybrid

The spec is explicit: take the radio away from NM (BOTH
interfaces). The current code is a hybrid: NM owns wlan0 STA, the
ap0 unit owns the AP. The hybrid is the comitup-adjacent path; the
spec wants the full-supervisor path.

**Cost of full displacement:**
- `backend/openmarquee/wifi_station.py` (~550 LOC of nmcli logic)
  becomes a thin shim
- `backend/openmarquee/wifi_prefill.py` (queries nmcli for
  known-SSID list) needs an equivalent against wpa_supplicant
- In-place upgrade migration: existing NM connection profiles at
  `/etc/NetworkManager/system-connections/*` need to be enumerated
  + converted to `wpa_supplicant-wlan0.conf` blocks
- Test-bed risk: nmcli quirks are well-understood by the team;
  wpa_supplicant control-socket events are new ground

**Reading of the spec:** full displacement is what's specified.
The hybrid is acknowledged in `wifi_station.py:24-36` as a
deliberate scoping choice from task #99 that was always pending
this resolution.

**Recommendation:** plan ships full displacement in P1.1 as spec'd.
The fallback mode flag (§"Fallback position") provides a safety
net.

**Flag:** qarl/QA may want to push back if the displacement risk on
FYS is uncomfortable. The alternative (hybrid + supervisor-drives-
nmcli) is implementable but doesn't address the "NM contention"
likely-contributor in §A.

### C.3 — `ONLINE = AP off` as default

Spec says ONLINE is the default steady state and ONLINE means AP
off. Current defaults: `wifi_ap_enabled=true` in `settings.py:216`.
FYS currently runs AP 24/7.

**Migration:** existing devices need to keep AP enabled across the
upgrade, then auto-transition to ONLINE when the supervisor reaches
LINGER → ONLINE for the first time. The migration logic:

```
on supervisor startup:
    if settings.wifi_ap_enabled == true and settings.wifi_station_enabled == true:
        # Existing pre-rework device. Honor wifi_ap_enabled until
        # supervisor's own LINGER → ONLINE transition fires, at which
        # point flip wifi_ap_enabled = false (one-time migration).
        log: "applying ONLINE=AP-off migration on first successful association"
```

**Flag:** this is a behavior change. The spec is opinionated about
it. I'll implement it but flag for review on the migration log
line.

### C.4 — Marquee status surface IPC: which shape?

P4 needs a renderer IPC op for system status cards. Three shapes
proposed above (a/b/c). **Recommendation (a)** — lowest latency.
**Flag for code-Jimmy coordination via QA.**

### C.5 — dnsmasq IP range

Spec uses `192.168.4.0/24`; code uses `10.0.0.0/24`. Cosmetic. Keep
existing `10.0.0.0/24` (in-place upgrade safety; clients holding
old leases).

### C.6 — Best-wifi.sh fate

The r60 best-wifi.sh implements scan-and-pick-best across known
SSIDs (qarl-direct r60 spec). Spec doesn't address this. The
supervisor will absorb the functionality. **Flag:** the supervisor
absorbing this means we lose the r60 dispatch's separate
"safety contract" (in-band ssh safety + hysteresis). Plan must
re-implement these inside the supervisor's STA-selection logic.

---

## §D — P1.0 deliverables (this commit)

In parallel with QA reviewing the plan above:

1. `docs/onboarding-rework-plan.md` — this file.
2. `system/hostapd.conf` — add `country_code=US`, pin minimal
   `ht_capab` / 20MHz-only.
3. `system/openmarquee-ap0-setup.sh` — log STA channel at start
   (best-effort, `iw dev wlan0 link` may report nothing on first
   boot pre-association — handle gracefully).
4. `system/openmarquee-wifi-powersave-off.service` + `.sh` —
   boot-time + post-reassociation oneshot that calls
   `iw dev <iface> set power_save off` on wlan0 + ap0.
5. `install.sh` — install the new powersave-off service.
6. Unit tests for the new shell wrapper logic (channel-format
   parsing) in the existing pytest harness.

**Out of scope for this dispatch (deferred to subsequent P1.0 commits if
QA agrees):**
- iw event ring buffer (sketched in P1.0; lower priority than
  channel logging)
- supervisor skeleton (P1.1)

---

## §E — Hard constraints inherited from dispatch

- **Dev target = openMarqueeDev.** No FYS network-config changes
  through any phase. P4 marquee status surface is the only phase
  that interacts with FYS infrastructure (renderer IPC), and even
  that is just the surface; FYS still runs the supervisor-less
  hybrid until QA flips deploy ordering.
- **Fallback mode flag from day one.** `network_fallback_mutex_mode:
  bool = False` in settings, exposed through the supervisor's
  state machine as a runtime switch.
- **Sacred subagent review before every commit.**

---

## §F — Open question for review

The plan above commits to FULL NetworkManager displacement in P1.1
(§C.2). If qarl wants the hybrid path instead (supervisor drives
nmcli, NM stays in charge of wlan0), the plan reshapes
considerably:

- §A contributor #3 (NM contention) becomes UNADDRESSED
- The supervisor's wpa_supplicant socket integration becomes nmcli
  dbus integration
- P1.1 scope shrinks by ~30%
- Fallback mode flag becomes less load-bearing (NM-driven STA is
  already the comitup-pattern fallback shape)

**Awaiting QA/qarl direction on §C.2 before starting P1.1.** P1.0
deliverables in §D are safe under either path.
