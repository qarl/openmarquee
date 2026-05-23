"""Tests for backend/openmarquee/identity.py — the MySignXXX reader.

The reader is the gateway by which /api/system/info exposes the
device_id to the operator UI. Failure modes (missing file, bad
JSON, format violation) must degrade to None so off-device dev
hosts + corrupted-state Pis both fall back gracefully to the OS
hostname.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openmarquee import identity


@pytest.fixture
def identity_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect OPENMARQUEE_IDENTITY_PATH at a tmp file."""
    path = tmp_path / "identity.json"
    monkeypatch.setenv("OPENMARQUEE_IDENTITY_PATH", str(path))
    return path


def test_reads_valid_device_id(identity_env: Path) -> None:
    identity_env.write_text(json.dumps({"device_id": "MySign7K2"}))
    assert identity.read_device_id() == "MySign7K2"


def test_returns_none_when_file_missing(identity_env: Path) -> None:
    """Off-device dev path: identity.json doesn't exist; UI falls
    back to OS hostname. Must not raise."""
    assert not identity_env.exists()
    assert identity.read_device_id() is None


def test_returns_none_when_json_invalid(identity_env: Path) -> None:
    identity_env.write_text("{not json")
    assert identity.read_device_id() is None


def test_returns_none_when_device_id_missing(identity_env: Path) -> None:
    identity_env.write_text(json.dumps({"other_field": "stuff"}))
    assert identity.read_device_id() is None


def test_returns_none_on_format_violation(identity_env: Path) -> None:
    """Format is MySign + 3 [A-Z0-9]. Lowercase, wrong prefix,
    wrong length, or extra chars all reject."""
    for bad in [
        "mysign7K2",      # lowercase prefix
        "MySign7k2",      # lowercase suffix char
        "MySign7K",       # too short
        "MySign7K22",     # too long
        "OtherPrefix7K2", # wrong prefix
        "MySign-K2",      # hyphen in suffix
        "MySign7!2",      # special char in suffix
        "",               # empty
    ]:
        identity_env.write_text(json.dumps({"device_id": bad}))
        assert identity.read_device_id() is None, (
            f"format-invalid {bad!r} should have returned None"
        )


def test_returns_none_when_device_id_not_string(identity_env: Path) -> None:
    """Defensive: someone hand-edits identity.json and types a
    number / null. read_device_id is the only contract enforcement
    between disk and the wire shape."""
    for bad in [123, None, ["MySign7K2"], {"nested": "obj"}]:
        identity_env.write_text(json.dumps({"device_id": bad}))
        assert identity.read_device_id() is None, (
            f"non-string {bad!r} should have returned None"
        )


def test_format_regex_pins_valid_set() -> None:
    """The DEVICE_ID_RE is the wire contract; any change here must
    be a deliberate breaking change."""
    assert identity.DEVICE_ID_RE.match("MySign000")
    assert identity.DEVICE_ID_RE.match("MySignZZZ")
    assert identity.DEVICE_ID_RE.match("MySign7K2")
    assert not identity.DEVICE_ID_RE.match("MySign7k2")  # case-sensitive


def test_path_overridable_via_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """OPENMARQUEE_IDENTITY_PATH lets tests + dev hosts point at a
    fixture without touching /var/. Pinned because regressions here
    break the test suite silently."""
    custom = tmp_path / "custom" / "identity.json"
    custom.parent.mkdir()
    custom.write_text(json.dumps({"device_id": "MySignABC"}))
    monkeypatch.setenv("OPENMARQUEE_IDENTITY_PATH", str(custom))
    assert identity.read_device_id() == "MySignABC"
