"""Pi shell-script timestamp UTC contract (postmortem #8, 2026-05-24).

A wedge-investigation 2026-05-23 cost ~20 min reconciling timestamps
across two emitters with different timezones:
- systemd journal (`journalctl`) emits UTC.
- wifi-watchdog.sh wrote local time via `date -Iseconds` (no `-u`).

Local was BST (`+01:00`); a 16:42 BST entry in the watchdog log
correlated to a 15:42 UTC entry in `journalctl`. ~20 min of mental
hour-arithmetic per forensic pass; the postmortem flagged it as
item #8.

Fix: standardize ALL log-timestamp emitters on UTC via `date -u`
(or `date --utc` — both POSIX-portable). This test fences the
six sites identified in the postmortem + auto-catches any future
shell script that adds a `date -Iseconds` log emitter without the
UTC flag.

Out of scope (per the schedule.py:22-28 timezone contract):
- `timedatectl set-timezone UTC` on the Pi system clock. The
  playback schedule evaluator (`backend/openmarquee/schedule.py`)
  takes a NAIVE `datetime` and assumes the device clock is in the
  OPERATOR'S LOCAL timezone. Flipping system TZ to UTC silently
  shifts every operator-configured window like `11:00-14:00` by
  the local-TZ offset. A proper system-wide-UTC fix would require
  the schedule's reserved `Schedule.tz: str | None` field + zoned
  rule evaluation; cross-cutting refactor.

A future "helpful" change could break the contract silently:
- Dropping the `-u` from any of the 6 specific call sites
  reintroduces the TZ-drift forensics burden.
- Adding a NEW `date -Iseconds` invocation in a watched shell
  script without `-u` triggers the auto-catch (the directory
  sweep). Add `-u` to fix, or namespace the call as not a log
  emitter (rare: filename construction can stay local; the
  forensics burden only applies to LOG output).

Static parse only — pytest doesn't invoke real shell scripts on
the Pi. Same shape as the D2 / M5 / H4 / font-load / Bug 2 /
Bug 3 closures.
"""

from __future__ import annotations

import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPTS_DIR = _PROJECT_ROOT / "scripts"
_SYSTEM_DIR = _PROJECT_ROOT / "system"

# The six sites identified in the postmortem-#8 surface report.
# Each entry: (relative_path, anchor_substring_that_must_be_present_after_fix).
# The anchor is the LITERAL substring including the `-u` flag — if a
# future edit drops `-u`, the substring won't match → test fails.
_FIXED_SITES = (
    ("scripts/wifi-watchdog.sh", "ts() { date -u -Iseconds; }"),
    ("scripts/wifi-preemptive-reload.sh", "ts() { date -u -Iseconds; }"),
    ("scripts/install.sh", "date -u -Iseconds 2>/dev/null || date -u"),
    ("scripts/renderer_pi_soak_ipc.sh", "$(date -u -Iseconds)"),
    (
        "system/hostapd.service.d/log-to-file.conf",
        '"$(date -u -Iseconds)"',
    ),
)

# Directories swept for the AUTO-CATCH check below. Any `date ... -Iseconds`
# in any .sh / .conf / .service / .unit / .source file under these paths
# is asserted to include `-u` or `--utc` in its args.
_SWEPT_DIRS = (_SCRIPTS_DIR, _SYSTEM_DIR)
_SWEPT_SUFFIXES = (".sh", ".conf", ".service", ".unit", ".source")

# Matches a `date` invocation followed by `-Iseconds`, capturing
# everything between (the args). We then check the args contain
# `-u` or `--utc` as a standalone flag.
#
# Pattern reasoning:
# - `\bdate\b` — the `date` command word (word-bounded so it doesn't
#   match `update`, `_date_`, etc.)
# - `([^|;)\n]*)` — args up to the next pipe / semicolon / paren
#   close / newline. Captures the arg run for the UTC-flag check.
# - `-Iseconds\b` — the ISO-seconds output flag (the format used
#   by the postmortem's watchdog log line).
_DATE_ISECONDS_RE = re.compile(r"\bdate\b([^|;)\n]*)-Iseconds\b")

