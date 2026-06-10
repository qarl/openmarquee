"""P1.2-B take-over orchestrator tests.

Cover Tailscale pre-flight parser, RollbackWatchdog timing, the
TakeoverOrchestrator's flip + rollback path with all subprocess
hops monkeypatched, and the lifespan entry-point's preconditions
gate.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from openmarquee.network_supervisor import (
    NetworkSupervisor,
    SupervisorConfig,
)
from openmarquee.network_supervisor_takeover import (
    TAKEOVER_ENV_FLAG,
    RollbackWatchdog,
    TailscalePreflightResult,
    TakeoverOrchestrator,
    TakeoverPaths,
    parse_tailscale_status_json,
    render_nm_unmanage_drop_in,
    render_wpa_supplicant_conf,
)

# ============================================================
# parse_tailscale_status_json
# ============================================================


class TestParseTailscaleStatusJson:
    def test_running_with_local_ip(self):
        raw = json.dumps(
            {
                "BackendState": "Running",
                "Self": {"TailscaleIPs": ["100.x.y.z"]},
            }
        )
        r = parse_tailscale_status_json(raw)
        assert r.ok is True
        assert r.backend_state == "Running"
        assert r.local_node_ip == "100.x.y.z"

    def test_stopped_backend(self):
        raw = json.dumps({"BackendState": "Stopped"})
        r = parse_tailscale_status_json(raw)
        assert r.ok is False
        assert "backend_state_not_running" in r.reason

    def test_running_without_ip(self):
        raw = json.dumps({"BackendState": "Running", "Self": {"TailscaleIPs": []}})
        r = parse_tailscale_status_json(raw)
        assert r.ok is False
        assert "no_tailscale_ip" in r.reason

    def test_malformed_json(self):
        r = parse_tailscale_status_json("not json")
        assert r.ok is False
        assert "json_decode_error" in r.reason


# ============================================================
# render_* pure-function tests
# ============================================================


def test_render_nm_unmanage_drop_in_targets_wlan0_by_default():
    out = render_nm_unmanage_drop_in()
    assert "[keyfile]" in out
    assert "unmanaged-devices=interface-name:wlan0" in out


def test_render_nm_unmanage_drop_in_respects_extra_iface():
    out = render_nm_unmanage_drop_in("foo0")
    assert "unmanaged-devices=interface-name:foo0" in out


def test_render_wpa_supplicant_conf_embeds_credentials():
    out = render_wpa_supplicant_conf("pikazo", "secret-pass", country="GB")
    assert "country=GB" in out
    assert 'ssid="pikazo"' in out
    assert 'psk="secret-pass"' in out


# ============================================================
# RollbackWatchdog
# ============================================================


class TestRollbackWatchdog:
    @pytest.mark.asyncio
    async def test_fires_after_timeout(self):
        fired = asyncio.Event()

        async def cb():
            fired.set()

        wd = RollbackWatchdog(timeout_s=0.05, rollback_cb=cb)
        await wd.arm()
        await asyncio.wait_for(fired.wait(), timeout=0.5)
        assert wd.fired is True
        assert wd.armed is False

    @pytest.mark.asyncio
    async def test_disarm_prevents_fire(self):
        fired = asyncio.Event()

        async def cb():
            fired.set()

        wd = RollbackWatchdog(timeout_s=10.0, rollback_cb=cb)
        await wd.arm()
        assert wd.armed is True
        await wd.disarm()
        # Sleep longer than would matter for any reasonable race.
        await asyncio.sleep(0.05)
        assert fired.is_set() is False
        assert wd.fired is False

    @pytest.mark.asyncio
    async def test_disarm_is_idempotent(self):
        async def cb():
            pass

        wd = RollbackWatchdog(timeout_s=10.0, rollback_cb=cb)
        await wd.arm()
        await wd.disarm()
        # Second disarm must not raise.
        await wd.disarm()

    @pytest.mark.asyncio
    async def test_arm_is_idempotent(self):
        async def cb():
            pass

        wd = RollbackWatchdog(timeout_s=10.0, rollback_cb=cb)
        await wd.arm()
        first_task = wd._task
        await wd.arm()
        assert wd._task is first_task
        await wd.disarm()


# ============================================================
# TakeoverOrchestrator
# ============================================================


@dataclass
class _StubPaths:
    nm_unmanage_path: Path
    wpa_conf_path: Path
    nm_dir: Path


def _make_supervisor(tmp_path: Path) -> NetworkSupervisor:
    return NetworkSupervisor(
        config=SupervisorConfig(state_file=tmp_path / "supervisor-state.json"),
    )


def _stub_paths(tmp_path: Path) -> TakeoverPaths:
    nm_dir = tmp_path / "etc-nm"
    nm_dir.mkdir()
    return TakeoverPaths(
        nm_unmanage_path=nm_dir / "openmarquee-wlan0-unmanaged.conf",
        wpa_conf_path=tmp_path / "wpa_supplicant-wlan0.conf",
        nm_dir=nm_dir,
    )


@pytest.fixture
def systemctl_recorder(monkeypatch):
    """Replace the orchestrator's _systemctl with a recording stub."""
    calls: list[list[str]] = []

    async def _stub_systemctl(args, *, timeout_s=15.0):
        calls.append(list(args))

    monkeypatch.setattr(
        "openmarquee.network_supervisor_takeover._systemctl",
        _stub_systemctl,
    )
    return calls


