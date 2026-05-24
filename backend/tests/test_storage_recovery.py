"""Unit tests for the shared corrupt-file quarantine helper.

The wiring path PlaylistStorage.load() -> JSONDecodeError ->
quarantine_corrupt_file -> defaults bootstrap is exercised as an
INTEGRATION test at test_playlist.py::test_invalid_json_is_quarantined_
and_starts_fresh. These tests cover the helper in ISOLATION so the
contract surface (return value, log shape, filename format, error
handling) is pinned without needing to drive a storage class through
its full load path.

QA backend test-coverage gap audit 2026-05-24: _storage_recovery was
the single GAP module in the audit (1 GAP / 2 THIN / 34 GOOD / 8 DEEP);
this file closes it.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openmarquee._storage_recovery import quarantine_corrupt_file


def test_quarantine_renames_corrupt_file_to_timestamped_sibling(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    """Happy path: a corrupt file is renamed to a `.corrupt-<UTC>`
    sibling; the returned Path points at the new location; the source
    path no longer exists."""
    source = tmp_path / "playlist.json"
    source.write_text("this is not JSON")
    parse_exc = ValueError("Expecting value: line 1 column 1 (char 0)")

    with caplog.at_level(logging.WARNING, logger="openmarquee._storage_recovery"):
        result = quarantine_corrupt_file(source, parse_exc)

    assert result is not None, "happy path must return the quarantine Path"
    assert result.exists(), "the renamed file must exist at the returned path"
    assert not source.exists(), "the source path must be gone after rename"
    assert result.parent == source.parent, "quarantine sibling lives next to source"


def test_quarantine_returns_none_when_source_missing(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    """If the source path doesn't exist, the helper short-circuits and
    returns None without logging — callers may call this defensively
    after a separate failure and we don't want to spam the journal."""
    missing = tmp_path / "does-not-exist.json"
    parse_exc = ValueError("oops")

    with caplog.at_level(logging.DEBUG, logger="openmarquee._storage_recovery"):
        result = quarantine_corrupt_file(missing, parse_exc)

    assert result is None
    assert len(caplog.records) == 0, (
        f"missing-source path must not log; got: {[r.message for r in caplog.records]}"
    )


def test_quarantine_returns_none_when_rename_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """When os.rename fails (read-only fs, permission denied, parent
    dir gone mid-call), the helper must log at ERROR via log.exception
    and return None. The caller's "return defaults" path then still
    completes — better to bring the service up against fresh defaults
    than leave it hard-crashing on every restart."""
    source = tmp_path / "playlist.json"
    source.write_text("garbage")
    parse_exc = ValueError("parse fail")

    def raise_oserror(self, target):
        raise OSError("simulated read-only filesystem")

    monkeypatch.setattr(Path, "rename", raise_oserror)

    with caplog.at_level(logging.ERROR, logger="openmarquee._storage_recovery"):
        result = quarantine_corrupt_file(source, parse_exc)

    assert result is None, "rename-failure path must return None"
    # log.exception emits at ERROR level with exc_info attached.
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "rename failure must log at ERROR level"
    assert any("quarantine-rename to" in r.message for r in error_records), (
        f"error log must reference the failed quarantine target; got: "
        f"{[r.message for r in error_records]}"
    )


def test_quarantine_filename_matches_iso_utc_z_pattern(tmp_path: Path):
    """Filename shape contract: the quarantine suffix is
    `.corrupt-<UTC-ISO-second-with-Z>`. UTC-Z is what makes multiple
    quarantine events sortable + locally unambiguous (no `+HH:MM`
    offset surprises across hosts). Second granularity is the
    documented limit; same-second double-quarantine on the same path
    is acknowledged-not-handled (would silently overwrite per POSIX
    rename semantics)."""
    source = tmp_path / "settings.json"
    source.write_text("not json")

    result = quarantine_corrupt_file(source, ValueError("x"))

    assert result is not None
    pattern = re.compile(r"\.corrupt-\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    assert pattern.search(result.name), (
        f"quarantine filename {result.name!r} does not match `<name>.corrupt-<UTC-ISO-Z>` pattern"
    )


