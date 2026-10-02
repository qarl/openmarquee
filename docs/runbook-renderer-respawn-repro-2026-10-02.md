# Runbook: renderer respawn-loop repro window (qarl's sign, 2026-10-02)

Goal: reproduce handoff item (b) (`docs/handoff-supervisor-crashloop-2026-09-23.md`)
once, under measurement, then roll back. One window, one attempt.

Target: `openmarquee@192.168.1.174` (key `~/.ssh/id_ed25519_noether`), the only
HDMI Pi. rotation stays 90. All commands below run **on the sign** (one interactive
ssh session) unless marked **[Mac]**.

**Single actor:** openmarquee-code alone on the sign for the whole window. QA and
admin stay off it (no deploys, no ssh sessions, no restarts) until rollback is verified.

## What we already know (narrows what to capture)

The renderer's `rc=0` exits are **not crashes**. The sidecar exits 0 on exactly one of:
1. **stdin EOF**: the backend closed the pipe (`ipc_main.rs` ~1901);
2. **IPC `Close` op**: something called `RustRenderer.close()` (`ipc_main.rs` ~1915);
3. **SIGTERM/SIGINT**: clean-shutdown handler (`renderer/src/sigterm.rs`), which logs
   `[stability] shutdown_requested at IPC loop head` (`ipc_main.rs` ~2685).

The capture must say **which one, and who sent it**.

Reconnect budget: the backend respawns at most 3 times per 60s
(`DEFAULT_RECONNECT_MAX_RETRIES`/`_WINDOW_S`), then logs
`RustRenderer reconnect retries exhausted ... not respawning` and may fall back to
MockRenderer (which itself calls `close()`). **Attribute the FIRST death**; later
teardowns can be consequences.

Journal is persistent (300M cap, ~3 days retention): export evidence in step 6.

## Disruption

- Intermittent black/flicker while the loop runs (~3-5 min of capture).
- Rollback reboot: ~1-2 min dark.
- Total: ~**5-8 min** degraded display.
- **Abort** at any point = step 6 (export) then step 7, unless the sign is at risk,
  in which case go straight to step 7.

## Helpers (paste once)

`pgrep -x openmarquee-render` can NEVER match (process name is truncated to 15
chars). Resolve PIDs from the backend unit instead:
```
bpid() { systemctl show -p MainPID --value openmarquee-backend; }        # uvicorn (Type=notify, execs uvicorn)
rpid() { pgrep -n -P "$(bpid)" -f /usr/local/bin/openmarquee-render; }  # newest renderer child; empty mid-respawn
```

## Steps

**0. Pre-check (sign stays clean).**
```
command -v strace && strace -V | head -1
cat /etc/systemd/system/openmarquee-backend.service.d/disable-netsup.conf   # expect ...SUPERVISOR=1
grep -n 'DISABLE_NETWORK_SUPERVISOR' /opt/openmarquee/backend/openmarquee/app.py   # expect != "1"
echo "bpid=$(bpid) rpid=$(rpid)"; df -h /
date -Is | tee /tmp/repro-start-time.txt                                    # --since fallback
sudo journalctl -n1 --show-cursor --no-pager | tail -1 | sed 's/^-- cursor: //' | tee /tmp/repro-cursor.txt
```
Abort if the cursor is empty AND no start time was recorded.

**1. Re-enable the supervisor via a RUNTIME drop-in (leave /etc untouched).**
`/run` is tmpfs, so ANY reboot (including a pulled plug or a lost ssh session)
restores the known-good disabled state. `zz-` sorts after `disable-netsup.conf`,
so its `=0` wins; the app enables the supervisor for any value `!= "1"`.
```
sudo mkdir -p /run/systemd/system/openmarquee-backend.service.d
printf '[Service]\nEnvironment=OPENMARQUEE_DISABLE_NETWORK_SUPERVISOR=0\n' | sudo tee /run/systemd/system/openmarquee-backend.service.d/zz-repro.conf
sudo systemctl daemon-reload
systemctl show openmarquee-backend -p Environment | tr ' ' '\n' | grep DISABLE_NETWORK   # expect =0
```

**2. Trigger: a plain fast restart** (deploy-style stop→delay→start did NOT loop on 09-23).
```
sudo systemctl restart openmarquee-backend
```

**3. Confirm the loop (≤2 min).** Renderer PID should change every ~13-15s:
```
for i in $(seq 1 45); do echo "$(date +%T) bpid=$(bpid) rpid=$(rpid)"; sleep 2; done
```
No rpid change within 2 min → **no repro**: note it, then step 6, then step 7.
rpid empty for good after a few cycles → reconnect budget exhausted; go to 4a.

