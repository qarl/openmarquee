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

### C.1 — Interface naming: `ap0` vs `uap0` [RESOLVED 2026-06-10]

The spec uses `uap0` throughout. Existing code uses `ap0` across
12+ files (hostapd, dnsmasq, ap0-setup.sh, NM unmanaged conf,
sudoers, install.sh, backend wifi_ap_enabled docs, etc.). Both are
valid choices for an `iw dev ... interface add ... type __ap`
virtual interface; the `u` prefix conventionally signals
"userspace AP."

**Recommendation:** keep `ap0`. Renaming would touch every file in
the network stack + would break in-place upgrades on FYS-class
deployments (NM unmanaged conf keyed on interface-name).

**Resolution (QA 2026-06-10):** KEEP `ap0`. **Spec-divergence
note:** the canonical spec at
`~/project/openmarquee/qa/spec-onboarding-ap-sta-concurrent-2026-06-10.md`
uses `uap0`; the implementation uses `ap0`. Reading the spec
literally, substitute `ap0` everywhere `uap0` appears. This is
cosmetic — no functional difference.

### C.2 — Full NM displacement vs hybrid [RESOLVED 2026-06-10]

**Resolution (QA 2026-06-10):** FULL NM displacement, per the spec.
De-risk via:
1. Keep the shim approach for `wifi_station.py` (compatibility
   layer over the new supervisor).
2. Land behind the fallback mode flag (`network_fallback_mutex_mode`)
   so a regression can flip back to NM-managed wlan0 without
   reinstall.
3. Sequence the nmcli-profile migration (convert existing
   `/etc/NetworkManager/system-connections/*` to
   `wpa_supplicant-wlan0.conf` blocks) as its own commit with its
   own tests.

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

### C.3 — `ONLINE = AP off` as default [RESOLVED 2026-06-10]

**Resolution (QA 2026-06-10):** the plan's migration approach is
correct. **One addition:** when the supervisor lands, the
`settings.wifi_ap_enabled` field's semantics CHANGE from "AP runs
24/7" to "Setup Mode allowed." The settings UI copy must be updated
to match the spec's naming convention: rename the field's UI label
to "Setup Mode" everywhere. The wire protocol field name stays
`wifi_ap_enabled` for API compatibility; only the UI copy + the
runtime semantics change. Sequence the UI rename as its own commit
during P3.


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

### C.4 — Marquee status surface IPC: which shape? [DEFERRED 2026-06-10]

**Resolution (QA 2026-06-10):** new IPC op (shape (a)) confirmed.
Parked until P4; QA will coordinate the renderer side with
code-Jimmy when supervisor work reaches P4.


P4 needs a renderer IPC op for system status cards. Three shapes
proposed above (a/b/c). **Recommendation (a)** — lowest latency.
**Flag for code-Jimmy coordination via QA.**

### C.5 — dnsmasq IP range [RESOLVED 2026-06-10]

Spec uses `192.168.4.0/24`; code uses `10.0.0.0/24`. Cosmetic. Keep
existing `10.0.0.0/24` (in-place upgrade safety; clients holding
old leases).

**Resolution (QA 2026-06-10):** KEEP `10.0.0.0/24`. **Spec-divergence
note:** reading the spec literally, substitute `10.0.0.x` everywhere
`192.168.4.x` appears. Captive-portal IP is `10.0.0.1`.

### C.6 — Best-wifi.sh fate [RESOLVED 2026-06-10]

The r60 best-wifi.sh implements scan-and-pick-best across known
SSIDs (qarl-direct r60 spec). Spec doesn't address this. The
supervisor will absorb the functionality. **Flag:** the supervisor
absorbing this means we lose the r60 dispatch's separate
"safety contract" (in-band ssh safety + hysteresis). Plan must
re-implement these inside the supervisor's STA-selection logic.

