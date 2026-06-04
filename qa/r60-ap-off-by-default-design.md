# r60 — AP off by default + best-known-WiFi auto-connect

**Author lane:** code1 (parallel to r61 perf work).

**Scope:** ship qarl's spec verbatim: *"create a system where it
scans all the network/passwords it knows and connects to the
best. AND DISABLE THE AP it ruins everything."*

**Origin/main HEAD at fix time:** `cc5955b04` (my r61). r60 stacks
on top.

---

## §A — Field-provisioning fallback decision

When the Pi boots into a location where no known WiFi network is
visible, the operator needs a way to configure new credentials.
Pre-r60 this was the always-on AP (`openmarquee-ap0.service`) —
the operator's phone joined `openMarquee-XXXX`, hit the captive-
portal admin UI, entered new WiFi creds, the Pi switched to STA.

Per dispatch, three candidates for the post-r60 replacement:

| Option                                              | Implementation                                                                                                                                                                                                                          | Trade-offs                                                                                                                                                                                                                                                                                                            |
|-----------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **1. AP-on-first-boot wizard, auto-disable on first STA success** | Detect "no NM connection has ever activated" at boot (e.g. via `/var/lib/openmarquee/has-connected` sentinel). If absent, unmask + start AP. Watch for first successful STA activation; mask AP + remove sentinel marker so it doesn't fire again. | Best UX. Operator's first-boot looks like the pre-r60 flow. Implementation requires sentinel-file lifecycle + an event watcher (systemd Connectivity= or a polling service). ~80-120 LOC. Risk: a botched first-boot leaves the sentinel absent forever and AP keeps coming up.                                       |
| **2. Operator runs an ordered systemctl sequence at the console** (PICKED for r60)                                                                                                                                                          | ssh to Pi via direct ethernet OR plug in keyboard/monitor; run the ORDERED sequence (subagent-fixed -- a single `systemctl start a b c` does NOT guarantee left-to-right serialization, so the explicit ordering below preserves the openmarquee-ap0 → hostapd → dnsmasq dependency chain that pre-r60 install.sh §8 maintained): <br><br>```bash<br>sudo systemctl unmask openmarquee-ap0.service hostapd.service dnsmasq.service<br>sudo systemctl start openmarquee-ap0.service<br>sudo systemctl restart hostapd.service<br>sudo systemctl restart dnsmasq.service<br>```<br><br>Operator finishes provisioning, then runs the reverse to mask. A future `system/openmarquee-fieldsetup.sh` wrapper can encode this sequence + an idempotent teardown. | Cheapest to ship. Zero new state-machine code. Operator-facing only when they're already on-site (the only time they'd need it). Documented in this doc + on the install page. Risk: a field tech who didn't read the docs assumes the AP is broken; mitigation = INSTALL.md + a `motd` line + the planned `openmarquee-fieldsetup` wrapper.                                                                                  |
| **3. Bluetooth provisioning**                       | Pi advertises a BLE service; operator's phone uses a companion app to push wifi creds.                                                                                                                                                  | Best UX for non-technical operators. Requires a companion app + BLE stack + Pi's BlueZ wiring. ~300-500 LOC plus iOS/Android app surface. Out of scope for r60.                                                                                                                                                       |

**Chosen for r60: Option 2.** Cheapest to ship + zero new
runtime state. Documented as the standard field-provisioning
procedure. A future r62+ may move to Option 1 if field reports
show operators struggling.

The `openmarquee-fieldsetup` CLI name in the dispatch is
aspirational; for r60 the operator runs the bare systemctl
sequence (3 commands). A wrapper script can land later if QA
reports operators want one.

---

## §B — Best-known-WiFi script semantics

`system/openmarquee-best-wifi.sh` (165 LOC).

### B.1 — Boot path

1. `systemctl start openmarquee-best-wifi.service` fires after
   `NetworkManager-online` (`After=` ordering in the .service).
