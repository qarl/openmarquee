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
   REBOOT_WINDOW_SECONDS (default 600s); if the remaining count
   meets REBOOT_AFTER_N_RESTARTS (default 3), the script issues
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
        f"wifi-watchdog.sh not found at {_SCRIPT}; relocation? "
        f"Update the test path."
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
        "could not locate the `if [ -z \"$gw\" ]; then ... elif` "
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
        r'elif ping[^\n]*then.*?\belse\b(.*?)\bfi\b\s*$',
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
        r'export\s+PATH=/usr/sbin:/usr/bin:/sbin:/bin',
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
    assert not re.search(r'set\s+-[eu]*e[u]*o\s+pipefail', source), (
        "`set -euo pipefail` (or any combination including -e or "
        "pipefail) is present — under cron a non-zero exit from "
        "`ip route show` or a similar query would abort the "
        "watchdog silently. Use explicit per-command exit checks "
        "instead."
    )
    # Confirm `set -u` (unset-var safety) IS kept — catches
    # typos at runtime without killing on harmless command
    # failures.
    assert re.search(r'^\s*set\s+-u\s*$', source, flags=re.MULTILINE), (
        "`set -u` removed — unset-variable safety lost. Keep it "
        "(catches refactor typos like `$STATE_FIL` writing to /)."
    )


def test_cron_entry_has_30s_cadence() -> None:
    """Cron file must fire the watchdog twice per minute (once at
    :00, once at :30 via `sleep 30 &&`). Cron's smallest native
    unit is 1 min, so the double-line is the standard idiom for
    sub-minute cadence."""
    cron = _read_cron_source()
    # First line: standard every-minute fire.
    assert re.search(
        r'^\*\s+\*\s+\*\s+\*\s+\*\s+root\s+/usr/local/bin/wifi-watchdog\.sh\s*$',
        cron,
        flags=re.MULTILINE,
    ), (
        "cron file missing the at-:00 fire line "
        "(`* * * * * root /usr/local/bin/wifi-watchdog.sh`) — "
        "regression to 1-min cadence."
    )
    # Second line: offset-by-30s fire. Match `sleep 30 &&` followed
    # by the script invocation; whitespace flexible.
    assert re.search(
        r'^\*\s+\*\s+\*\s+\*\s+\*\s+root\s+sleep\s+30\s*&&\s*/usr/local/bin/wifi-watchdog\.sh\s*$',
        cron,
        flags=re.MULTILINE,
    ), (
        "cron file missing the at-:30 fire line "
        "(`* * * * * root sleep 30 && /usr/local/bin/wifi-"
        "watchdog.sh`) — without it the cadence is 1 min, not 30s, "
        "and detection floor doubles to ~120s."
    )


def test_cron_entry_has_path_preamble() -> None:
    """Cron's default PATH lacks /usr/sbin — without explicit PATH
    the systemctl call in the script can fail silently. The
    script ALSO sets PATH but the cron-side line is belt-and-
    suspenders + matches the sibling `wifi-preemptive-reload`
    cron deploy pattern."""
    cron = _read_cron_source()
    assert re.search(
        r'^PATH=/usr/sbin:/usr/bin:/sbin:/bin\s*$',
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
        r'^\s*RESTARTS_FILE=/var/run/wifi-watchdog\.restarts\s*$',
        source,
        flags=re.MULTILINE,
    ), (
        "RESTARTS_FILE constant must point at /var/run/wifi-"
        "watchdog.restarts. A path off /var/run (a persistent fs) "
        "would break the tmpfs auto-wipe-on-boot anti-reboot-loop "
        "safety property."
    )


def test_reboot_threshold_is_three() -> None:
    """REBOOT_AFTER_N_RESTARTS == 3 per the postmortem. A bump
    higher delays recovery from a real firmware wedge; a bump lower
    risks rebooting on a single flap."""
    source = _read_script_source()
    assert re.search(
        r'^\s*REBOOT_AFTER_N_RESTARTS=3\s*$',
        source,
        flags=re.MULTILINE,
    ), (
        "REBOOT_AFTER_N_RESTARTS must be 3 (the postmortem value). "
        "Higher delays brcmfmac wedge recovery; lower risks "
        "rebooting on a single transient flap."
    )


def test_reboot_window_is_600s() -> None:
    """REBOOT_WINDOW_SECONDS == 600 per the postmortem (10 min).
    Combined with THRESHOLD=2 × 30s cadence × 3 restarts, this
    spans ~6 min of real activity — enough to confirm a sustained
    wedge, fast enough to recover before customer-facing impact."""
    source = _read_script_source()
    assert re.search(
        r'^\s*REBOOT_WINDOW_SECONDS=600\s*$',
        source,
        flags=re.MULTILINE,
    ), (
        "REBOOT_WINDOW_SECONDS must be 600 (the postmortem value). "
        "A larger window means an old restart from before a healthy "
        "spell could still trip the reboot threshold; a smaller "
        "window means a genuine wedge might not accumulate enough "
        "restart events to trigger recovery."
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
    matches = re.findall(r'\bsystemctl\s+reboot\b', source)
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
        r'record_nm_restart_and_maybe_reboot\s*\(\s*\)\s*\{(.*?)\n\}',
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
        "helper missing `[ \"$count\" -ge \"$REBOOT_AFTER_N_RESTARTS\" ]` "
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
        r'record_nm_restart_and_maybe_reboot\s*\(\s*\)\s*\{(.*?)\n\}',
        source,
        flags=re.DOTALL,
    )
    assert helper, (
        "record_nm_restart_and_maybe_reboot() helper not found — see "
        "test_reboot_is_count_gated for the same diagnostic."
    )
    body = helper.group(1)
    wipe = re.search(r':\s*>\s*"\$RESTARTS_FILE"', body)
    reboot = re.search(r'systemctl\s+reboot', body)
    assert wipe, (
        "ledger wipe (`: > \"$RESTARTS_FILE\"`) missing from the "
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
        r'record_nm_restart_and_maybe_reboot\s*\(\s*\)\s*\{(.*?)\n\}',
        source,
        flags=re.DOTALL,
    )
    assert helper, "record_nm_restart_and_maybe_reboot() helper missing"
    body = helper.group(1)
    assert re.search(
        r'\[\s*"\$ts_line"\s*-ge\s*"\$cutoff"\s*\]',
        body,
    ), (
        "ledger prune doesn't use `[ \"$ts_line\" -ge \"$cutoff\" ]` "
        "keep-criterion. A delta-math refactor (`now - ts < window`) "
        "behaves surprisingly under NTP backward-jumps on an "
        "RTC-less Pi — future-dated entries produce negative deltas."
    )