**Resolution (QA 2026-06-10):** supervisor absorbs +
re-implements the r60 safety contracts. **These contracts protect
QA's remote-access SSH during dev — treat as P1.1 acceptance
criteria, not polish.** Specifically:
- In-band SSH guard: before changing wlan0 connections, verify
  the current SSH session is reachable via a path that won't be
  broken by the switch (Tailscale tailnet path counts; same-wlan
  path doesn't).
- Hysteresis: don't switch unless candidate is >= 8 percentage
  points stronger than current (NM's SIGNAL units, NOT raw dBm —
  see r60 dispatch's BLOCKER fix).

---

## §D.6 — P1.2-B.3 deliverables (2026-06-10 — 3 verify-bounce fixes)

Shipped after QA's third dev-Pi verify (the first to actually
WORK end-to-end — the daemon executed `nm-write-unmanaged-wlan0`
as root with the 410-byte NM-drop-in payload, and the rollback
path again ran clean with no connectivity loss). Three findings:

### 1. `/run/openmarquee/` was unreachable to the backend user

QA observed: `/run/openmarquee/` was created `root:root 0750` by
systemd (the .socket unit's `DirectoryMode=0750` sets mode but
the OWNER is whatever process creates the parent dir — PID 1 =
root). With group=root and mode 0750, the openmarquee backend
user had no path to traverse INTO the directory to reach the
socket file.

**Fix:** `system/openmarquee-tmpfiles.conf` (NEW) creates
`/run/openmarquee/` as `root:openmarquee 0750` via systemd-
tmpfiles. install.sh stages to `/etc/tmpfiles.d/openmarquee.conf`
and runs `systemd-tmpfiles --create` immediately so the next
`systemctl start openmarquee-netctl.socket` finds the dir with
correct ownership without a reboot.

The socket file inside (`/run/openmarquee/netctl.sock`) keeps
`SocketUser=root SocketGroup=openmarquee SocketMode=0660`. The
backend (member of group openmarquee) now traverses into the
dir (group-x via 0750's group bit) AND reads/writes the socket
(group-rw via 0660's group bits). Non-root non-openmarquee
processes still can't reach either.

### 2. 15s socket read timeout too short for slow systemd ops

QA observed: the take-over flip died at `wpa-enable-wlan0` with
"response read timed out after 15s". `systemctl enable --now
wpa_supplicant@wlan0.service` under boot load takes 20-30s
including unit-state transitions; the 15s cap killed it mid-op.
The daemon-side `BrokenPipe` confirmed the client hung up
mid-op.

**Fix:** per-op timeouts. `network_supervisor_takeover.py`
splits the netctl timeout into FAST (15s) and SLOW (60s) buckets:

| Op | Bucket | Why |
|---|---|---|
| `nm-write-unmanaged-wlan0` | FAST 15s | atomic file write |
| `nm-reload` | FAST 15s | dbus signal |
| `nm-supplicant-stop` | SLOW 60s | systemctl stop on a busy service |
| `wpa-write-wlan0-conf` | FAST 15s | atomic file write |
| `wpa-enable-wlan0` | SLOW 60s | systemctl enable --now + unit start + initial association |
| `wpa-stop-wlan0` | SLOW 60s | systemctl stop on a failing-to-associate unit |
| `nm-supplicant-start` | SLOW 60s | systemctl start + dbus reconnect |

The daemon-side `HELPER_TIMEOUT_S` also bumped 30 → 90 so the
backend's 60s cap has headroom + the underlying `subprocess.run`
on the helper doesn't itself time out first.

### 3. NM's wpa_supplicant singleton owned wlan0 after unmanage

The architectural gap QA spotted: `nm-reload` un-manages wlan0
from NetworkManager's perspective, but NM's `wpa_supplicant.service`
singleton still HOLDS the nl80211 ifname. When we then enable
`wpa_supplicant@wlan0.service`, it fails status=255 "You may
have another wpa_supplicant process already running."

**Fix:** add `nm-supplicant-stop` between `nm-reload` and
`wpa-write-wlan0-conf` in the flip sequence, and add
`nm-supplicant-start` between `wpa-stop-wlan0` and `nm-reload` in
rollback. On this device NM's supplicant only serves wlan0 (ap0
is hostapd-driven), so stopping it is safe.

Flip sequence (5 steps, was 4):
```
1. nm-write-unmanaged-wlan0   (NEW unmanage conf)
2. nm-reload                  (NM picks up the conf)
3. nm-supplicant-stop         (P1.2-B.3: release nl80211 ifname)
4. wpa-write-wlan0-conf       (our config)
5. wpa-enable-wlan0           (our wpa_supplicant@wlan0)
```

Rollback sequence (4 steps, was 3):
```
1. nm-remove-unmanaged-wlan0   (drop unmanage conf)
2. wpa-stop-wlan0              (free our wpa_supplicant@wlan0)
3. nm-supplicant-start         (P1.2-B.3: NM gets its supplicant back)
4. nm-reload                   (NM resumes managing wlan0)
```

QA noted: "the supervisor observe-loop ALSO holds a socket to
NM's supplicant ctrl — make sure the loop re-attaches to OUR
supplicant post-flip." The existing observe-loop in
`network_supervisor_loop.py` ALREADY handles reconnect on
OSError via close+next-tick-reconnect with a 30s cadence; after
NM's supplicant stops + ours starts at the same `/var/run/wpa_supplicant/wlan0`
ctrl path, the loop will reconnect within 30s. Documented here
+ deferred to P1.3 if QA wants tighter re-attach signaling.

### Files

| File | Change |
|---|---|
| `system/openmarquee-tmpfiles.conf` | NEW — creates `/run/openmarquee/` root:openmarquee 0750 |
| `system/openmarquee-netctl` | + 2 subcommands: `nm-supplicant-stop`, `nm-supplicant-start` |
| `system/openmarquee-netctl-daemon` | + 2 ALLOWLIST entries; bumped HELPER_TIMEOUT_S 30 → 90 |
| `scripts/install.sh` | + tmpfiles stage + `systemd-tmpfiles --create` |
| `backend/openmarquee/network_supervisor_takeover.py` | + `NETCTL_FAST_TIMEOUT_S` / `NETCTL_SLOW_TIMEOUT_S` constants; attempt + rollback sequence updated; per-op timeouts on slow ops |
| `backend/tests/test_network_supervisor_takeover.py` | sequence assertions updated; timeout assertions added; `netctl_recorder` fixture now records `timeout_s` |
| `docs/onboarding-rework-plan.md` | this §D.6 |

### Parallel-arc coordination note

QA flagged that my P1.2-B.x backend pushes mid-renderer-arc
caused code-Jimmy to lose edits twice (worktree file-state
sync). My discipline going forward:
- `git fetch origin && git pull --ff-only` BEFORE every commit
- Stage explicitly with file paths (never `git add .` or `git add -A`)
- `git diff --cached --stat` check before commit — only my lane's files
- Restore renderer/ if anything leaks in (P1.2-B.2's sacred review
  caught r109 in my worktree from the shared `/tmp/openmarquee-main` clone)

P1.2-B.3 was rebased onto main 57cb65f (code-Jimmy's r109) before
this commit.

## §D.5 — P1.2-B.2 deliverables (2026-06-10 — socket-activated daemon, sudo retired)

Shipped after QA's verify of P1.2-B.1 caught a second
production-reality bug. P1.2-B.1's "sudo works under NNP" bet
was wrong: `sudo: The "no new privileges" flag is set, which
prevents sudo from running as root`. Plus the state-file path
`/var/lib/openmarquee/network-state.json` was outside the
backend's `ReadWritePaths=/var/openmarquee` sandbox so every
`save_persisted_state` raised "Read-only file system" and the
rollback cooldown record was lost.

QA's preferred Option B (over polkit + dbus): socket-activated
root companion. Backend connects to `/run/openmarquee/netctl.sock`;
systemd template service spawns a fresh root daemon per
connection with STDIN/STDOUT bound to the socket. No privilege
escalation in the backend process — the daemon is already root.
NNP is irrelevant.

### What this commit ships (10 files)

1. **`system/openmarquee-netctl-daemon` (NEW Python ~150 LOC)** —
   per-connection IPC adapter. Reads subcommand line from STDIN
   (cap 256 bytes), validates against the 7-subcommand allowlist,
   reads payload bytes (cap 64KB belt-and-braces), invokes the
   existing bash helper at `/usr/local/sbin/openmarquee-netctl
   <subcommand>` with the payload piped to stdin, writes
   `OK\n` or `ERR <message>\n` to STDOUT. Diagnostics to the
   systemd journal tagged `openmarquee-netctl-daemon`.

2. **`system/openmarquee-netctl.socket` (NEW)** — listens on
   `/run/openmarquee/netctl.sock` with `Accept=yes`,
   `SocketUser=root` + `SocketGroup=openmarquee` + `SocketMode=0660`.
   Only the backend user (member of group `openmarquee`) can
   connect.

3. **`system/openmarquee-netctl@.service` (NEW)** — template
   service. `User=root`, `StandardInput=socket`,
   `StandardOutput=socket`, `StandardError=journal`. Per-
   connection one-shot.

4. **`scripts/install.sh` (MODIFIED)** — installs the daemon at
   `/usr/local/sbin/openmarquee-netctl-daemon` mode 0755 root:root,
   stages the socket + template unit into `/etc/systemd/system/`,
   `systemctl enable + start openmarquee-netctl.socket`. The bash
   helper from P1.2-B.1 is still installed (the daemon delegates
   to it).

5. **`system/openmarquee-sudoers` (MODIFIED)** — REMOVED the 7
   `openmarquee-netctl <subcmd>` grants. Sudo is dead under NNP;
   they were unreachable. Kept the 2 nmcli grants (production
   `wifi_station.py` still has them) with a note that those
   likely fail silently too — auditing is P1.2-C work.

6. **`backend/openmarquee/network_supervisor.py` (MODIFIED)** —
   `DEFAULT_STATE_FILE` moved from `/var/lib/openmarquee/network-state.json`
   to `/var/openmarquee/network-state.json` (inside the sandbox's
   `ReadWritePaths=`). Consistent with playlist / settings / auth
   which all live in `/var/openmarquee/`.

7. **`backend/openmarquee/network_supervisor_takeover.py` (MODIFIED)** —
   `_run_netctl` replaced sudo+subprocess with
   `asyncio.open_unix_connection(NETCTL_SOCKET_PATH)`. Same
   contract (subcommand allowlist on server side; payload via
   stdin); only the transport changed. Constants:
   `NETCTL_SOCKET_PATH = "/run/openmarquee/netctl.sock"`.

8. **`backend/openmarquee/network_supervisor_actuator.py`
   (MODIFIED)** — `_run_netctl_hostapd_write_and_restart` replaced
   sudo+subprocess with sync `socket.AF_UNIX + SOCK_STREAM`
   connect + sendall + shutdown(SHUT_WR) + recv loop. Cap on
   response bytes (8KB) prevents a chatty daemon blowing memory.
   Post-verify via `iw dev ap0 info` unchanged (read-only).

9. **`backend/tests/test_network_supervisor_actuator.py` (MODIFIED)** —
   tests monkeypatch `_run_netctl_hostapd_write_and_restart`
   directly (records payload list) instead of mocking `subprocess.run`
   for sudo. Same coverage: happy path + each failure shape +
   post-verify mismatch (the load-bearing P1.2-A.1 NIT) +
   missing iw + timeout + payload-contract test pinning
   `channel=11` in the netctl payload.

10. **`docs/onboarding-rework-plan.md` (MODIFIED)** — this §D.5
    section.

### Privilege transport diff vs P1.2-B.1

| Concern | P1.2-B.1 | P1.2-B.2 |
|---|---|---|
| Privilege boundary | `sudo` -> bash helper | Unix socket -> systemd template -> bash helper |
| Works under NoNewPrivileges? | **NO** (verified by QA) | **YES** (no privilege transition in backend) |
| argv-validation gates | Helper `$#` + sudoers no-trailing-args | Daemon allowlist + helper `$#` |
| File content carrier | STDIN to helper via sudo | STDIN to daemon via socket -> STDIN to helper |
| Payload size cap | None (sudo doesn't care) | 64KB enforced in daemon |
| Connect AuthN | sudo's PAM stack | `SocketGroup=openmarquee SocketMode=0660` |
| Spawn cost per call | sudo + bash | systemd-spawn + python + bash |
| Failure shape on backend | `RuntimeError(rc=N stderr)` | `RuntimeError(ERR <msg>)` |

### Audit: what's still in-process (no privilege needed)

| Operation | Why it doesn't need netctl |
|---|---|
| Read `/etc/hostapd/hostapd.conf` | `ProtectSystem=strict` allows reads |
| `iw dev ap0 info` (post-verify) | Read-only netlink query; no CAP_NET_ADMIN |
| `iw dev wlan0 info` (STA freq poll) | Same |
| `tailscale status --json` (pre-flight) | Read-only |
| Read+write `/var/openmarquee/network-state.json` | Inside `ReadWritePaths=` |
| Wpa_supplicant ctrl socket events | Read via SOCK_DGRAM client |

### Sandbox-path move audit

| State file | Old path | New path |
|---|---|---|
| `network-state.json` | `/var/lib/openmarquee/` ❌ | `/var/openmarquee/` ✓ |
| `playlist.json` | `/var/openmarquee/` ✓ | unchanged |
| `settings.json` | `/var/openmarquee/` ✓ | unchanged |
| `auth.json` (?) | `/var/openmarquee/` ✓ | unchanged |

### Verify procedure for QA's third attempt

Same recipe as P1.2-B and P1.2-B.1 (no env-var changes; the
systemd drop-in for `OPENMARQUEE_NETWORK_TAKEOVER_ENABLED=1` you
already set is still in place). The first deploy-side change to
watch for:

```
sudo systemctl status openmarquee-netctl.socket    # listening
sudo systemctl list-sockets | grep openmarquee     # bound at /run/openmarquee/netctl.sock
ls -la /run/openmarquee/netctl.sock                 # owner root:openmarquee, mode srw-rw----
```

Then the take-over flip path:

```
sudo journalctl -u openmarquee-backend | grep network-takeover
sudo journalctl -t openmarquee-netctl-daemon       # per-connection daemon logs
nmcli device status                                 # wlan0 -> unmanaged
systemctl status wpa_supplicant@wlan0.service      # active
```

Wifi bounce, rollback drill, fallback_mutex — all the same as
P1.2-B's verify procedure.

## §D.4 — P1.2-B.1 deliverables (2026-06-10 — privilege boundary)

Shipped after QA's live verify caught the production-reality bug
QA's sacred review and my unit tests both missed: the backend
runs as user `openmarquee` under `NoNewPrivileges=true` +
`ProtectSystem=strict` (see `system/openmarquee-backend.service`),
so direct file writes to `/etc/` and `systemctl` invocations fail
with EROFS / permission errors. The rollback path worked perfectly
on the first failed flip (no connectivity loss, observe-only
preserved, watchdog disarmed clean) — the architecture is sound;
just needed the privilege boundary.

### What this commit ships (10 files)

1. **`system/openmarquee-netctl` (NEW ~100 LOC)** — the privilege-
   boundary helper. Bash script with strict argv contract:
   ```
   openmarquee-netctl <command>
       nm-write-unmanaged-wlan0       # STDIN = NM drop-in content
       nm-remove-unmanaged-wlan0
       nm-reload
       wpa-write-wlan0-conf           # STDIN = wpa config (mode 0600)
       wpa-enable-wlan0
       wpa-stop-wlan0
       hostapd-write-and-restart      # STDIN = hostapd config
   ```
   File content for write-* subcommands flows through STDIN,
   never argv (the sudoers grant blocks trailing args). Each
   write uses atomic tempfile + `mv -f` in the same directory.

2. **`system/openmarquee-sudoers` (MODIFIED)** — adds 7 NOPASSWD
   grants, one per netctl subcommand. **No wildcards** — the
   subcommand allowlist is the security boundary. Adding a new
   privileged operation requires (a) new subcommand in the
   helper, (b) new grant here.

3. **`scripts/install.sh` (MODIFIED)** — installs the helper to
   `/usr/local/sbin/openmarquee-netctl` with mode 0755 owner
   root:root. Runs BEFORE the sudoers stage so `visudo -c`
   validation has a valid target binary path to anchor against.

4. **`backend/openmarquee/network_supervisor_takeover.py`
   (MODIFIED)** — replaced direct `paths.nm_unmanage_path.write_text(...)`
   and `_systemctl(["reload", ...])` calls with `_run_netctl(
   subcommand, stdin_input=...)`. Same orchestrator semantics
   (arm watchdog FIRST, mark_associated hook, immediate-rollback
   on exception), just routed through sudo to netctl. Replaced
   `_systemctl()` helper with `_run_netctl()`.

5. **`backend/openmarquee/network_supervisor_actuator.py`
   (MODIFIED)** — replaced the tempfile + os.replace + systemctl
   restart pattern with a single `_run_netctl_hostapd_write_and_restart`
   call. The new conf text is piped via STDIN. Atomic-write
   responsibility moves into the helper (`install_from_stdin`
   shell function). Post-verify via `iw dev ap0 info` unchanged
   (read-only, no privilege escalation needed).

6. **`backend/tests/test_network_supervisor_actuator.py`
   (MODIFIED)** — tests now mock `sudo` invocations + assert the
   STDIN payload content. Added `test_actuator_pipes_new_conf_through_netctl_stdin`
   pinning the exact argv shape: `["sudo", "-n", "/usr/local/sbin/openmarquee-netctl",
   "hostapd-write-and-restart"]`. Defense against argv-injection
   regression.

7. **`backend/tests/test_network_supervisor_takeover.py`
   (MODIFIED)** — renamed `systemctl_recorder` fixture to
   `netctl_recorder` returning `[(subcommand, stdin_input), ...]`
   tuples. Tests pin the exact subcommand sequence + the STDIN
   payload for the write-* steps.

8. **`docs/onboarding-rework-plan.md` (MODIFIED)** — this §D.4
   section documents the privilege-boundary design + audit.

### Privilege audit (the "every subprocess/file-write" review QA asked for)

| Operation                                          | P1.2-B path                           | P1.2-B.1 path                            |
| -------------------------------------------------- | ------------------------------------- | ---------------------------------------- |
| Write /etc/NetworkManager/conf.d/...-wlan0.conf    | `path.write_text` ❌                  | `netctl nm-write-unmanaged-wlan0` ✓       |
| Delete the NM drop-in                              | `path.unlink` ❌                      | `netctl nm-remove-unmanaged-wlan0` ✓      |
| systemctl reload NetworkManager                    | `subprocess(systemctl ...)` ❌        | `netctl nm-reload` ✓                      |
| Write /etc/wpa_supplicant/wpa_supplicant-wlan0.conf| `path.write_text` ❌                  | `netctl wpa-write-wlan0-conf` ✓           |
| systemctl enable --now wpa_supplicant@wlan0        | `subprocess(systemctl ...)` ❌        | `netctl wpa-enable-wlan0` ✓               |
| systemctl stop wpa_supplicant@wlan0                | `subprocess(systemctl ...)` ❌        | `netctl wpa-stop-wlan0` ✓                 |
| Write /etc/hostapd/hostapd.conf                    | `tempfile + os.replace` ❌            | `netctl hostapd-write-and-restart` ✓     |
| systemctl restart hostapd                          | `subprocess(systemctl ...)` ❌        | (combined with write above) ✓             |
| `iw dev ap0 info` (post-verify)                    | `subprocess(iw ...)` ✓                | unchanged — read-only doesn't need root  |
| `iw dev wlan0 info` (STA freq poll in observe loop)| `subprocess(iw ...)` ✓                | unchanged                                |
| `tailscale status --json` (pre-flight)             | `subprocess(tailscale ...)` ✓         | unchanged                                |
| Read `/etc/hostapd/hostapd.conf` (actuator)        | `path.read_text` ✓                    | unchanged — `ProtectSystem=strict` allows reads |
| Read/write `/var/openmarquee/network-state.json`   | `path.write_text` ✓                   | unchanged — already in `ReadWritePaths=` |

**Privilege boundary contract:** the helper validates `$# -eq 1`
on every subcommand so the sudoers `... <subcommand>` form can't
be tricked into accepting extra args. File content arrives via
STDIN (not argv) to prevent injection of arbitrary file bytes.
Pinned paths in the helper (no `$1`-based file paths) prevent
write-arbitrary-file attacks. The `install_from_stdin` helper
chowns to root:root + mv's atomically.

### NoNewPrivileges + sudo compatibility

`NoNewPrivileges=true` blocks setuid escalation, which is normally
how sudo elevates. Production evidence (wifi_station.py's
nmcli-via-sudo calls have worked since shipping) confirms sudo
DOES function on Debian Bookworm under NNP — likely via the
nss-systemd / pam_systemd pipeline that doesn't require setuid.
Verified by reference: the existing sudoers grant for nmcli is
production-tested.

If P1.2-B.1's QA verify on the dev Pi shows sudo failing under
NNP after all, the fallback is option (B) from QA's dispatch:
template oneshot systemd units invoked via `systemctl start
openmarquee-net-takeover.service`, gated by a polkit rule
granting the openmarquee user the `org.freedesktop.systemd1.manage-units`
right scoped to `openmarquee-net-*`. Flag this as a known
fallback path; don't implement until needed.

## §D.3 — P1.2-B deliverables (2026-06-10 — NM take-over + active actuator)

Shipped after QA's 42-min P1.2-A.1 soak passed (259 decision lines,
253× already_on_target + 2× follow_sta initial, ZERO warns, ZERO
actuation subprocesses). Per the locked sequence in QA dispatch
2026-06-10.

### Modules shipped

1. **`backend/openmarquee/network_supervisor_actuator.py` (NEW, ~210 LOC):**
   - `HostapdChannelActuator(hostapd_conf_path, ap_iface)` — active
     actuator that:
     1. Reads `/etc/hostapd/hostapd.conf`
     2. Substitutes `channel=<N>` with the decision's target
     3. Atomic-writes via tempfile + `os.replace`
     4. `systemctl restart hostapd.service` (blocking, 15s timeout)
     5. **POST-VERIFY** via `iw dev ap0 info` — confirms hostapd is
        actually beaconing on the target channel. Raises
        `HostapdActuationError` if not.
   - Sync (uses `subprocess.run`) because `apply_sta_freq` is sync.
     Fires only on STA frequency change — boot association + rare
     router CSA. ~5s of blocking per change is acceptable.
   - Pure helpers `_substitute_channel` + `_parse_iw_dev_info_channel`
     for unit testability.

2. **`backend/openmarquee/network_supervisor_takeover.py` (NEW, ~450 LOC):**
   - `parse_tailscale_status_json(raw)` — pure parser for
     `tailscale status --json`. Returns `TailscalePreflightResult`
     with structured failure reasons.
   - `tailscale_preflight_check()` — async subprocess wrapper.
     On Mac dev / missing binary / non-zero exit: `ok=False`.
   - `RollbackWatchdog(timeout_s, rollback_cb)` — arm-before-flip
     self-healing timer. `arm()` is idempotent; `disarm()` cancels
     cleanly; on timeout `rollback_cb` is awaited then `fired=True`.
   - `render_nm_unmanage_drop_in(iface="wlan0")` — pure renderer.
   - `render_wpa_supplicant_conf(ssid, psk, country="US")` — pure
     renderer.
   - `TakeoverPreconditions` dataclass + `evaluate_preconditions(...)`
     — 6 gates: env flag, NM dir exists, credentials present,
     Tailscale ok, not-already-active, rollback-cooldown-clear.
   - `TakeoverOrchestrator(supervisor, paths)` — composes the flip:
     1. ARM watchdog FIRST (per QA dispatch)
     2. Write NM unmanage drop-in for wlan0
     3. Reload NM
     4. Render `wpa_supplicant-wlan0.conf` with operator credentials
     5. Enable + start `wpa_supplicant@wlan0.service`
     6. Caller (observe loop) calls `mark_associated()` on
        CTRL-EVENT-CONNECTED → disarms watchdog + persists
        `takeover_active=True`
     7. On watchdog timeout (3 min default): rollback runs
        automatically — deletes drop-in, reloads NM, stops
        `wpa_supplicant@wlan0`, persists `rollback_fired_at`
   - `run_takeover_attempt_if_eligible(...)` — lifespan-friendly
     one-shot entry point. Spawned by `app.py` at startup; runs
     once + exits.

3. **`backend/openmarquee/network_supervisor.py` (MODIFIED):**
   - `PersistedState` gains `takeover_active: bool = False` and
     `rollback_fired_at: float | None = None`. Schema version
     bumped 1 → 2.
   - `NetworkSupervisor` exposes `takeover_active`,
     `rollback_fired_at`, `set_takeover_active(bool)`,
     `mark_rollback_fired()`, `set_channel_follow_actuator(callable)`.
     The last lets the take-over orchestrator swap in the active
     actuator after a successful flip.

4. **`backend/openmarquee/wifi_station.py` (MODIFIED):**
   - `apply_enabled` short-circuits with a clear "failed" state +
     diagnostic detail when `supervisor.takeover_active` is True.
     Prevents confusing nmcli errors when an operator submits new
     credentials via the settings UI during a live take-over.
     Full settings-write delegation to `supervisor.apply_credentials`
     is deferred to P1.2-C.

5. **`backend/openmarquee/app.py` (MODIFIED):**
   - Lifespan startup spawns `run_takeover_attempt_if_eligible` as
     a separate one-shot task alongside the observe-loop. Env-gated
     by `OPENMARQUEE_DISABLE_NETWORK_TAKEOVER` (test/CI opt-out)
     AND the orchestrator's own `OPENMARQUEE_NETWORK_TAKEOVER_ENABLED`
     opt-in (default OFF).
   - Shutdown cancels the take-over evaluator if still running.
     The watchdog itself (when armed) continues independently — it
     holds its own asyncio.Task.

6. **Tests (NEW + extended; +25 across 2 new test files):**
   - `test_network_supervisor_actuator.py` (NEW, ~190 LOC): pure
     helpers + actuator happy path + each failure shape (target
     None, conf unreadable, systemctl non-zero, **post-verify
     mismatch**, iw binary missing, systemctl timeout) + atomic
     write semantics.
   - `test_network_supervisor_takeover.py` (NEW, ~290 LOC):
     Tailscale parser, NM drop-in + wpa_supplicant renderers,
     watchdog fire/disarm/idempotency, orchestrator flip + persistence,
     rollback path (drop-in deletion + systemctl ordering), **end-to-end
     watchdog-fires-rollback**, all 5 preconditions failure modes
     individually + the all-met case.
   - `test_network_supervisor.py` (EXTENDED): schema_version
     round-trip updated to v2.

### Env-gating + opt-in semantics

The take-over is **OFF by default**. Three env-var gates:

- `OPENMARQUEE_DISABLE_AUTOSTART=1` → skip supervisor entirely
  (umbrella test/CI flag).
- `OPENMARQUEE_DISABLE_NETWORK_SUPERVISOR=1` → spawn neither
  observe nor take-over.
- `OPENMARQUEE_NETWORK_TAKEOVER_ENABLED=1` → REQUIRED to opt in to
  take-over. Without it, the supervisor stays observe-only even
  when all other preconditions (Tailscale, credentials, etc.) are
  met.

On openMarqueeDev, QA opts in via a systemd drop-in for
`openmarquee-backend.service`:

```
[Service]
Environment=OPENMARQUEE_NETWORK_TAKEOVER_ENABLED=1
```

After `systemctl daemon-reload + systemctl restart openmarquee-backend`,
the take-over evaluator runs once at next startup.

### Verify procedure for openMarqueeDev

Per QA dispatch §"GO":

1. **Pre-flight check:** confirm Tailscale is the OOB path —
   `tailscale ping <peer>` succeeds before the flip.
2. **Take-over verify:** deploy P1.2-B; opt in via env var; restart
   service; watch journal:
   - `journalctl -u openmarquee-backend | grep network-takeover`
     should show the flip + arm + STA association
   - `nmcli device status` should show wlan0 as `unmanaged`
   - `systemctl status wpa_supplicant@wlan0.service` should be
     active
3. **Wifi bounce verify** (the soak gap from P1.2-A.1):
   ```
   wpa_cli -i wlan0 disconnect
   sleep 20
   wpa_cli -i wlan0 reconnect
   ```
   Confirm:
   - State machine walks `LINGER|ONLINE → DEGRADED → LINGER|ONLINE`
     (note: my state machine collapses CONNECTING → LINGER in the
     reassociation path; if QA wants the explicit intermediate
     CONNECTING state, ship as P1.2-B.1)
   - Watchdog stays disarmed (because the supervisor sees
     STA_ASSOCIATED before the 3-min timeout)
   - Tailscale session survives the wifi blip
4. **Rollback-watchdog drill** (per QA dispatch §"Reminder on the
   rollback watchdog"):
   - Set `settings.wifi_station_ssid` to a BOGUS SSID
   - Restart backend
   - Observe the take-over attempt + the 3-min watchdog fire
   - Confirm rollback restores wlan0 to NM management
5. **fallback_mutex end-to-end:** set `network_fallback_mutex_mode=true`
   in settings; restart; verify CONNECTING → ONLINE (skips LINGER);
   verify hostapd state matches the mutex contract
   (note: enforcement of "AP off when STA up" lands in P1.3 — the
   state machine flag is wired but the AP-control side-effects
   are observed-only in P1.2-B).

### What's NOT in P1.2-B (deferred to P1.2-C / P3+)

- **Operator credential delegation** through
  `supervisor.apply_credentials` (P1.2-C): the wifi_station shim
  currently warns + fails when takeover is active; the full
  delegation path replaces nmcli with wpa_cli RECONFIGURE.
- **nmcli profile migration** to wpa_supplicant blocks (P1.2-C).
- **DEGRADED active recovery** (P3): periodic `wpa_cli reconnect`
  during DEGRADED state. P1.2-B relies on the operator OR
  wpa_supplicant's own retry behavior.
- **AP-control side-effects** for fallback_mutex_mode (P3): the
  state machine flag is wired but `hostapd start/stop` on
  ONLINE/DEGRADED transitions lands in P3.
- **`scan-and-pick-best` absorption** (P3): `best-wifi.sh` and
  `wifi-watchdog` cron still run in parallel.

## §D.2 — P1.2-A deliverables (2026-06-10 — observe-only soak)

Shipped after QA's GO on (A) — sequenced ahead of P1.2-B
take-over per the de-risk pattern.

1. `backend/openmarquee/network_supervisor_loop.py` (NEW, ~250 LOC):
   - `parse_iw_freq_mhz(iw_output) -> int | None` — pure parser for
     the `channel <N> (<MHZ> MHz)` line of `iw dev wlan0 info`.
   - `poll_sta_freq_mhz()` async — best-effort subprocess of
     `iw dev wlan0 info`. Returns None on Mac dev / missing iw /
     timeout / no-association.
   - `supervisor_observe_loop(supervisor)` async — the long-running
     task. Maintains a `WpaSupplicantSocketClient` connection with
     30s reconnect cadence on failure, drains parsed wpa events
     into the supervisor every 500ms, polls STA freq every 10s.
     Logs missing-binary / missing-socket conditions ONCE (no
     stderr spam on Mac dev). On cancel, closes the socket
     client + re-raises CancelledError.
   - `_iw_binary_present()` — cheap which-style PATH probe.

2. `backend/openmarquee/api_network_supervisor.py` (NEW):
   - `GET /api/network-supervisor/state` — read-only observability
     surface. Returns the supervisor's state + STA freq + AP
     channel + fallback flag + diagnostics ring buffer (last 5
     min).

3. `backend/openmarquee/network_supervisor.py` (MODIFIED):
   - New `_emit(source, severity, message)` helper dual-emits to
     BOTH the diagnostics ring buffer AND the Python logger with
     a stable `[network-supervisor] source=... severity=...
     message=...` prefix. QA's grep pattern: `journalctl -u
     openmarquee-backend | grep '\[network-supervisor\]'`.
   - Load-bearing pushes (boot, state transitions, channel-follow
     decisions, default actuator) routed through `_emit`. Volume-
     heavy pushes (wpa raw events received) stay ring-buffer-only
     to keep journal noise bounded.
   - `_default_actuator` reworded to "observe-only" — explicit
     about no subprocess from the P1.2-A loop.
   - `lifespan_start` updated to match the new shape.

4. `backend/openmarquee/dependencies.py`:
   - `_network_supervisor_singleton` + `get_network_supervisor()` —
     process-wide singleton constructed from `SystemSettings.
     network_fallback_mutex_mode` at first call.

5. `backend/openmarquee/app.py`:
   - Imports `network_supervisor_router` + includes in the router
     list.
   - Lifespan startup spawns `supervisor_observe_loop` as an
     asyncio task (gated by `OPENMARQUEE_DISABLE_AUTOSTART` AND
     `OPENMARQUEE_DISABLE_NETWORK_SUPERVISOR` env vars).
   - Lifespan shutdown cancels the task + calls
     `supervisor.lifespan_stop`.

6. `backend/tests/test_network_supervisor_loop.py` (NEW, ~270 LOC):
   - 7 parametrized parse tests + 4 async loop tests (happy path,
     wpa event drives state machine, missing-socket survives,
     STA-freq poll cadence) + 2 smoke tests (interval defaults,
     binary probe returns bool).

7. `backend/tests/test_api_network_supervisor.py` (NEW): 2 tests
   for the state endpoint shape + the read-only invariant.

**Acceptance criteria for QA's P1.2-A live-fire soak on
openMarqueeDev:**

- Deploy main P1.2-A binary; service starts cleanly.
- `journalctl -u openmarquee-backend | grep '\[network-supervisor\]'`
  shows the parsed wpa events + state transitions + channel-follow
  decisions in real time.
- 30-min soak with the dev Pi joined to `pikazo` covers at least
  one association event (boot) and ideally one
  disassociate/reassociate round-trip.
- No subprocess actuation occurs (`grep -E '\b(systemctl|nmcli|iw
  set|hostapd_cli)\b'` over the observe-loop journal returns
  empty).
- The API endpoint at `/api/network-supervisor/state` returns the
  shape pinned by the test suite. **Auth-gated** — same bearer-
  token middleware as the rest of `/api/*`. Soak-side observability
  goes through `journalctl` (no token required); a curl-side check
  needs `Authorization: Bearer <token>` per QA decision 2026-06-10.
  (Decision rationale: the diagnostics ring buffer can include MAC
  addresses + SSID fields from CTRL-EVENT-CONNECTED parsing — not
  catastrophic but not appropriate for an unauthenticated endpoint
  either. The marginal value of an unauthenticated endpoint over
  the journalctl path doesn't justify the disclosure surface.)

**P1.2-A EXPLICITLY DEFERS** (P1.2-B):
- NM unmanage of wlan0
- wpa_supplicant@wlan0.service take-over
- wifi_station.py shim activation
- Active channel-follow actuator (hostapd config rewrite +
  systemctl restart hostapd)
- Pre-flight Tailscale connectivity check
- Self-healing rollback watchdog timer
- Fallback_mutex_mode end-to-end smoke

## §D.1 — P1.1 deliverables (2026-06-10 — supervisor skeleton)

Shipped AFTER QA's §C answers on 2026-06-10. Limited to the
SKELETON; the actual NM displacement + wifi_station.py shim +
nmcli-profile migration are sequenced as follow-up commits per
QA's de-risk plan.

1. `backend/openmarquee/network_supervisor.py` (NEW, ~620 LOC):
   - `SupervisorState` enum + `SupervisorEvent` enum
   - `next_state(state, event, fallback_mutex=False)` — pure
     functional state-transition table; `fallback_mutex=True`
     short-circuits CONNECTING → ONLINE (skips LINGER), matching
     spec §"Fallback position"
   - `freq_to_channel(freq_mhz)` — Python mirror of the P1.0
     shell math (authoritative reference now)
   - `decide_channel_follow(sta_freq, current_ap_channel)` — pure
     decision function returning `ChannelFollowDecision`
   - `DiagnosticsRingBuffer` — 5-min sliding-window in-memory
     event store; eviction on push + on snapshot
   - `hysteresis_allows_switch(candidate, current, threshold=8)` —
     r60 acceptance criterion (NM SIGNAL units, not dBm)
   - `in_band_ssh_guard_safe_to_switch(tailscale, lan_only)` —
     r60 acceptance criterion (Tailscale path is safe; LAN-only
     SSH blocks the switch)
   - `PersistedState` + `load_persisted_state` /
     `save_persisted_state` — atomic .tmp+rename JSON persistence
     at `/var/lib/openmarquee/network-state.json`; corrupt file
     returns None (defensive — caller starts from SETUP)
   - `WpaSupplicantSocketClient` — DGRAM client + ATTACH +
     non-blocking receive; works against either NM's singleton
     wpa_supplicant or our own (after the take-over commit)
   - `parse_wpa_event(raw)` — pure parser for CTRL-EVENT-* lines;
     maps to `SupervisorEvent` vocabulary
   - `NetworkSupervisor` — the orchestrator; lifespan_start /
     lifespan_stop hooks for FastAPI integration;
     `apply_event` / `apply_sta_freq` are the write surface;
     `snapshot_diagnostics` / `supervisor_to_dict` are the read
     surface

2. `backend/openmarquee/settings.py`:
   - `network_fallback_mutex_mode: bool = False` — the fallback
     flag QA mandated (de-risk via runtime flip without
     reinstall).

3. `backend/tests/test_network_supervisor.py` (NEW, ~430 LOC):
   60 tests covering the transition table (every state × event
   tuple incl. both fallback regimes), freq math, ring buffer,
   channel-follow decision, both r60 acceptance contracts,
   persistence round-trip + corrupt-file recovery, wpa event
   parser, and the supervisor's end-to-end skeleton.

4. `backend/tests/test_api_settings.py` (MODIFIED): adds the new
   field to the round-trip expected-dict.

5. `docs/onboarding-rework-plan.md`: §C answers from QA folded in
   ([RESOLVED 2026-06-10] markers on §C.1, C.2, C.3, C.4, C.5,
   C.6); this §D.1 section documents what P1.1 actually shipped.

**P1.1 EXPLICITLY DEFERS** (separate follow-up commits per QA's
de-risk sequence):
- Extending `NetworkManager-openmarquee-unmanaged.conf` to also
  unmanage wlan0 (the active take-over moment)
- `wpa_supplicant@wlan0.service` enablement
- `wifi_station.py` shim (so existing nmcli callers keep working
  through the supervisor)
- nmcli connection profile migration tool
- Scan-and-pick-best absorption (`best-wifi.sh` keeps running)
- iw event ring buffer (dmesg snapshot via subprocess)
- Active channel-follow actuator (current implementation logs to
  ring buffer only; doesn't write hostapd.conf or systemctl
  restart hostapd yet)

This commit ships a fully testable + observable skeleton that
runs on openMarqueeDev today. QA can verify state-machine
transitions via the journal + (when the API wiring lands) via
`/api/network-supervisor/state` without committing to the
take-over.

## §D.0 — P1.0 deliverables (2026-06-09 — diagnostics-first wins)

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