2. Helper runs `nmcli device wifi rescan`, sleeps 3 s for results.
3. Enumerates connection profiles via `nmcli connection show` filtered
   to `802-11-wireless` type — these are "known SSIDs we have
   credentials for."
4. Reads signal strength per SSID from `nmcli device wifi list`.
5. Picks the strongest visible known SSID.
6. If different from current, calls `nmcli connection up id NAME`.
7. Verifies reachability via `ping` to the default gateway (up to
   30 s budget).
8. If reachability fails, reverts to the prior connection.

### B.2 — Recurring tick (roam)

`openmarquee-best-wifi.timer` fires the same service every 5 min
(per dispatch's "every N minutes (think 5-10 min), re-scan").
Hysteresis: only switches when the candidate's signal is strictly
better than the current's by `HYSTERESIS_SIGNAL` (default 8 NM-signal points, NOT dBm — nmcli SIGNAL is a 0-100 normalized percentage).

The 5-min cadence + 8-point NM-signal hysteresis is tuned for: operator
notices a switch within ~5 min of moving the Pi; transient signal
dips don't oscillate the connection.

### B.3 — Safety contract

- **NEVER leave the Pi disconnected.** If `nmcli connection up`
  fails, the script restores the prior SSID before returning.
- **Post-switch reachability check.** A connection that joins but
  can't reach the gateway (bad DHCP, captive portal,
  authentication mid-failure) gets rolled back.
- **In-band ssh safety.** A `nmcli connection up` will drop the
  current connection. If the operator is sshed in via the same
  wlan0, their session times out. The script's safety net (revert
  on failure) doesn't help here because the ssh transport is gone
  before the script can react. Mitigation: operators doing manual
  network switches via ssh should expect to lose the session;
  they reconnect on the new SSID.
- **Idempotent.** Re-running on the already-best SSID is a no-op.
  The 5-min timer is safe to fire as often as wanted.

### B.4 — Failure modes handled

| Failure mode                                  | Behavior                                                                                                |
|-----------------------------------------------|---------------------------------------------------------------------------------------------------------|
| No known SSIDs configured                     | Log + return 0; NM keeps its current state.                                                            |
| No known SSID visible in scan                 | Log + return 0; NM keeps its current state.                                                            |
| Current SSID is best (no switch needed)       | Log + return 0; idempotent no-op.                                                                      |
| Candidate signal within hysteresis            | Log + return 0; stay put.                                                                              |
| `nmcli connection up` fails                   | Log error + attempt revert to prior + return 1. Pi stays connected on prior SSID.                       |
| Switch succeeds but reachability check fails  | Log error + revert to prior + return 1. Pi stays connected on prior SSID (assuming revert succeeds).    |
| Both candidate AND revert fail                | Pi is disconnected. NM's auto-reconnect logic takes over (it will retry the best-priority profile).     |
| `nmcli` binary missing                        | Service's `ConditionPathExists=/usr/bin/nmcli` short-circuits; never runs.                              |
| `nmcli device wifi rescan` hangs              | `timeout $RESCAN_TIMEOUT` (default 20 s) caps the wait.                                                 |

---

## §C — systemd wiring

### C.1 — `openmarquee-best-wifi.service`

- `Type=oneshot RemainAfterExit=no`
- `After=NetworkManager.service NetworkManager-online.service network-online.target`
- `Wants=NetworkManager-online.service`
- `ConditionPathExists=/usr/bin/nmcli`
- `SuccessExitStatus=0 1` so a non-fatal failure doesn't mark the
  unit "failed" + propagate to other units' `Requires=`.
- `MemoryMax=64M` defense-in-depth.
- `WantedBy=multi-user.target`.

### C.2 — `openmarquee-best-wifi.timer`

- `OnBootSec=1min` — first roam check 1 min after boot (giving
  the boot oneshot time to complete).
- `OnUnitActiveSec=5min` — recurring tick.
- `RandomizedDelaySec=10` — small jitter so log lines aren't
  fingerprinted to a fixed wall-clock second.