# Matches the UTC-forcing flag as a standalone token. Both POSIX
# forms accepted:
# - `-u` (short)
# - `--utc` (long)
# Word-boundaries so we don't match the literal `-u` inside another
# flag like `--user`.
_UTC_FLAG_RE = re.compile(r"(?:^|\s)(-u|--utc)(?=\s|$)")


def test_fixed_sites_all_emit_utc_timestamps() -> None:
    """The six sites from postmortem #8 must each carry the literal
    `-u` flag on their `date -Iseconds` invocation. If any future
    edit drops `-u`, the anchor substring no longer matches and
    this test fails with a pointer to the regressed file."""
    failures: list[str] = []
    for relpath, anchor in _FIXED_SITES:
        path = _PROJECT_ROOT / relpath
        assert path.exists(), f"Watched file missing: {relpath}"
        content = path.read_text(encoding="utf-8")
        if anchor not in content:
            failures.append(f"{relpath}: missing anchor {anchor!r}")
    assert not failures, (
        "One or more postmortem-#8 sites regressed (UTC `-u` flag "
        "dropped from `date -Iseconds` call):\n  " + "\n  ".join(failures)
    )


def test_no_bare_date_iseconds_in_watched_dirs() -> None:
    """Auto-catch: any `date ... -Iseconds` invocation under
    `scripts/` or `system/` (in .sh / .conf / .service / .unit /
    .source files) must include the UTC-forcing flag (`-u` or
    `--utc`). Adding a new log emitter without the flag re-opens
    the cross-emitter TZ-correlation forensics burden documented
    in postmortem #8."""
    violations: list[str] = []
    for sweep_dir in _SWEPT_DIRS:
        if not sweep_dir.exists():
            continue
        for path in sweep_dir.rglob("*"):
            if not path.is_file() or path.suffix not in _SWEPT_SUFFIXES:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for match in _DATE_ISECONDS_RE.finditer(content):
                args = match.group(1)
                if not _UTC_FLAG_RE.search(args):
                    relpath = path.relative_to(_PROJECT_ROOT)
                    line_no = content[: match.start()].count("\n") + 1
                    snippet = match.group(0).strip()
                    violations.append(
                        f"{relpath}:{line_no}: {snippet!r} — add `-u` or `--utc` before `-Iseconds`"
                    )
    assert not violations, (
        "Bare `date -Iseconds` (without `-u`/`--utc`) found in "
        "watched directories — every log-emitter timestamp must be "
        "UTC for journalctl-correlation per postmortem #8:\n  " + "\n  ".join(violations)
    )


def test_utc_flag_regex_accepts_both_short_and_long_forms() -> None:
    """Self-test the auto-catch regex: ensure it accepts the two
    POSIX UTC-forcing forms (`-u` and `--utc`) and rejects
    look-alikes (`--user`, `-update`)."""
    # Positive cases — these must MATCH (no false negatives).
    for accepted in (" -u ", "\t-u\n", " --utc ", "\n--utc\t"):
        assert _UTC_FLAG_RE.search(accepted), f"Should accept: {accepted!r}"
    # Negative cases — these must NOT match (no false positives).
    for rejected in (" --user ", " -update ", "-utc", "u "):
        assert not _UTC_FLAG_RE.search(rejected), f"Should reject: {rejected!r}"


def test_date_iseconds_regex_catches_typical_invocations() -> None:
    """Self-test the auto-catch regex: ensure it finds `date
    -Iseconds` in the common shell forms used across the watched
    sites."""
    # Each of these patterns appears (post-fix) in the real files.
    samples = (
        "ts() { date -u -Iseconds; }",
        '"$(date -u -Iseconds)"',
        '"$(date -u -Iseconds 2>/dev/null || date -u)"',
    )
    for sample in samples:
        match = _DATE_ISECONDS_RE.search(sample)
        assert match is not None, f"Regex missed: {sample!r}"
        assert _UTC_FLAG_RE.search(match.group(1)), (
            f"Sample has -u but regex captured args without it: "
            f"{sample!r} → args={match.group(1)!r}"
        )
