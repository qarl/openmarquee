"""Recovery A3: POST /api/system/factory-reset + the
system_control.factory_reset_device wipe/teardown bridge.

The destructive `systemctl reboot` + nmcli teardown never runs here — we
assert the endpoint's confirm-token gate, that it routes the right paths
to the wipe, that the wipe deletes data + hands off to the netctl daemon,
and that failures map to a clear HTTP status.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openmarquee import system_control
from openmarquee.app import app

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_factory_reset_crossing_present_in_daemon_and_helper():
    """The bash `factory-reset` case reboots, so it can't be executed in
    tests. Guard against silent drift (a dropped allowlist entry or helper
    case would 503 forever) with a static presence check on both halves of
    the privilege crossing."""
    daemon = (_REPO_ROOT / "system" / "openmarquee-netctl-daemon").read_text()
    helper = (_REPO_ROOT / "system" / "openmarquee-netctl").read_text()
    assert '"factory-reset"' in daemon, "factory-reset missing from daemon ALLOWLIST"
    assert "factory-reset)" in helper, "factory-reset case missing from bash helper"


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


# --- system_control.factory_reset_device (the bridge) ---


def test_factory_reset_device_wipes_data_then_calls_netctl(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    playlist = tmp_path / "playlist.json"
    playlist.write_text("{}")
    missing = tmp_path / "never-existed.json"  # already gone → tolerated
    content_root = tmp_path / "content"
    (content_root / "item-1").mkdir(parents=True)
    (content_root / "item-1" / "asset.bin").write_text("x")
    (content_root / "loose.txt").write_text("y")

    sent: dict = {}

    def fake_send(subcommand, payload=b"", **kwargs):
        sent["subcommand"] = subcommand
        sent["error_cls"] = kwargs.get("error_cls")

    monkeypatch.setattr(system_control, "netctl_send", fake_send)

    system_control.factory_reset_device(
        data_files=[settings, playlist, missing],
        content_root=content_root,
    )

    # Data files gone.
    assert not settings.exists()
    assert not playlist.exists()
    # Content root KEPT, but all children removed (dirs + loose files).
    assert content_root.exists()
    assert not (content_root / "item-1").exists()
    assert not (content_root / "loose.txt").exists()
    # Privileged teardown+reboot handed off with the right typed error.
    assert sent["subcommand"] == "factory-reset"
    assert sent["error_cls"] is system_control.SystemControlError


def test_factory_reset_device_tolerates_none_content_root(tmp_path, monkeypatch):
    monkeypatch.setattr(system_control, "netctl_send", lambda *a, **k: None)
    # No content root + no files → still calls netctl, no error.
    system_control.factory_reset_device(data_files=[], content_root=None)


def test_wipe_does_not_escape_root_via_symlink_child(tmp_path, monkeypatch):
    """Safety guard: a symlink child (even one pointing to a directory
    OUTSIDE the content root) must be UNLINKED, not rmtree'd — so the
    wipe can't delete the symlink's target."""
    monkeypatch.setattr(system_control, "netctl_send", lambda *a, **k: None)

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("do not delete")

    content_root = tmp_path / "content"
    content_root.mkdir()
    link = content_root / "evil-link"
    link.symlink_to(outside, target_is_directory=True)

    system_control.factory_reset_device(data_files=[], content_root=content_root)

    # The symlink itself is gone from the content root...
    assert not link.exists()
    # ...but its target directory + contents survive untouched.
    assert outside.exists()
    assert (outside / "precious.txt").read_text() == "do not delete"


def test_factory_reset_device_wraps_netctl_failure(monkeypatch):
    def failing(subcommand, payload=b"", *, error_cls=RuntimeError, **_kwargs):
        raise error_cls("socket connect failed")

    monkeypatch.setattr(system_control, "netctl_send", failing)
    with pytest.raises(system_control.SystemControlError, match="socket connect failed"):
        system_control.factory_reset_device(data_files=[], content_root=None)


# --- the endpoint ---


def test_factory_reset_missing_confirm_is_422(client: TestClient):
    r = client.post("/api/system/factory-reset", json={})
    assert r.status_code == 422  # pydantic: `confirm` required


def test_factory_reset_wrong_confirm_is_400(client: TestClient):
    r = client.post("/api/system/factory-reset", json={"confirm": "yes"})
    assert r.status_code == 400
    assert "confirm" in r.json()["detail"]


def test_factory_reset_happy_path_202_and_routes_paths(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    captured: dict = {}

    def fake_reset(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(system_control, "factory_reset_device", fake_reset)

    r = client.post("/api/system/factory-reset", json={"confirm": "factory-reset"})
    assert r.status_code == 202
    assert r.json()["status"] == "resetting"
    # The endpoint gathered the full fresh-flash wipe set.
    assert "data_files" in captured
    names = [p.name for p in captured["data_files"]]
    joined = " ".join(names)
    assert "settings" in joined
    assert "network-state" in joined
    assert "auth" in joined  # operator password cleared (handover)
    assert "tombstone" in joined
    assert "seeded" in joined  # re-seed demo content on next boot
    assert "wpa_supplicant" in joined  # plaintext prefill residue
    assert len(captured["data_files"]) == 9
    assert "content_root" in captured


def test_factory_reset_maps_control_error_to_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    def boom(**_kwargs):
        raise system_control.SystemControlError("netctl socket not found")

    monkeypatch.setattr(system_control, "factory_reset_device", boom)
    r = client.post("/api/system/factory-reset", json={"confirm": "factory-reset"})
    assert r.status_code == 503
    assert "factory reset unavailable" in r.json()["detail"]
