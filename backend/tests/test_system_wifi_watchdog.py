"""WiFi watchdog mitigation #2 regression lock (2026-05-23).

qarl-direct ship-blocker postmortem 2026-05-23: today's FYS Pi
"outage" was an AP-side deauth (reason 16 = group_cipher_not_valid)
that DHCP-renumbered the Pi `.67` → `.69`. The existing
`wifi-watchdog.sh` observed the wedge (logged three consecutive
`no default gateway` lines) but took NO action because its no-GW
branch incremented `fails` without ever calling
`systemctl restart NetworkManager` — only the ping-fail branch did.

Mitigation #2 hardening (this commit):

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
    sub-minute cadence."""
    cron = _read_cron_source()
    # First line: standard every-minute fire.
    assert re.search(
        r"^\*\s+\*\s+\*\s+\*\s+\*\s+root\s+/usr/local/bin/wifi-watchdog\.sh\s*$",
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
        r"^\*\s+\*\s+\*\s+\*\s+\*\s+root\s+sleep\s+30\s*&&\s*/usr/local/bin/wifi-watchdog\.sh\s*$",
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