@pytest.mark.asyncio
async def test_orchestrator_attempt_writes_files_and_calls_systemctl(
    tmp_path: Path, systemctl_recorder, monkeypatch
):
    sup = _make_supervisor(tmp_path)
    paths = _stub_paths(tmp_path)
    orch = TakeoverOrchestrator(supervisor=sup, paths=paths, rollback_timeout_s=10.0)
    # Pre-flight Tailscale: pass via direct stub.

    async def _ok_tailscale():
        return TailscalePreflightResult(ok=True, reason="ok", local_node_ip="100.x.y.z")

    monkeypatch.setattr(
        "openmarquee.network_supervisor_takeover.tailscale_preflight_check",
        _ok_tailscale,
    )
    watchdog = await orch.attempt(
        wifi_station_ssid="pikazo",
        wifi_station_password="hunter2",
    )
    # NM drop-in + wpa_supplicant conf both written.
    assert paths.nm_unmanage_path.exists()
    assert "wlan0" in paths.nm_unmanage_path.read_text()
    assert paths.wpa_conf_path.exists()
    assert 'ssid="pikazo"' in paths.wpa_conf_path.read_text()
    # systemctl reload NetworkManager AND enable wpa_supplicant@wlan0.
    assert ["reload", "NetworkManager"] in systemctl_recorder
    assert ["enable", "--now", "wpa_supplicant@wlan0.service"] in systemctl_recorder
    assert watchdog.armed is True
    # Cleanup: disarm so we don't leak the timer task.
    await watchdog.disarm()


@pytest.mark.asyncio
async def test_orchestrator_mark_associated_disarms_and_persists(
    tmp_path: Path, systemctl_recorder, monkeypatch
):
    sup = _make_supervisor(tmp_path)
    paths = _stub_paths(tmp_path)
    orch = TakeoverOrchestrator(supervisor=sup, paths=paths, rollback_timeout_s=10.0)

    async def _ok():
        return TailscalePreflightResult(ok=True, reason="ok", local_node_ip="100.x")

    monkeypatch.setattr("openmarquee.network_supervisor_takeover.tailscale_preflight_check", _ok)
    await orch.attempt(wifi_station_ssid="pikazo", wifi_station_password="hunter2")
    assert sup.takeover_active is False
    await orch.mark_associated()
    assert sup.takeover_active is True
    assert orch.watchdog.armed is False


@pytest.mark.asyncio
async def test_orchestrator_rollback_cleans_drop_in(
    tmp_path: Path, systemctl_recorder, monkeypatch
):
    sup = _make_supervisor(tmp_path)
    paths = _stub_paths(tmp_path)
    # Pre-populate the NM drop-in so we can verify rollback deletes it.
    paths.nm_unmanage_path.write_text("simulated")
    orch = TakeoverOrchestrator(supervisor=sup, paths=paths, rollback_timeout_s=10.0)
    await orch.rollback()
    assert not paths.nm_unmanage_path.exists()
    # systemctl stop wpa_supplicant@wlan0 + reload NetworkManager.
    assert ["stop", "wpa_supplicant@wlan0.service"] in systemctl_recorder
    assert ["reload", "NetworkManager"] in systemctl_recorder
    # Supervisor state reflects rollback.
    assert sup.takeover_active is False
    assert sup.rollback_fired_at is not None