**4. Capture while it loops: cheapest first, ONE tracer at a time.**
Observer effect: strace can shift timing on a Pi Zero enough to hide the bug.
Escalate only if the previous tier didn't name the cause. Never run both straces
at once. Both straces use `-f --seccomp-bpf` (filtered syscalls only stop the
tracee, which is much lighter than plain ptrace).

- **4a. Journal (free, no attach).** After ~60s of looping:
  ```
  sudo journalctl --after-cursor "$(cat /tmp/repro-cursor.txt)" --no-pager \
    | grep -E "RustRenderer reconnect|subprocess died|retries exhausted|shutdown_requested|SIGTERM|close|perf summary|rust-sidecar stderr" | head -60
  ```
  Read the FIRST death: `shutdown_requested` right before it = SIGTERM;
  reconnect reason + the last sidecar stderr lines may name the path. If named,
  skip to step 5.
- **4b. Renderer strace (cheap, single process tree).** EOF vs Close vs signal:
  ```
  R=$(rpid); [ -n "$R" ] && sudo timeout 60 strace -f --seccomp-bpf -tt -e trace=read,exit_group -s 200 -p "$R" -o /tmp/strace-render.txt || echo "no renderer pid"
  ```
  - `read(0, "", ...) = 0` then `exit_group(0)` → **EOF**
  - a `{"op":"close"...` read → **Close op**
  - `--- SIGTERM {si_signo=SIGTERM, ..., si_pid=N ...}` → **signal**, and `N` is
    the sender (`ps -o pid,cmd -p N`, or compare with `bpid`). Signals are shown by
    default.
- **4c. uvicorn strace, ONLY if 4a+4b don't name the sender/caller.**
  ```
  sudo timeout 60 strace -f --seccomp-bpf -tt -T -e trace=write,close,kill,tgkill -s 120 -p "$(bpid)" -o /tmp/strace-uvicorn.txt
  ```
  If the loop stops while traced, that is itself a timing signal: record it, detach.
- py-spy is NOT installed in /opt venv; skip unless pre-installed before the window
  (`/opt/openmarquee/venv/bin/pip install py-spy`, needs network).

**5. Note the on-screen state** (DEGRADED "Lost the wifi connection" card yes/no, timing).

**6. Export evidence before rollback.**
```
sudo journalctl --after-cursor "$(cat /tmp/repro-cursor.txt)" -o short-iso --no-pager > /tmp/repro-journal.txt \
  || sudo journalctl --since "$(cat /tmp/repro-start-time.txt)" -o short-iso --no-pager > /tmp/repro-journal.txt
grep -E "RustRenderer reconnect|subprocess died|retries exhausted|shutdown_requested|SIGTERM|rust-sidecar|supervisor|DEGRADED" /tmp/repro-journal.txt > /tmp/repro-key.txt
```
**[Mac]** copy off before step 7:
```
scp -i ~/.ssh/id_ed25519_noether 'openmarquee@192.168.1.174:/tmp/repro-*.txt' 'openmarquee@192.168.1.174:/tmp/strace-*.txt' <scratch dir>/
```

**7. Rollback (always).**
```
sudo rm -f /run/systemd/system/openmarquee-backend.service.d/zz-repro.conf
sudo systemctl daemon-reload
sudo reboot
```
Verify after boot (~2 min):
```
systemctl show openmarquee-backend -p Environment | tr ' ' '\n' | grep DISABLE_NETWORK   # expect =1
systemctl is-active openmarquee-backend                                                # active (not stuck on mini)
ls /var/lib/openmarquee/backend-failure-reboot 2>&1                                    # expect: No such file
for i in 1 2 3 4; do rpid; sleep 15; done                                              # one stable renderer PID
```
Plus by eye: no DEGRADED card, content playing. Supervisor-disabled is the
known-good state as of 09-23.

## What each outcome means

- **EOF + uvicorn closes the pipe** → a backend teardown/reconnect path fires; the
  4c thread/timestamp plus the reconnect-reason log line name the caller.
- **Close op** → something calls `RustRenderer.close()` (wrapper teardown /
  `_swap_to_mock` in `dependencies.py` are the only callers found so far; check it
  isn't just the post-exhaustion Mock fallback).
- **SIGTERM** → `si_pid` names the sender (the backend itself, systemd, or another
  unit). Check for `kill`/`tgkill` in 4c if the sender is uvicorn.
- **No repro on fast restart** → timing-dependent; record it and stop. Do not
  iterate on the live sign in the same window.
