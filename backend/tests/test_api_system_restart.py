"""Recovery A4: POST /api/system/restart.

Covers the endpoint (202 happy path + 503 when the privilege crossing
fails) and the `system_control.reboot_device` -> netctl bridge. The
actual `systemctl reboot` is never run — we assert the endpoint routes
through the netctl daemon client and maps failures to a clear HTTP
status rather than a silent no-op.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from openmarquee import system_control
from openmarquee.app import app


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def test_restart_happy_path_returns_202_and_invokes_reboot(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    called = {"n": 0}

    def fake_reboot(**_kwargs) -> None:
        called["n"] += 1

    monkeypatch.setattr(system_control, "reboot_device", fake_reboot)

    resp = client.post("/api/system/restart")
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "restarting"
    assert "minute" in body["message"].lower()
    assert called["n"] == 1


def test_restart_maps_control_error_to_503(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    def boom(**_kwargs) -> None:
        raise system_control.SystemControlError("netctl socket not found")

    monkeypatch.setattr(system_control, "reboot_device", boom)

    resp = client.post("/api/system/restart")
    assert resp.status_code == 503
    assert "restart unavailable" in resp.json()["detail"]


def test_reboot_device_sends_reboot_subcommand(monkeypatch: pytest.MonkeyPatch):
    sent: dict = {}

    def fake_send(subcommand, payload=b"", **kwargs) -> None:
        sent["subcommand"] = subcommand
        sent["payload"] = payload
        sent["error_cls"] = kwargs.get("error_cls")

    monkeypatch.setattr(system_control, "netctl_send", fake_send)

    system_control.reboot_device()
    assert sent["subcommand"] == "reboot"
    assert sent["payload"] == b""
    # The bridge tags its own typed exception so the API can distinguish
    # a control failure from any other RuntimeError.
    assert sent["error_cls"] is system_control.SystemControlError


def test_reboot_device_wraps_send_failure_in_control_error(
    monkeypatch: pytest.MonkeyPatch,
):
    # Real netctl_send raises error_cls (which reboot_device passes as
    # SystemControlError) on socket failure; simulate that contract.
    def failing_send(subcommand, payload=b"", *, error_cls=RuntimeError, **_kwargs):
        raise error_cls("socket connect failed")

    monkeypatch.setattr(system_control, "netctl_send", failing_send)

    with pytest.raises(system_control.SystemControlError, match="socket connect failed"):
        system_control.reboot_device()