def test_quarantine_warns_with_path_and_exception_on_success(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    """The success-path log must include BOTH the source path AND the
    original parse exception. Operator forensics: a log line that just
    says 'something quarantined' without saying WHERE or WHY is useless;
    a line with both lets the on-call grep for "failed to parse" + see
    the path + the JSON decoder's error in one shot."""
    source = tmp_path / "schedule.json"
    source.write_text("{ malformed")
    parse_exc = ValueError("Expecting property name enclosed in double quotes")

    with caplog.at_level(logging.WARNING, logger="openmarquee._storage_recovery"):
        quarantine_corrupt_file(source, parse_exc)

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records, "success path must emit at WARNING level"
    msg = warning_records[0].getMessage()
    assert str(source) in msg, f"WARN must include the source path; got: {msg!r}"
    # log.warning("...: %s", ..., exc) → %s renders str(exc); the
    # parse_exc message text must surface.
    assert "Expecting property name" in msg, f"WARN must include the parse exception; got: {msg!r}"


def test_quarantine_preserves_byte_identical_content(tmp_path: Path):
    """Quarantine is a rename, not a copy-then-truncate — the bad bytes
    must round-trip exactly so a postmortem can inspect them. Covers
    invalid-UTF8 bytes that a naive `read_text().write_text()` round-
    trip would silently mangle."""
    source = tmp_path / "tombstones.json"
    # Binary garbage + invalid UTF-8 sequence (lone continuation byte).
    original_bytes = b"\x00\x01\x02\xc3\x28truncated\xff\xfe"
    source.write_bytes(original_bytes)

    result = quarantine_corrupt_file(source, ValueError("decode fail"))

    assert result is not None
    assert result.read_bytes() == original_bytes, (
        "quarantine target must be byte-identical to the original source"
    )


def test_quarantine_coexists_with_prior_quarantine_siblings(tmp_path: Path):
    """If the directory already has older `.corrupt-*` siblings from
    previous quarantine events, a NEW quarantine must not clobber
    them. Different timestamps → different filenames → safe coexistence.
    Same-second collisions are out-of-scope (docstring limit)."""
    source = tmp_path / "flock.json"
    source.write_text("bad")
    older_quarantine = tmp_path / "flock.json.corrupt-2026-01-01T00:00:00Z"
    older_quarantine.write_text("ancient garbage")

    result = quarantine_corrupt_file(source, ValueError("x"))

    assert result is not None
    assert result != older_quarantine, (
        "new quarantine must not reuse the existing older sibling's path"
    )
    assert older_quarantine.exists(), "older quarantine must not be clobbered"
    assert older_quarantine.read_text() == "ancient garbage", (
        "older quarantine's content must be untouched"
    )


def test_quarantine_uses_utc_not_local_time(tmp_path: Path):
    """Regression-lock against a future `datetime.now()` (no tz arg)
    refactor that would silently swap UTC for local time. The Pi
    system TZ stays local per memory `feedback_pi_system_tz_must_stay_
    local` (schedule.py contract); the quarantine timestamp must
    remain UTC so postmortem timestamps cross-correlate cleanly with
    journal entries on hosts in any timezone."""
    source = tmp_path / "settings.json"
    source.write_text("not json")

    before = datetime.now(UTC)
    result = quarantine_corrupt_file(source, ValueError("x"))
    after = datetime.now(UTC)

    assert result is not None
    # Extract the UTC ISO from the filename and confirm it falls in
    # the [before, after] window inclusive of second-rounding. The
    # upper bound widens by +1s so sub-second drift across the three
    # datetime reads (e.g. before=12:00:00.999 → helper=12:00:01.000 →
    # after=12:00:01.001 where after-floored == 12:00:01 == parsed
    # ✓ but a slow CI runner could flip the math the other way) can't
    # produce a flake under load.
    match = re.search(r"\.corrupt-(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z$", result.name)
    assert match, f"could not parse UTC stamp from filename {result.name!r}"
    parsed = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    lower = before.replace(microsecond=0)
    upper = after.replace(microsecond=0) + timedelta(seconds=1)
    assert lower <= parsed <= upper, (
        f"quarantine timestamp {parsed} not in [{lower}, {upper}] window — "
        f"is the helper accidentally using local time instead of UTC?"
    )