@pytest.mark.asyncio
async def test_orchestrator_rollback_fires_via_watchdog_on_timeout(
    tmp_path: Path, systemctl_recorder, monkeypatch
):
    """End-to-end self-healing path: attempt() arms the watchdog;
    if mark_associated never fires, the watchdog runs rollback().
    Uses a tiny timeout for the test."""
    sup = _make_supervisor(tmp_path)
    paths = _stub_paths(tmp_path)
    orch = TakeoverOrchestrator(supervisor=sup, paths=paths, rollback_timeout_s=0.05)

    async def _ok():
        return TailscalePreflightResult(ok=True, reason="ok", local_node_ip="100.x")

    monkeypatch.setattr("openmarquee.network_supervisor_takeover.tailscale_preflight_check", _ok)
    watchdog = await orch.attempt(wifi_station_ssid="pikazo", wifi_station_password="hunter2")
    # Wait for the watchdog to fire.
    for _ in range(20):
        await asyncio.sleep(0.05)
        if watchdog.fired:
            break
    assert watchdog.fired is True
    # Rollback deleted the drop-in + persisted the marker.
    assert not paths.nm_unmanage_path.exists()
    assert sup.rollback_fired_at is not None


# ============================================================
# evaluate_preconditions — each fail mode + the all-met case
# ============================================================


@pytest.mark.asyncio
async def test_evaluate_requires_env_flag(tmp_path: Path, monkeypatch):
    sup = _make_supervisor(tmp_path)
    paths = _stub_paths(tmp_path)
    orch = TakeoverOrchestrator(supervisor=sup, paths=paths)

    async def _ok():
        return TailscalePreflightResult(ok=True, reason="ok", local_node_ip="100.x")

    monkeypatch.setattr("openmarquee.network_supervisor_takeover.tailscale_preflight_check", _ok)
    pre = await orch.evaluate_preconditions(
        wifi_station_ssid="pikazo",
        wifi_station_password="hunter2",
        takeover_active_in_state=False,
        rollback_fired_at=None,
        env={},  # no env flag
    )
    assert pre.all_met is False
    assert any("OPENMARQUEE_NETWORK_TAKEOVER_ENABLED" in r for r in pre.reasons())


@pytest.mark.asyncio
async def test_evaluate_requires_credentials(tmp_path: Path, monkeypatch):
    sup = _make_supervisor(tmp_path)
    paths = _stub_paths(tmp_path)
    orch = TakeoverOrchestrator(supervisor=sup, paths=paths)

    async def _ok():
        return TailscalePreflightResult(ok=True, reason="ok", local_node_ip="100.x")

    monkeypatch.setattr("openmarquee.network_supervisor_takeover.tailscale_preflight_check", _ok)
    pre = await orch.evaluate_preconditions(
        wifi_station_ssid=None,
        wifi_station_password=None,
        takeover_active_in_state=False,
        rollback_fired_at=None,
        env={TAKEOVER_ENV_FLAG: "1"},
    )
    assert pre.all_met is False
    assert any("credentials_missing" in r for r in pre.reasons())


@pytest.mark.asyncio
async def test_evaluate_blocks_when_already_active(tmp_path: Path, monkeypatch):
    sup = _make_supervisor(tmp_path)
    paths = _stub_paths(tmp_path)
    orch = TakeoverOrchestrator(supervisor=sup, paths=paths)

    async def _ok():
        return TailscalePreflightResult(ok=True, reason="ok", local_node_ip="100.x")

    monkeypatch.setattr("openmarquee.network_supervisor_takeover.tailscale_preflight_check", _ok)
    pre = await orch.evaluate_preconditions(
        wifi_station_ssid="pikazo",
        wifi_station_password="hunter2",
        takeover_active_in_state=True,
        rollback_fired_at=None,
        env={TAKEOVER_ENV_FLAG: "1"},
    )
    assert pre.all_met is False
    assert any("already_active" in r for r in pre.reasons())


@pytest.mark.asyncio
async def test_evaluate_blocks_when_rollback_recent(tmp_path: Path, monkeypatch):
    import time as _time

    sup = _make_supervisor(tmp_path)
    paths = _stub_paths(tmp_path)
    orch = TakeoverOrchestrator(supervisor=sup, paths=paths)

    async def _ok():
        return TailscalePreflightResult(ok=True, reason="ok", local_node_ip="100.x")

    monkeypatch.setattr("openmarquee.network_supervisor_takeover.tailscale_preflight_check", _ok)
    pre = await orch.evaluate_preconditions(
        wifi_station_ssid="pikazo",
        wifi_station_password="hunter2",
        takeover_active_in_state=False,
        rollback_fired_at=_time.monotonic() - 60.0,  # 1 min ago, cooldown is 1h
        env={TAKEOVER_ENV_FLAG: "1"},
    )
    assert pre.all_met is False
    assert any("rollback_recently_fired" in r for r in pre.reasons())