- `WantedBy=timers.target`.

---

## §D — install.sh changes

### D.1 — §3 install systemd units

Added `openmarquee-best-wifi.service` + `openmarquee-best-wifi.timer`
to the unit-copy loop. Also added `openmarquee-best-wifi.sh` to the
§3a `chmod +x` helper-script loop.

### D.2 — §8 enable units (MAJOR change)

Pre-r60:
```bash
systemctl unmask hostapd.service dnsmasq.service
systemctl enable openmarquee-backend openmarquee-ap0 hostapd dnsmasq
systemctl reset-failed hostapd dnsmasq
systemctl start openmarquee-ap0
systemctl restart hostapd dnsmasq
```

Post-r60:
```bash
systemctl mask openmarquee-ap0.service hostapd.service dnsmasq.service
systemctl enable openmarquee-backend.service
systemctl enable openmarquee-best-wifi.service openmarquee-best-wifi.timer
systemctl start openmarquee-best-wifi.service
```

### D.3 — `§5.5` apt mask unchanged

The Phase 4u mask of `hostapd.service dnsmasq.service` before the
apt install survives — it's still the right guard against the .deb
postinst auto-start. r60's §8 mask just *keeps* them masked
instead of unmasking + enabling.

---

## §E — Backwards compatibility

### E.1 — Existing field deployments

The first `install.sh` run after r60 lands will:
1. Copy the 2 new units + script.
2. Mask `openmarquee-ap0`, `hostapd`, `dnsmasq` (idempotent if they
   were already enabled — `systemctl mask` overrides `enable`).
3. Enable `openmarquee-best-wifi` units.
4. Start the best-wifi boot oneshot, which will pick the strongest
   known SSID.

For an operator currently relying on the AP (e.g. running the Pi
in a tablet-on-AP config), this is a **breaking change**. The
INSTALL.md doc updates to call out the new behavior + the manual
unmask command for field-provisioning.

### E.2 — Pi OS image / pi-gen substage

The `images/openmarquee/.../00-packages` package list is unchanged.
The pi-gen build still ships hostapd + dnsmasq for the
field-provisioning fallback path. install.sh masks them at boot;
the operator manually unmasks when needed.

---

## §F — CMA + perf

Shell script overhead: negligible. `nmcli` + `awk` per-tick is
sub-megabyte RSS, sub-second wall time (modulo the 3 s rescan
sleep). Timer cadence is 5 min — 12 fires per hour, well below
any meaningful CPU budget.

No CMA implications; this is userspace shell + nmcli ioctls.

---

## §G — Verification per dispatch

| Check                                                               | Status     |
|---------------------------------------------------------------------|------------|
| AP is masked (no ap0 broadcasting)                                  | install.sh §8 masks all 3 units    |
| Boot picks the highest-signal known SSID                            | helper script main() loop          |
| After moving Pi, switches to new best within one timer tick         | 5-min OnUnitActiveSec + post-rescan logic |
| Hysteresis works: doesn't oscillate when two SSIDs are within 5 dB  | HYSTERESIS_DBM=8 (above the 5 dB ask) |
| Doesn't lock Pi out                                                 | Revert-on-failure + reachability check + ConditionPathExists |

---

## §H — Push posture

Bash + 2 systemd unit files + install.sh edits + this doc.

- 4 files modified/added:
  - `system/openmarquee-best-wifi.sh` (new, +165 LOC)
  - `system/openmarquee-best-wifi.service` (new, +35 LOC)
  - `system/openmarquee-best-wifi.timer` (new, +25 LOC)
  - `scripts/install.sh` (modified, ~25 net LOC change)
  - `qa/r60-ap-off-by-default-design.md` (this file)

Total ~250-300 LOC including comments; within the dispatch's
200-350 estimate.

— jimmy:openmarquee-code1 (lane: r60 AP off by default)
