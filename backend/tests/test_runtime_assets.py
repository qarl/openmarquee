"""r67 (2026-06-05): backend-side runtime asset verification tests.

Counterpart to scripts/tests/test_check_runtime_assets.sh — the bash
check gates deploy, this Python check gates runtime observability.
Tests the same three cases:

  1. Font absent → AssetIssue surfaced + CRITICAL log line emitted.
  2. Font present but 0-byte → AssetIssue surfaced + CRITICAL log.
  3. Font present + healthy → empty issues list, no CRITICAL.
  4. Fonts dir doesn't exist (dev laptop) → empty list + INFO skip.

The "intentionally remove the font and assert the check fires" case
(case 1) is the regression test r67 dispatch mandated — it proves
the check itself works, so a future refactor that silently no-ops
the check has a green test that turns red.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from openmarquee.runtime_assets import (
    AssetIssue,
    check_runtime_assets,
    log_runtime_asset_issues,
)


def test_missing_font_surfaces_asset_issue(tmp_path: Path) -> None:
    """The exact failure mode r67 was built to catch: the canonical
    /opt/openmarquee/ui/fonts/ dir EXISTS on the target Pi but the
    emoji font has been wiped (partial-rsync, manual rm, --delete
    propagating a pruned local clone). Check MUST flag this."""
    # Empty fonts dir, font absent.
    issues = check_runtime_assets(fonts_dir=tmp_path)
    assert len(issues) == 1
    issue = issues[0]
    assert isinstance(issue, AssetIssue)
    assert issue.asset == "COLRv1 emoji font"
    assert issue.path.name == "noto-color-emoji-colrv1.ttf"
    assert "missing" in issue.reason
    assert "download-emoji-font-colrv1.sh" in issue.remedy


def test_zero_byte_stub_surfaces_asset_issue(tmp_path: Path) -> None:
    """Font file exists but is 0 bytes (truncated download / failed
    SVG-strip / disk-full mid-write). Must be flagged with size in
    the reason so the operator sees `only 0 bytes` not just
    `something wrong`."""
    (tmp_path / "noto-color-emoji-colrv1.ttf").write_bytes(b"")
    issues = check_runtime_assets(fonts_dir=tmp_path)
    assert len(issues) == 1
    assert "0 bytes" in issues[0].reason
    assert "stub" in issues[0].reason or "truncated" in issues[0].reason


def test_healthy_font_returns_empty_list(tmp_path: Path) -> None:
    """Font >= 1 MB → no issues, list is empty. Pins the contract
    that a healthy install is the silent path."""
    (tmp_path / "noto-color-emoji-colrv1.ttf").write_bytes(b"x" * (2 * 1024 * 1024))
    issues = check_runtime_assets(fonts_dir=tmp_path)
    assert issues == []


def test_nonexistent_fonts_dir_silently_skips(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Dev laptop case: /opt/openmarquee/ was never provisioned. The
    check must NOT false-positive on a non-existent dir; instead it
    skips with INFO so the test journal stays clean and the dev
    workflow doesn't surface a CRITICAL on every backend boot."""
    missing_dir = tmp_path / "does-not-exist"
    with caplog.at_level(logging.INFO, logger="openmarquee.runtime_assets"):
        issues = check_runtime_assets(fonts_dir=missing_dir)
    assert issues == []
    assert any("does not exist" in r.message for r in caplog.records)


def test_log_runtime_asset_issues_emits_critical(caplog: pytest.LogCaptureFixture) -> None:
    """log_runtime_asset_issues MUST emit CRITICAL (not WARNING /
    INFO) so the issue surfaces in operator-readable logs even when
    a typical service log level filters below ERROR. The specific
    text "render as tofu" + the remedy MUST appear so the line is
    self-diagnosing."""
    issues = [
        AssetIssue(
            asset="COLRv1 emoji font",
            path=Path("/opt/openmarquee/ui/fonts/noto-color-emoji-colrv1.ttf"),
            reason="file missing",
            remedy="run scripts/download-emoji-font-colrv1.sh on the dev host, then re-deploy",
        )
    ]
    with caplog.at_level(logging.CRITICAL, logger="openmarquee.runtime_assets"):
        log_runtime_asset_issues(issues)
    critical_records = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(critical_records) == 1
    msg = critical_records[0].getMessage()
    assert "render as tofu" in msg
    assert "FontMissing" in msg
    assert "download-emoji-font-colrv1.sh" in msg


def test_env_override_resolves_fonts_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """OPENMARQUEE_UI_FONTS_DIR overrides the default path so dev /
    Docker / non-standard deploys can point the check at the right
    place without code changes."""
    (tmp_path / "noto-color-emoji-colrv1.ttf").write_bytes(b"x" * (2 * 1024 * 1024))
    monkeypatch.setenv("OPENMARQUEE_UI_FONTS_DIR", str(tmp_path))
    # No explicit fonts_dir arg → falls through to env override.
    issues = check_runtime_assets()
    assert issues == []