@pytest.mark.asyncio
async def test_evaluate_blocks_when_tailscale_down(tmp_path: Path, monkeypatch):
    sup = _make_supervisor(tmp_path)
    paths = _stub_paths(tmp_path)
    orch = TakeoverOrchestrator(supervisor=sup, paths=paths)

    async def _down():
        return TailscalePreflightResult(ok=False, reason="backend_state_not_running")

    monkeypatch.setattr("openmarquee.network_supervisor_takeover.tailscale_preflight_check", _down)
    pre = await orch.evaluate_preconditions(
        wifi_station_ssid="pikazo",
        wifi_station_password="hunter2",
        takeover_active_in_state=False,
        rollback_fired_at=None,
        env={TAKEOVER_ENV_FLAG: "1"},
    )
    assert pre.all_met is False
    assert any("tailscale" in r for r in pre.reasons())


@pytest.mark.asyncio
async def test_apply_event_sta_associated_disarms_watchdog(
    tmp_path: Path, systemctl_recorder, monkeypatch
):
    """Sacred-review BLOCKER regression-lock (P1.2-B): the
    orchestrator MUST install its mark_associated hook on the
    supervisor, and the supervisor MUST fire it on the first
    STA_ASSOCIATED event. Without this wiring the rollback
    watchdog would fire 3 min after a SUCCESSFUL flip.
    """
    sup = _make_supervisor(tmp_path)
    paths = _stub_paths(tmp_path)
    # Tiny watchdog so the test doesn't hang if the wiring is broken.
    orch = TakeoverOrchestrator(supervisor=sup, paths=paths, rollback_timeout_s=2.0)

    async def _ok():
        return TailscalePreflightResult(ok=True, reason="ok", local_node_ip="100.x")

    monkeypatch.setattr("openmarquee.network_supervisor_takeover.tailscale_preflight_check", _ok)
    watchdog = await orch.attempt(
        wifi_station_ssid="pikazo",
        wifi_station_password="hunter2",
    )
    assert watchdog.armed is True
    # Pre-arm the state machine: SETUP -> CONNECTING so STA_ASSOCIATED
    # is a valid transition in the next call.
    from openmarquee.network_supervisor import SupervisorEvent

    sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
    # The wiring under test: STA_ASSOCIATED fires the orchestrator's
    # mark_associated hook via the supervisor's one-shot slot.
    sup.apply_event(SupervisorEvent.STA_ASSOCIATED)
    # Give the asyncio loop a tick to run the scheduled hook task.
    for _ in range(20):
        await asyncio.sleep(0.05)
        if not watchdog.armed:
            break
    assert watchdog.armed is False, "watchdog should be disarmed after STA_ASSOCIATED"
    assert sup.takeover_active is True, "takeover_active should be persisted True"
    # And the hook is one-shot: a second STA_ASSOCIATED must not
    # re-call mark_associated (which would re-disarm an already-
    # disarmed watchdog, harmlessly, but the principle is to keep
    # the slot empty).
    assert sup._takeover_associated_hook is None


@pytest.mark.asyncio
async def test_rollback_clears_takeover_associated_hook(
    tmp_path: Path, systemctl_recorder, monkeypatch
):
    """Defensive: rollback must clear the hook so a delayed
    STA_ASSOCIATED post-rollback doesn't re-trigger mark_associated
    against a state where the watchdog has already fired."""
    sup = _make_supervisor(tmp_path)
    paths = _stub_paths(tmp_path)
    paths.nm_unmanage_path.write_text("simulated")
    orch = TakeoverOrchestrator(supervisor=sup, paths=paths, rollback_timeout_s=10.0)
    # Manually install a fake hook to simulate post-attempt state.
    sup.set_takeover_associated_hook(orch.mark_associated)
    assert sup._takeover_associated_hook is not None
    await orch.rollback()
    assert sup._takeover_associated_hook is None


@pytest.mark.asyncio
async def test_evaluate_all_met_when_everything_lines_up(tmp_path: Path, monkeypatch):
    sup = _make_supervisor(tmp_path)
    paths = _stub_paths(tmp_path)
    orch = TakeoverOrchestrator(supervisor=sup, paths=paths)

    async def _ok():
        return TailscalePreflightResult(ok=True, reason="ok", local_node_ip="100.x")

    monkeypatch.setattr("openmarquee.network_supervisor_takeover.tailscale_preflight_check", _ok)
    pre = await orch.evaluate_preconditions(
        wifi_station_ssid="pikazo",
        wifi_station_password="hunter2",
        takeover_active_in_state=False,
        rollback_fired_at=None,
        env={TAKEOVER_ENV_FLAG: "1"},
    )
    assert pre.all_met is True
    assert pre.reasons() == []
