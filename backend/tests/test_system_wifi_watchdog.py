"""WiFi watchdog mitigation #2 + #4 regression lock (2026-05-23).

qarl-direct ship-blocker postmortem 2026-05-23: today's FYS Pi
"outage" was an AP-side deauth (reason 16 = group_cipher_not_valid)
that DHCP-renumbered the Pi `.67` → `.69`. The existing
`wifi-watchdog.sh` observed the wedge (logged three consecutive
`no default gateway` lines) but took NO action because its no-GW
branch incremented `fails` without ever calling
`systemctl restart NetworkManager` — only the ping-fail branch did.

Mitigation #2 hardening:

1. NM restart fires from BOTH branches (no-GW + ping-fail), gated
   by the same THRESHOLD check and resetting `fails=0` after
   escalation.
2. THRESHOLD lowered 3 → 2 (~3-min floor → ~60s).
3. Cron cadence lowered 1 min → 30 s (two cron lines, one offset
   by `sleep 30 &&` — cron's smallest native unit is 1 min).
4. Every file-log write paired with a `logger -t wifi-watchdog`
   journal write so post-mortem cross-correlation with NM /
   wpa_supplicant doesn't require BST↔UTC reconciliation.
5. `set -e` / `set -o pipefail` removed (`set -u` kept) — cron is
   a context where silent script abort is the worst-case outcome,
   and every command's exit is checked explicitly.
6. Cron entry checked into the repo at `system/openmarquee-wifi-
   watchdog` so cadence changes are deployable + auditable rather
   than drift surface.

Mitigation #4 (auto-reboot on watchdog escalation):

7. NM-restarts are recorded in `/var/run/wifi-watchdog.restarts`
   (tmpfs ledger, one epoch ts per line).
8. After each NM-restart, the ledger is pruned to entries inside
   REBOOT_WINDOW_SECONDS (default 1800s, widened from 600s on
   2026-05-24); if the remaining count meets
   REBOOT_AFTER_N_RESTARTS (default 5, widened from 3), the script issues
   `systemctl reboot` — kernel-level firmware re-init for the
   brcmfmac wedge case where NM restart alone CAN'T recover.
9. The ledger is wiped BEFORE the reboot call (anti-reboot-loop;
   tmpfs would also wipe on boot, this is belt-and-suspenders).
10. Pruning uses the keep-criterion `ts >= cutoff` rather than
    delta math, so NTP backward jumps don't trigger negative-delta
    surprises (the wedge could correlate with NTP drift on a Pi
    without RTC).

Static parse — same shape as D2 / M5 / H4 / inline-preview font-
load / web-slide preview closures. The fences below catch a future
"helpful" refactor that silently regresses any of the load-bearing
mechanisms.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _REPO / "scripts" / "wifi-watchdog.sh"
_CRON = _REPO / "system" / "openmarquee-wifi-watchdog"
_SIBLING = _REPO / "scripts" / "wifi-preemptive-reload.sh"


def _read_script_source() -> str:
    """Read `wifi-watchdog.sh` and strip shell `#` comments so
    narrative mentions of locked symbols in the header docstring
    don't false-pass the assertions. Inline `#` after code (e.g.
    `set -u  # comment`) is also stripped."""
    assert _SCRIPT.is_file(), (
        f"wifi-watchdog.sh not found at {_SCRIPT}; relocation? Update the test path."
    )
    text = _SCRIPT.read_text(encoding="utf-8")
    # Strip `#`-to-EOL comments. Keep the shebang (`#!`) by anchoring
    # on `#` only when NOT followed immediately by `!` on line 1.
    lines = []
    for i, line in enumerate(text.splitlines()):
        if i == 0 and line.startswith("#!"):
            lines.append(line)
            continue
        stripped = re.sub(r"#.*$", "", line)
        lines.append(stripped)
    return "\n".join(lines)


def _read_cron_source() -> str:
    assert _CRON.is_file(), (
        f"openmarquee-wifi-watchdog cron file not found at {_CRON}; "
        f"relocation? Update the test path."
    )
    return _CRON.read_text(encoding="utf-8")


def test_threshold_is_two() -> None:
    """THRESHOLD=2 (lowered from 3 per postmortem mitigation #2).
    Paired with the 30s cron cadence this gives a ~60s minimum
    detection floor — fast enough to act before customer-facing
    impact on an AP-deauth event."""
    source = _read_script_source()
    assert re.search(r"^\s*THRESHOLD=2\s*$", source, flags=re.MULTILINE), (
        "THRESHOLD must be set to 2 — the postmortem-named value "
        "for the AP-deauth recovery floor. A regression to 3 (or "
        "anything higher) restores the ~3-min detection floor that "
        "missed today's wedge entirely."
    )


def test_no_gateway_branch_restarts_network_manager() -> None:
    """The no-default-gateway branch MUST escalate to a
    NetworkManager restart on threshold — the EXACT bug observed
    2026-05-23. Previously this branch only incremented `fails`
    and logged; today's actual wedge took this path and the
    watchdog took zero recovery action."""
    source = _read_script_source()
    # Find the `if [ -z "$gw" ]; then ... elif` block — the
    # no-default-gateway arm — and assert it contains both the
    # threshold check AND the systemctl-restart call inside.
    match = re.search(
        r'if \[ -z "\$gw" \];\s*then(.*?)\belif\b',
        source,
        flags=re.DOTALL,
    )
    assert match, (
        'could not locate the `if [ -z "$gw" ]; then ... elif` '
        "no-default-gateway branch — refactor may have restructured "
        "the control flow. Re-confirm the branch still escalates."
    )
    body = match.group(1)
    assert re.search(r'\[\s*"\$fails"\s*-ge\s*"\$THRESHOLD"\s*\]', body), (
        "no-default-gateway branch missing THRESHOLD escalation "
        "check — this is the precise gap mitigation #2 fixes. "
        "Without it, an AP-deauth wedge will be observed but never "
        "acted on (the 2026-05-23 failure mode)."
    )
    assert "systemctl restart NetworkManager" in body, (
        "no-default-gateway branch missing `systemctl restart "
        "NetworkManager` call — escalation check is present but "
        "the recovery action isn't wired. Re-confirm the branch "
        "actually restarts NM on threshold."
    )
    # Both branches must reset `fails=0` after escalation so the
    # next fire starts a fresh count rather than ratcheting up
    # indefinitely. (Subagent-review-named invariant.)
    assert re.search(r'echo\s+0\s*>\s*"\$STATE_FILE"', body), (
        "no-default-gateway branch missing the post-escalation "
        "`echo 0 > $STATE_FILE` reset — without it, `fails` "
        "ratchets up indefinitely and every subsequent fire "
        "re-triggers NM-restart."
    )


def test_ping_fail_branch_restarts_network_manager() -> None:
    """The ping-fail branch (the `else` arm, after no-GW + ping-OK)
    MUST also restart NM on threshold + reset fails. This was
    already working pre-mitigation-#2 — the test pins it so a
    refactor that consolidates the two branches doesn't lose the
    ping-path escalation."""
    source = _read_script_source()
    # Match the trailing `else` arm from after the `elif ping ...`
    # to the closing `fi`. Greedy/non-greedy doesn't matter here —
    # the script has exactly one `else ... fi` after the ping-OK arm.
    match = re.search(
        r"elif ping[^\n]*then.*?\belse\b(.*?)\bfi\b\s*$",
        source,
        flags=re.DOTALL,
    )
    assert match, (
        "could not locate the ping-fail `else` arm — refactor may "
        "have restructured the control flow. Re-confirm the ping "
        "branch still escalates to NM-restart."
    )
    body = match.group(1)
    assert re.search(r'\[\s*"\$fails"\s*-ge\s*"\$THRESHOLD"\s*\]', body), (
        "ping-fail branch missing THRESHOLD escalation check — "
        "regression: this was working before mitigation #2."
    )
    assert "systemctl restart NetworkManager" in body, (
        "ping-fail branch missing `systemctl restart NetworkManager` "
        "call — regression: this was working before mitigation #2."
    )
    assert re.search(r'echo\s+0\s*>\s*"\$STATE_FILE"', body), (
        "ping-fail branch missing post-escalation `echo 0 > "
        "$STATE_FILE` reset — `fails` would ratchet up indefinitely."
    )


def test_journal_logging_invoked() -> None:
    """Every file-log write must be paired with a journal write
    via `logger -t wifi-watchdog` so post-mortem cross-correlation
    with NetworkManager / wpa_supplicant entries doesn't require
    BST↔UTC reconciliation (the 20-min forensics cost from the
    2026-05-23 postmortem). The current script wraps both in a
    `note()` helper invoked ≥4 times — three info messages (no-GW,
    NM-restart-from-no-GW, ping-fail, NM-restart-from-ping-fail,
    ping-OK-recovery) plus the reset message."""
    source = _read_script_source()
    assert "logger -t wifi-watchdog" in source, (
        "`logger -t wifi-watchdog` invocation missing — journal "
        "logging not wired. Post-mortem cross-correlation with NM/"
        "wpa_supplicant journal entries will require TZ "
        "reconciliation again."
    )
    note_calls = re.findall(r'\bnote\s+"', source)
    assert len(note_calls) >= 4, (
        f"expected ≥4 `note` calls (one per file-log site to keep "
        f"the file + journal timelines in lockstep); found "
        f"{len(note_calls)}. A refactor likely consolidated or "
        f"dropped log sites."
    )


def test_path_preamble_in_script() -> None:
    """Cron's default PATH does not include /usr/sbin where
    systemctl lives. The script must export an explicit PATH so a
    refactor that removes the cron-side PATH= line doesn't silently
    break NM-restart calls."""
    source = _read_script_source()
    assert re.search(
        r"export\s+PATH=/usr/sbin:/usr/bin:/sbin:/bin",
        source,
    ), (
        "script PATH preamble missing — without /usr/sbin in PATH "
        "the `systemctl restart NetworkManager` calls fail silently "
        "under cron."
    )


def test_pipefail_relaxed_for_cron_safety() -> None:
    """`set -e` and `set -o pipefail` were removed (`set -u` kept)
    so a non-zero exit from `ip route show` or a similar query
    can't abort the watchdog silently mid-cron. Postmortem §3
    flagged this as a real risk."""
    source = _read_script_source()
    assert not re.search(r"set\s+-[eu]*e[u]*o\s+pipefail", source), (
        "`set -euo pipefail` (or any combination including -e or "
        "pipefail) is present — under cron a non-zero exit from "
        "`ip route show` or a similar query would abort the "
        "watchdog silently. Use explicit per-command exit checks "
        "instead."
    )
    # Confirm `set -u` (unset-var safety) IS kept — catches
    # typos at runtime without killing on harmless command
    # failures.
    assert re.search(r"^\s*set\s+-u\s*$", source, flags=re.MULTILINE), (
        "`set -u` removed — unset-variable safety lost. Keep it "
        "(catches refactor typos like `$STATE_FIL` writing to /)."
    )


def test_cron_entry_has_30s_cadence() -> None:
    """Cron file must fire the watchdog twice per minute (once at
    :00, once at :30 via `sleep 30 &&`). Cron's smallest native
    unit is 1 min, so the double-line is the standard idiom for
    sub-minute cadence. The flock wrapper (2026-05-24) is allowed
    to sit between `root` and the script — the cadence pattern
    survives the wrap because flock is structurally part of how
    we run the script, not part of the schedule itself."""
    cron = _read_cron_source()
    # First line: standard every-minute fire. Tolerates an optional
    # `flock` wrapper between `root` and the script path.
    assert re.search(
        r"^\*\s+\*\s+\*\s+\*\s+\*\s+root\s+(?:/usr/bin/flock\s+\S+\s+\S+\s+)?"
        r"/usr/local/bin/wifi-watchdog\.sh\s*$",
        cron,
        flags=re.MULTILINE,
    ), (
        "cron file missing the at-:00 fire line "
        "(`* * * * * root [flock prefix] /usr/local/bin/wifi-"
        "watchdog.sh`) — regression to 1-min cadence."
    )
    # Second line: offset-by-30s fire. Same flock-tolerant shape.
    assert re.search(
        r"^\*\s+\*\s+\*\s+\*\s+\*\s+root\s+sleep\s+30\s*&&\s*"
        r"(?:/usr/bin/flock\s+\S+\s+\S+\s+)?"
        r"/usr/local/bin/wifi-watchdog\.sh\s*$",
        cron,
        flags=re.MULTILINE,
    ), (
        "cron file missing the at-:30 fire line "
        "(`* * * * * root sleep 30 && [flock prefix] /usr/local/"
        "bin/wifi-watchdog.sh`) — without it the cadence is 1 min, "
        "not 30s, and detection floor doubles to ~120s."
    )


def test_cron_entry_has_path_preamble() -> None:
    """Cron's default PATH lacks /usr/sbin — without explicit PATH
    the systemctl call in the script can fail silently. The
    script ALSO sets PATH but the cron-side line is belt-and-
    suspenders + matches the sibling `wifi-preemptive-reload`
    cron deploy pattern."""
    cron = _read_cron_source()
    assert re.search(
        r"^PATH=/usr/sbin:/usr/bin:/sbin:/bin\s*$",
        cron,
        flags=re.MULTILINE,
    ), (
        "cron file missing the `PATH=` preamble — without it, "
        "cron's minimal PATH means systemctl/ip/logger may not "
        "resolve."
    )


def test_sibling_preemptive_reload_not_modified() -> None:
    """Sanity-check fence: mitigation #2 explicitly does NOT touch
    `wifi-preemptive-reload.sh` (the daily 03:00 sibling). This
    test asserts the sibling file still exists at its known path —
    a refactor that accidentally merged the two scripts would
    delete this file. If you legitimately want to merge them, this
    test is the place to update."""
    assert _SIBLING.is_file(), (
        f"wifi-preemptive-reload.sh missing at {_SIBLING} — "
        f"mitigation #2 was supposed to leave the daily-03:00 "
        f"sibling untouched. Did a refactor merge the two scripts?"
    )


# --- Mitigation #4 — auto-reboot on watchdog escalation ----------


def test_restarts_file_constant_present() -> None:
    """The NM-restart ledger lives at /var/run/wifi-watchdog.restarts
    on tmpfs — the path matters for the anti-reboot-loop guarantee
    (tmpfs auto-wipes on boot, so even if the explicit pre-reboot
    wipe somehow failed, a reboot still clears the counter)."""
    source = _read_script_source()
    assert re.search(
        r"^\s*RESTARTS_FILE=/var/run/wifi-watchdog\.restarts\s*$",
        source,
        flags=re.MULTILINE,
    ), (
        "RESTARTS_FILE constant must point at /var/run/wifi-"
        "watchdog.restarts. A path off /var/run (a persistent fs) "
        "would break the tmpfs auto-wipe-on-boot anti-reboot-loop "
        "safety property."
    )


def test_reboot_threshold_is_five() -> None:
    """REBOOT_AFTER_N_RESTARTS == 5 (Path 1 widen-envelope, 2026-05-24).

    Original value was 3 (postmortem #4) but live measurement on FYS
    during the 11:00-11:50 wedge investigation showed real
    catastrophic 0/5-burst windows lasting 60-120s. 3 NM-restarts
    in 600s tripped on every degraded RF window, producing reboots
    every ~7-10 min. 5 NM-restarts in 1800s (paired with the new
    window) gives the system room to ride out a transient bad-RF
    pocket without the operator seeing the sign blank.

    Below 5 re-introduces the pre-fix reboot rate; above 5 risks
    sitting on a genuinely-wedged chip for too long. Pin to 5 so a
    future "tighten this back" refactor surfaces this trade-off
    instead of silently undoing it."""
    source = _read_script_source()
    assert re.search(
        r"^\s*REBOOT_AFTER_N_RESTARTS=5\s*$",
        source,
        flags=re.MULTILINE,
    ), (
        "REBOOT_AFTER_N_RESTARTS must be 5 (the Path 1 widen-envelope "
        "value, 2026-05-24). Lower values re-introduce the every-7-"
        "10-min reboot rate measured on FYS; higher values sit on a "
        "genuinely-wedged chip for too long."
    )


def test_reboot_window_is_1800s() -> None:
    """REBOOT_WINDOW_SECONDS == 1800 (Path 1 widen-envelope, 2026-05-24).

    Original value was 600 (10 min). Widened to 1800 (30 min) so a
    sustained-but-transient bad-RF period can absorb 5 NM-restart
    attempts before triggering the kernel-level reboot. Paired with
    REBOOT_AFTER_N_RESTARTS=5: 5 restarts in 30 min = ~6 min apart,
    which is the natural cadence of the THRESHOLD=2-failures-then-
    NM-restart pattern under sustained loss. Above 1800 starts to
    accumulate stale restart events that no longer reflect current
    chip health."""
    source = _read_script_source()
    assert re.search(
        r"^\s*REBOOT_WINDOW_SECONDS=1800\s*$",
        source,
        flags=re.MULTILINE,
    ), (
        "REBOOT_WINDOW_SECONDS must be 1800 (the Path 1 widen-"
        "envelope value, 2026-05-24). Smaller windows re-trigger "
        "reboots too quickly under sustained-but-transient loss; "
        "larger windows accumulate stale restart events that no "
        "longer reflect current chip health."
    )


def test_systemctl_reboot_invoked() -> None:
    """The script must actually invoke `systemctl reboot` — the
    escalation action of last resort when NM restarts can't recover
    the brcmfmac firmware wedge. A refactor that dropped the call
    would leave the script logging the escalation decision without
    acting on it (the precise shape of the 2026-05-23 no-op bug
    mitigation #2 fixed for NM-restart).

    The call must appear EXACTLY ONCE in the source. A duplicate
    ungated call elsewhere — e.g. someone adding a "while-we're-at-it
    reboot from a different branch" — would break the single-gate
    invariant test_reboot_is_count_gated otherwise enforces."""
    source = _read_script_source()
    matches = re.findall(r"\bsystemctl\s+reboot\b", source)
    assert len(matches) == 1, (
        f"`systemctl reboot` must appear exactly once (count="
        f"{len(matches)}). Zero = escalation unwired (the brcmfmac "
        f"firmware wedge recovery doesn't fire). More than one = a "
        f"duplicate ungated call may have slipped in outside the "
        f"REBOOT_AFTER_N_RESTARTS gate."
    )


def test_reboot_is_count_gated() -> None:
    """The `systemctl reboot` call must be gated by a count
    comparison against REBOOT_AFTER_N_RESTARTS — fences a refactor
    that ungates the reboot (calling it from every NM-restart path
    unconditionally would auto-reboot on the FIRST flap)."""
    source = _read_script_source()
    # Find the reboot helper body and verify both the count check
    # and the reboot call live inside the same function. We match
    # from the helper definition's `{` through to the closing `}`.
    helper = re.search(
        r"record_nm_restart_and_maybe_reboot\s*\(\s*\)\s*\{(.*?)\n\}",
        source,
        flags=re.DOTALL,
    )
    assert helper, (
        "record_nm_restart_and_maybe_reboot() helper not found in "
        "expected shape. Refactor may have inlined or restructured "
        "the reboot path — re-confirm the count gate is preserved."
    )
    body = helper.group(1)
    # The body must contain BOTH the count comparison AND the
    # reboot call. Use `-ge` against the threshold variable name.
    assert re.search(
        r'\[\s*"\$count"\s*-ge\s*"\$REBOOT_AFTER_N_RESTARTS"\s*\]',
        body,
    ), (
        'helper missing `[ "$count" -ge "$REBOOT_AFTER_N_RESTARTS" ]` '
        "gate before the reboot call — refactor may have ungated the "
        "reboot, making it fire on EVERY NM-restart."
    )
    assert "systemctl reboot" in body, (
        "helper missing `systemctl reboot` call in its body — the "
        "count gate may have been preserved but the action removed."
    )


def test_ledger_wiped_before_reboot() -> None:
    """The restart ledger MUST be wiped before `systemctl reboot`.
    Without the wipe — and absent the tmpfs auto-wipe-on-boot
    safety net (which already protects /var/run) — a post-reboot
    Pi could inherit the 3-restart state and reboot again
    immediately. The wipe is belt-and-suspenders defense against
    /var/run being relocated to a persistent fs."""
    source = _read_script_source()
    # Match the helper body and confirm `: > "$RESTARTS_FILE"` (or
    # any equivalent wipe) appears BEFORE the systemctl reboot call.
    helper = re.search(
        r"record_nm_restart_and_maybe_reboot\s*\(\s*\)\s*\{(.*?)\n\}",
        source,
        flags=re.DOTALL,
    )
    assert helper, (
        "record_nm_restart_and_maybe_reboot() helper not found — see "
        "test_reboot_is_count_gated for the same diagnostic."
    )
    body = helper.group(1)
    wipe = re.search(r':\s*>\s*"\$RESTARTS_FILE"', body)
    reboot = re.search(r"systemctl\s+reboot", body)
    assert wipe, (
        'ledger wipe (`: > "$RESTARTS_FILE"`) missing from the '
        "reboot helper — anti-reboot-loop relies on this wipe "
        "PLUS the tmpfs auto-wipe-on-boot."
    )
    assert reboot and wipe.start() < reboot.start(), (
        f"ledger wipe must occur BEFORE the `systemctl reboot` call "
        f"(wipe at offset {wipe.start() if wipe else 'missing'}, "
        f"reboot at {reboot.start() if reboot else 'missing'}). If "
        f"the wipe lands after reboot it's a no-op."
    )


def test_pruning_uses_keep_criterion_not_delta_math() -> None:
    """The ledger prune must use the keep-criterion `ts >= cutoff`
    (where cutoff = now - REBOOT_WINDOW_SECONDS) rather than delta
    math like `(now - ts) < window`. On an RTC-less Pi, NTP can
    backward-jump and produce future-dated timestamps; delta math
    would yield a negative value and the comparison semantics get
    surprising. The keep-criterion handles future-dated entries
    correctly (kept and counted, as they should be — they're
    real events)."""
    source = _read_script_source()
    # The keep comparison should appear inside the helper body as
    # `[ "$ts_line" -ge "$cutoff" ]` (or any anchored variant).
    helper = re.search(
        r"record_nm_restart_and_maybe_reboot\s*\(\s*\)\s*\{(.*?)\n\}",
        source,
        flags=re.DOTALL,
    )
    assert helper, "record_nm_restart_and_maybe_reboot() helper missing"
    body = helper.group(1)
    assert re.search(
        r'\[\s*"\$ts_line"\s*-ge\s*"\$cutoff"\s*\]',
        body,
    ), (
        'ledger prune doesn\'t use `[ "$ts_line" -ge "$cutoff" ]` '
        "keep-criterion. A delta-math refactor (`now - ts < window`) "
        "behaves surprisingly under NTP backward-jumps on an "
        "RTC-less Pi — future-dated entries produce negative deltas."
    )


# ---- 2026-05-24: burst-ping check (false-positive mitigation) ----


def test_ping_burst_constants_present() -> None:
    """Burst-ping uses 5-of-5 with a 3-of-5 minimum to OK.

    Measured on FYS 2026-05-24 during the wedge investigation: a
    single ping every 30s showed ~55% loss to the gateway while a
    burst of 5 pings at 0.2s spacing showed 0% loss against the SAME
    target. The single-ping cadence false-positive-ed heavily,
    driving the auto-reboot loop. The 5/3 numbers are load-bearing
    enough to pin in the source so a 'tighten this further' refactor
    can't silently re-introduce the single-ping shape.
    """
    source = _read_script_source()
    assert re.search(r"^\s*PING_BURST_COUNT=5\s*$", source, flags=re.MULTILINE), (
        "PING_BURST_COUNT must be 5 — single-ping or smaller bursts "
        "re-introduce the rate-dependent false-positive shape we "
        "shipped this fix to escape."
    )
    assert re.search(r"^\s*PING_BURST_OK_MIN=3\s*$", source, flags=re.MULTILINE), (
        "PING_BURST_OK_MIN must be 3 — anything stricter rolls back "
        "to false-positive territory; anything looser misses real "
        "outages."
    )


def test_ping_burst_function_uses_burst_pattern() -> None:
    """`ping_burst_ok` must call ping with the 5-packet, 0.2s-spacing,
    1s-per-packet-timeout pattern. These flags are what defines the
    'coherent ~1-second probe' shape — losing any one of them turns
    the function into something else (-c 1 = single ping, -i >0.5 =
    rate-dependent again, -W >2 = slow probe stalls the cron run).
    """
    source = _read_script_source()
    match = re.search(
        r"ping_burst_ok\(\)\s*\{(.*?)\n\}",
        source,
        flags=re.DOTALL,
    )
    assert match, (
        "ping_burst_ok() function not found — the burst-check was "
        "removed or renamed. Re-add or update this test."
    )
    body = match.group(1)
    assert re.search(r'ping\s+-c\s+"\$PING_BURST_COUNT"', body), (
        'ping_burst_ok must pass `-c "$PING_BURST_COUNT"` so the '
        "5-packet count stays driven by the constant, not a literal."
    )
    assert "-i 0.2" in body, (
        "ping_burst_ok must use `-i 0.2` interval — wider spacing "
        "re-introduces the rate-dependent false-positive pattern."
    )
    assert "-W 1" in body, (
        "ping_burst_ok must use `-W 1` per-packet timeout so the "
        "whole burst caps at ~1s wallclock; -W 2 or more makes the "
        "cron run drag and risks watchdog overlap."
    )


def test_ping_burst_function_uses_ok_min_threshold() -> None:
    """The function's accept/reject decision must compare PING_RECEIVED
    against PING_BURST_OK_MIN — pinning the >=3 of 5 rule into the
    source so a refactor can't silently change it to e.g. '>= 1' (=
    same as single-ping) or '== PING_BURST_COUNT' (= no tolerance)."""
    source = _read_script_source()
    match = re.search(
        r"ping_burst_ok\(\)\s*\{(.*?)\n\}",
        source,
        flags=re.DOTALL,
    )
    assert match, "ping_burst_ok() not found"
    body = match.group(1)
    assert re.search(
        r'\[\s*"\$PING_RECEIVED"\s*-ge\s*"\$PING_BURST_OK_MIN"\s*\]',
        body,
    ), (
        'ping_burst_ok must gate on `[ "$PING_RECEIVED" -ge '
        '"$PING_BURST_OK_MIN" ]` — anything else (e.g. -gt 0, '
        "-eq COUNT) breaks the 'tolerate ~40% loss before alarm' "
        "semantic the FYS investigation pinned the constants to."
    )


def test_main_branch_calls_ping_burst_ok_not_raw_ping() -> None:
    """The main `elif` arm must call `ping_burst_ok` — NOT raw
    `ping -c 1` (the pre-2026-05-24 single-ping shape we're moving
    away from). Pins the call-site against a refactor that defines
    the function but forgets to switch the dispatch over."""
    source = _read_script_source()
    assert re.search(
        r'^\s*elif\s+ping_burst_ok\s+"\$gw"\s*;\s*then\s*$',
        source,
        flags=re.MULTILINE,
    ), (
        'main control flow must `elif ping_burst_ok "$gw"; then` '
        "— the burst-check function exists but the dispatch is "
        "still hitting raw `ping -c 1`."
    )
    # Anti-pattern: the only `ping -c 1` left in the source should
    # be NONE (the burst check uses -c "$PING_BURST_COUNT"). Catches
    # the case where someone leaves the old call site in.
    assert "ping -c 1 -W 2" not in source, (
        "found a `ping -c 1 -W 2` literal — that was the pre-burst "
        "single-ping shape. Anti-pattern: it would silently re-enable "
        "the false-positive cadence the burst check exists to fix."
    )


def test_degraded_log_message_includes_fraction() -> None:
    """The degraded-link note() message must include the X/N received
    fraction (`($PING_RECEIVED/$PING_BURST_COUNT received)`). This is
    the most useful piece of forensic data the watchdog emits — a
    raw 'ping failed' line forces an SSH-and-re-probe to figure out
    how bad the link is. The fraction lets a journal-grep tell the
    difference between 0/5 (true outage) and 2/5 (degraded but
    still mostly working). FYS wedge-investigation cost ~5 min of
    re-probing because the prior log line was opaque."""
    source = _read_script_source()
    assert "($PING_RECEIVED/$PING_BURST_COUNT received)" in source, (
        "degraded-link note() must include the (X/N received) "
        "fraction for forensics — see test docstring for why."
    )


# ---- 2026-05-24: modprobe escalation tier (Path 2) ----


def test_modprobe_threshold_constant_present() -> None:
    """MODPROBE_AFTER_N_RESTARTS=3 — pins the tier between
    REBOOT_AFTER_N_RESTARTS=5 (reboot) and the THRESHOLD=2 ping-burst
    fail count that triggers an NM-restart. Order matters: NM-restart
    must come first (cheapest), then modprobe-cycle (medium —
    resets just the wifi chip, leaves renderer + backend running),
    then reboot (most disruptive). A refactor that flattens the
    tiers loses the recovery hierarchy."""
    source = _read_script_source()
    assert re.search(
        r"^\s*MODPROBE_AFTER_N_RESTARTS=3\s*$",
        source,
        flags=re.MULTILINE,
    ), (
        "MODPROBE_AFTER_N_RESTARTS must be 3 — pinned between "
        "THRESHOLD=2 (NM-restart) and REBOOT_AFTER_N_RESTARTS=5 "
        "(reboot)."
    )


def test_modprobe_ledger_path_constant_present() -> None:
    """MODPROBE_LEDGER=/var/run/wifi-watchdog.modprobe — must be on
    tmpfs (matches RESTARTS_FILE convention) so a reboot wipes it
    by side-effect, preventing modprobe-cycle ledgers from carrying
    across boot transitions."""
    source = _read_script_source()
    assert re.search(
        r"^\s*MODPROBE_LEDGER=/var/run/wifi-watchdog\.modprobe\s*$",
        source,
        flags=re.MULTILINE,
    ), (
        "MODPROBE_LEDGER must be /var/run/wifi-watchdog.modprobe — "
        "/var/run is tmpfs on the Pi so reboot wipes the ledger "
        "and the next post-reboot escalation starts fresh."
    )


def test_try_modprobe_cycle_function_present() -> None:
    """`try_modprobe_cycle` must exist + do exactly `modprobe -r
    brcmfmac` + 1-second sleep + `modprobe brcmfmac`, returning
    success only if BOTH commands succeed. The sleep gives the
    kernel time to release wifi sysfs entries before re-probe —
    without it the modprobe race-attaches against in-cleanup state.

    `modprobe -r` (not bare `rmmod`) is load-bearing — see
    test_unload_uses_modprobe_r_not_rmmod for the wcc-sub-module
    rationale."""
    source = _read_script_source()
    match = re.search(
        r"try_modprobe_cycle\(\)\s*\{(.*?)\n\}",
        source,
        flags=re.DOTALL,
    )
    assert match, "try_modprobe_cycle() function not found"
    body = match.group(1)
    assert "modprobe -r brcmfmac" in body, (
        "try_modprobe_cycle must call `modprobe -r brcmfmac` — the "
        "cycle requires unloading the module (with reverse-deps "
        "handled, see test_unload_uses_modprobe_r_not_rmmod) before "
        "re-probing."
    )
    assert "sleep 1" in body, (
        "try_modprobe_cycle must `sleep 1` between unload + re-probe "
        "to let kernel release wifi sysfs entries before re-probe; "
        "without it, modprobe race-attaches against in-cleanup state."
    )
    # `modprobe brcmfmac` (the re-load) must appear distinct from
    # the `modprobe -r brcmfmac` (the unload). Match it bare —
    # not preceded by `-r ` — to avoid false positives.
    assert re.search(r"(?<!-r )modprobe brcmfmac\b", body), (
        "try_modprobe_cycle must call `modprobe brcmfmac` (without "
        "-r) to re-load the driver — that's the entire point of "
        "the cycle."
    )


def test_unload_uses_modprobe_r_not_rmmod() -> None:
    """The unload step MUST use `modprobe -r` (which handles
    reverse-deps) — NOT bare `rmmod` (which does not).

    Live-fire on FYS 2026-05-24 13:21:47 proved this necessary:
    `rmmod brcmfmac` failed because brcmfmac_wcc is loaded ON TOP
    of brcmfmac (chip-specific extension for the BCM43430-W
    variant). The kernel refuses: "Module brcmfmac is in use by:
    brcmfmac_wcc". `modprobe -r` walks the rdep graph and unloads
    brcmfmac_wcc first.

    Anti-pattern: `rmmod brcmfmac` anywhere in the script. A
    future refactor that "simplifies" back to bare rmmod would
    silently reintroduce the wcc-failure mode on every install
    that loads a brcmfmac_* sub-module. Catches that regression
    structurally."""
    source = _read_script_source()
    # Bare `rmmod brcmfmac` (with no -r prefix on the verb)
    # must be ABSENT from the script.
    assert not re.search(
        r"(?<!modprobe -)\brmmod brcmfmac\b",
        source,
    ), (
        "found bare `rmmod brcmfmac` — this fails on Pis where "
        "brcmfmac_wcc (or any other brcmfmac_* sub-module) is "
        "loaded on top, because rmmod doesn't handle reverse-deps. "
        "Use `modprobe -r brcmfmac` which walks the rdep graph and "
        "unloads sub-modules first."
    )
    # Positive: `modprobe -r brcmfmac` must be present somewhere.
    assert "modprobe -r brcmfmac" in source, (
        "unload step must use `modprobe -r brcmfmac` — see test "
        "docstring for the wcc-sub-module rationale."
    )


def test_dual_mode_known_limitation_documented() -> None:
    """Path 2's modprobe-cycle is a no-op on FYS and on any other
    dual-mode (STA + AP) install because `ap0` (the captive-portal
    AP virtual interface) holds a reference to brcmfmac that a
    simple modprobe-cycle can't release. Live-fire on FYS
    2026-05-24 14:00 confirmed; qarl decided to accept it as a
    known no-op rather than extend the function to tear down
    hostapd + ap0 (approaches reboot cost without reboot's clean
    state).

    Pin the documentation so a future Jimmy doesn't try to "fix"
    the no-op tier on FYS without understanding why it's
    intentionally a no-op. If the rationale comment gets stripped,
    this test fires — operator should read the surrounding
    comment block + dispatch QA before extending the function."""
    # _read_script_source() strips `#` comments so the rationale
    # docstring wouldn't survive — read the raw file for this test.
    source = _SCRIPT.read_text(encoding="utf-8")
    # Three key phrases that together encode the intent: the
    # limitation label, the dual-mode framing, and the explicit
    # don't-extend warning. All three must survive any future
    # cleanup of the function docstring.
    assert "KNOWN LIMITATION" in source, (
        "dual-mode limitation label missing from try_modprobe_cycle "
        "docstring — future Jimmy may not see the warning before "
        "extending the function. See test docstring for context."
    )
    assert "dual-mode" in source.lower() or "ap0" in source.lower(), (
        "dual-mode (STA + AP) framing missing — without it, the no-op-on-FYS rationale is unclear."
    )
    assert "hostapd" in source.lower(), (
        "hostapd dependency on brcmfmac (via ap0) not mentioned "
        "in the script — that's the actual ref-holder blocking "
        "modprobe -r on dual-mode setups."
    )


def test_power_save_warning_check_present() -> None:
    """Tier-zero observability check (2026-05-24): the watchdog must
    query `iw dev wlan0 get power_save` at tick start and log a
    WARN if it reports ON. Defense against a future NM-conf clobber
    that silently re-enables power-save — without this fence, a
    regression would require operators to re-discover the FYS
    2026-05-24 11:00 finding (PS-on creates burst-loss patterns
    that mimic genuine RF degradation) from scratch.

    Pin the iw query + the WARN-style note() call so a future
    "clean up the unused iw call" refactor surfaces this test
    before quietly removing the defense."""
    source = _read_script_source()
    # The iw query must be present + must extract power_save state.
    assert re.search(
        r"iw\s+dev\s+wlan0\s+get\s+power_save",
        source,
    ), (
        "watchdog must query `iw dev wlan0 get power_save` at tick "
        "start to surface a silent NM-conf clobber re-enabling PS. "
        "See test docstring for the discovery rationale."
    )
    # The WARN note() must fire when state == "on".
    assert re.search(
        r'\[\s*"\$ps_state"\s*=\s*"on"\s*\]',
        source,
    ), (
        'watchdog must check `[ "$ps_state" = "on" ]` and '
        "note() a WARN — anything else (always-log, only-on-fails, "
        "etc.) misses the silent-clobber detection."
    )
    # The note() must mention "power_save" so journalctl-grep finds it.
    assert re.search(
        r'note\s+["\'].*power_save\s+is\s+ON',
        source,
    ), (
        "PS-warning note() must include `power_save is ON` so "
        "operators searching `journalctl -t wifi-watchdog | grep "
        "power_save` find it during a future investigation."
    )


def test_modprobe_done_in_window_function_present() -> None:
    """`modprobe_done_in_window` gates the cycle to at most once per
    REBOOT_WINDOW_SECONDS — without it we'd retry modprobe on every
    cron firing once the threshold is crossed, which would either
    burn the cron budget on rmmod/modprobe or thrash the chip. Must
    use the same `[ ts -ge cutoff ]` keep-criterion pattern as the
    main ledger prune so an NTP backward-jump doesn't produce
    surprising negative-delta behavior."""
    source = _read_script_source()
    match = re.search(
        r"modprobe_done_in_window\(\)\s*\{(.*?)\n\}",
        source,
        flags=re.DOTALL,
    )
    assert match, "modprobe_done_in_window() function not found"
    body = match.group(1)
    assert re.search(
        r'\[\s*"\$ts"\s*-ge\s*"\$cutoff"\s*\]',
        body,
    ), (
        'modprobe_done_in_window must gate on `[ "$ts" -ge '
        '"$cutoff" ]` keep-criterion — same shape as the main '
        "ledger prune, NTP-backward-jump safe."
    )


def test_modprobe_tier_fires_below_reboot_threshold() -> None:
    """The modprobe-tier branch in record_nm_restart_and_maybe_reboot
    must check `$count -ge $MODPROBE_AFTER_N_RESTARTS` AND that
    `modprobe_done_in_window` returns false. Both gates must be
    present — without the "done in window" gate, we'd fire modprobe
    on every cron firing past the threshold. Without the "count >="
    gate, modprobe would fire on EVERY NM-restart (which is too
    eager: NM-restart alone fixes most cases)."""
    source = _read_script_source()
    # Match the modprobe-tier conditional anywhere in
    # record_nm_restart_and_maybe_reboot's body.
    assert re.search(
        r'\[\s*"\$count"\s*-ge\s*"\$MODPROBE_AFTER_N_RESTARTS"\s*\]'
        r"\s*&&\s*!\s*modprobe_done_in_window",
        source,
    ), (
        'modprobe-tier branch must gate on BOTH `[ "$count" -ge '
        '"$MODPROBE_AFTER_N_RESTARTS" ]` AND '
        "`! modprobe_done_in_window` — see test docstring for the "
        "consequence of dropping either gate."
    )


def test_modprobe_tier_fires_before_reboot_in_control_flow() -> None:
    """The modprobe branch must come AFTER the reboot-threshold
    check in record_nm_restart_and_maybe_reboot — otherwise a count
    of 5+ would fire modprobe + then reboot, when the intent is to
    skip modprobe entirely if we're already at reboot threshold (the
    chip needs more than a re-init). The reboot's `return` ensures
    the modprobe branch is unreachable when count >= 5."""
    source = _read_script_source()
    match = re.search(
        r"record_nm_restart_and_maybe_reboot\(\)\s*\{(.*?)\n\}",
        source,
        flags=re.DOTALL,
    )
    assert match, "record_nm_restart_and_maybe_reboot() not found"
    body = match.group(1)
    reboot_pos = body.find('"$REBOOT_AFTER_N_RESTARTS"')
    modprobe_pos = body.find('"$MODPROBE_AFTER_N_RESTARTS"')
    assert reboot_pos != -1, "reboot threshold check missing"
    assert modprobe_pos != -1, "modprobe threshold check missing"
    assert reboot_pos < modprobe_pos, (
        "REBOOT check must precede MODPROBE check in the body — "
        "if a count of 5+ hits the modprobe branch first, the "
        "intent (skip modprobe when chip needs kernel-level reset) "
        "is violated."
    )
    # And the reboot branch must `return` after `systemctl reboot`
    # so the modprobe branch below is unreachable when count >=
    # REBOOT_AFTER_N_RESTARTS. Look at the slice between the reboot
    # check and the modprobe check (the reboot if-block sits there).
    reboot_block = body[reboot_pos:modprobe_pos]
    assert "return" in reboot_block, (
        "reboot block must `return` after `systemctl reboot` so "
        "the modprobe branch below is unreachable; otherwise a "
        "5-restart-count fires BOTH reboot AND modprobe."
    )


def test_modprobe_ledger_recorded_unconditionally() -> None:
    """The `echo "$now" >> "$MODPROBE_LEDGER"` MUST run regardless
    of whether `try_modprobe_cycle` succeeded or failed — otherwise
    a persistent kernel-busy "Module in use" failure would loop
    forever (cron fires every 30s, ledger stays empty so the gate
    keeps opening, modprobe keeps failing, no progress toward the
    reboot tier that would actually recover). The ledger append
    being OUTSIDE the if/else of the success branch is the safety
    fence."""
    source = _read_script_source()
    match = re.search(
        r"record_nm_restart_and_maybe_reboot\(\)\s*\{(.*?)\n\}",
        source,
        flags=re.DOTALL,
    )
    assert match
    body = match.group(1)
    # Ledger append must exist + structurally land AFTER the
    # try_modprobe_cycle's if/else block (i.e. unconditional from
    # success/failure perspective). Easiest robust check: find
    # both anchors in source order.
    ledger_append_pos = body.find('echo "$now" >> "$MODPROBE_LEDGER"')
    assert ledger_append_pos != -1, (
        'modprobe-tier must contain `echo "$now" >> "$MODPROBE_LEDGER"`'
        " to fence the no-retry-loop invariant — see test docstring."
    )
    try_cycle_pos = body.find("try_modprobe_cycle")
    assert try_cycle_pos != -1 and try_cycle_pos < ledger_append_pos, (
        "ledger append must follow `try_modprobe_cycle` invocation in source order."
    )
    # And the ledger append must NOT be inside the `then` arm —
    # check by ensuring no `then` keyword appears between the LAST
    # `try_modprobe_cycle` text and the ledger append (which would
    # indicate the append is inside a success-only branch).
    between = body[try_cycle_pos:ledger_append_pos]
    # The expected shape has exactly one `then` (the
    # `if try_modprobe_cycle; then`) and one `else` and one `fi`
    # in `between`. If a `then` appears after the `fi`, the append
    # is in a different branch.
    last_fi = between.rfind("\n        fi\n")
    after_fi = between[last_fi + 1 :] if last_fi != -1 else between
    assert "then" not in after_fi, (
        "ledger append appears AFTER an unclosed `then` — it's "
        "inside a success-only branch. Loop hazard: a persistent "
        "kernel-busy modprobe failure never records to the ledger, "
        "so the cycle retries forever."
    )


def test_modprobe_ledger_wiped_before_reboot() -> None:
    """When the reboot tier fires, BOTH the restarts ledger AND the
    modprobe ledger must be wiped — symmetric with RESTARTS_FILE
    (anti-reboot-loop). If MODPROBE_LEDGER persists across a
    reboot-tier escalation (impossible on tmpfs but defensive),
    a post-reboot watchdog would see a stale modprobe entry and
    skip the modprobe tier in the next window."""
    source = _read_script_source()
    match = re.search(
        r"record_nm_restart_and_maybe_reboot\(\)\s*\{(.*?)\n\}",
        source,
        flags=re.DOTALL,
    )
    assert match
    body = match.group(1)
    # Find the reboot block (between the threshold check + systemctl reboot)
    assert re.search(
        r':\s*>\s*"\$MODPROBE_LEDGER"',
        body,
    ), (
        "reboot tier must wipe MODPROBE_LEDGER with `: > "
        '"$MODPROBE_LEDGER"` symmetric to RESTARTS_FILE wipe. '
        "Defensive: tmpfs clears it on reboot, but explicit wipe "
        "covers the edge case where /var/run is ever moved to a "
        "persistent fs."
    )


# ---- 2026-05-24: flock wrap (concurrent-cron-race fix) ----


def test_cron_uses_flock_n_nonblocking_lock() -> None:
    """Both cron lines must wrap the script in `flock -n
    /var/lock/wifi-watchdog.lock`.

    Why `-n` (non-blocking): we deliberately want concurrent cron
    firings to silently skip rather than queue. Without `-n`, the
    DEFAULT behavior is BLOCKING — if a slow NM-restart or
    modprobe-cycle holds the lock for 20s, the next cron firing
    queues behind it instead of being dropped, then both run
    back-to-back when the lock releases. That's the exact bug
    shape (back-to-back-NM-restart bursts) we observed live on
    FYS 11:00 — `flock` without `-n` would NOT fix it.

    Why /var/lock specifically: it's a tmpfs symlink to /run/lock
    on Debian, so the lock file does not survive a reboot — a
    stale lock from a wedged-then-rebooted invocation is impossible.

    Pin both the `-n` flag AND the specific lock path so a
    refactor (e.g. someone replacing `-n` with `-w 5` for a wait,
    or moving the lock to a persistent fs) trips this test."""
    cron = _read_cron_source()
    # Each script invocation line must be wrapped in flock.
    flock_pattern = re.compile(
        r"/usr/bin/flock\s+-n\s+/var/lock/wifi-watchdog\.lock\s+"
        r"/usr/local/bin/wifi-watchdog\.sh",
    )
    matches = flock_pattern.findall(cron)
    assert len(matches) == 2, (
        f"expected 2 flock-wrapped cron lines, found {len(matches)}. "
        "BOTH the at-:00 and at-:30 fire lines must be flock-wrapped — "
        "otherwise concurrent invocations race the shared ledger files "
        "and produce duplicate escalation events."
    )
    # And NO unwrapped invocation of the script (would be a regression).
    unwrapped = re.search(
        r"^\s*\*\s+\*\s+\*\s+\*\s+\*\s+root\s+"
        r"(?:sleep\s+30\s*&&\s*)?"
        r"/usr/local/bin/wifi-watchdog\.sh\s*$",
        cron,
        flags=re.MULTILINE,
    )
    assert not unwrapped, (
        "found an unwrapped `/usr/local/bin/wifi-watchdog.sh` "
        "cron invocation — every invocation must go through "
        "flock to avoid the concurrent-cron race that produced "
        "the back-to-back-NM-restart bursts observed on FYS."
    )
