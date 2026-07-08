"""Recovery A1: tests for openmarquee.boot_hint + its wiring into the
network-supervisor singleton (dependencies.py).

`boot_hint` reads/consumes the tmpfs setup hint written by the
power-cycle gesture; the singleton wiring forces the supervisor into
Setup Mode when the hint says "setup", overriding a probed/persisted
ONLINE (the operator's physical gesture wins).
"""

from __future__ import annotations

from pathlib import Path

from openmarquee import boot_hint


def test_read_returns_none_when_absent(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENMARQUEE_BOOT_HINT_PATH", str(tmp_path / "nope"))
    assert boot_hint.read_boot_hint() is None


def test_read_returns_stripped_hint(tmp_path: Path, monkeypatch):
    hint = tmp_path / "boot-hint"
    hint.write_text("setup\n")
    monkeypatch.setenv("OPENMARQUEE_BOOT_HINT_PATH", str(hint))
    assert boot_hint.read_boot_hint() == "setup"


def test_read_empty_file_is_none(tmp_path: Path, monkeypatch):
    hint = tmp_path / "boot-hint"
    hint.write_text("   \n")
    monkeypatch.setenv("OPENMARQUEE_BOOT_HINT_PATH", str(hint))
    assert boot_hint.read_boot_hint() is None


def test_consume_reads_then_unlinks_when_permitted(tmp_path: Path, monkeypatch):
    hint = tmp_path / "boot-hint"
    hint.write_text("setup\n")
    monkeypatch.setenv("OPENMARQUEE_BOOT_HINT_PATH", str(hint))
    assert boot_hint.consume_boot_hint() == "setup"
    # tmp dir is writable → consumed (removed) immediately.
    assert not hint.exists()


def test_consume_absent_is_none_and_no_error(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENMARQUEE_BOOT_HINT_PATH", str(tmp_path / "nope"))
    assert boot_hint.consume_boot_hint() is None


def test_consume_tolerates_unlink_failure(tmp_path: Path, monkeypatch):
    # Simulate the on-device case: read succeeds but unlink is denied.
    # consume must still RETURN the hint (the clear-timer cleans up).
    hint = tmp_path / "boot-hint"
    hint.write_text("setup\n")
    monkeypatch.setenv("OPENMARQUEE_BOOT_HINT_PATH", str(hint))

    real_unlink = Path.unlink

    def deny_unlink(self, *a, **k):
        if self == hint:
            raise PermissionError("denied")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", deny_unlink)
    assert boot_hint.consume_boot_hint() == "setup"


def test_setup_hint_forces_supervisor_into_setup(tmp_path: Path, monkeypatch):
    """Integration: a configured device (probe would seed ONLINE) with a
    'setup' boot-hint present must come up in SETUP — the gesture wins."""
    import openmarquee.network_supervisor as ns
    from openmarquee import dependencies
    from openmarquee.network_supervisor import (
        NetworkSupervisor,
        SupervisorEvent,
        SupervisorState,
    )

    # Isolate the persisted-state file so we don't touch a real
    # /var/openmarquee/network-state.json.
    monkeypatch.setattr(ns, "DEFAULT_STATE_FILE", tmp_path / "network-state.json")

    # Simulate a configured device: the probe drives the supervisor to
    # ONLINE (as a pre-staged / persisted-online sign would).
    def _seed_online(self, probe_fn=None):
        self.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        self.apply_event(SupervisorEvent.STA_ASSOCIATED)
        self.apply_event(SupervisorEvent.LINGER_TIMER_EXPIRED)
        return True

    monkeypatch.setattr(NetworkSupervisor, "probe_and_seed_from_existing_connection", _seed_online)

    hint = tmp_path / "boot-hint"
    hint.write_text("setup\n")
    monkeypatch.setenv("OPENMARQUEE_BOOT_HINT_PATH", str(hint))

    dependencies._network_supervisor_singleton.cache_clear()
    try:
        sup = dependencies.get_network_supervisor()
        # The gesture overrode the probed ONLINE.
        assert sup.current_state == SupervisorState.SETUP
        # Hint consumed (tmp dir writable).
        assert not hint.exists()
    finally:
        dependencies._network_supervisor_singleton.cache_clear()


def test_lingering_hint_from_setup_is_idempotent_no_op(tmp_path: Path, monkeypatch):
    """Re-consume hazard: a backend restart (new process) while the hint
    is still present AND the supervisor is already in SETUP must be a
    genuine no-op — re-forcing OPERATOR_REQUESTED_SETUP_MODE from SETUP
    has no transition. This is the safety net that makes the best-effort
    (non-guaranteed) hint deletion acceptable: within the +20s clear
    window a re-read can't churn a sign that's legitimately in SETUP."""
    import openmarquee.network_supervisor as ns
    from openmarquee import dependencies
    from openmarquee.network_supervisor import NetworkSupervisor, SupervisorState

    monkeypatch.setattr(ns, "DEFAULT_STATE_FILE", tmp_path / "network-state.json")

    # Probe is a no-op (device already in SETUP; nothing to seed).
    monkeypatch.setattr(
        NetworkSupervisor,
        "probe_and_seed_from_existing_connection",
        lambda self, probe_fn=None: False,
    )

    # Simulate the unlink being denied (on-device perms), so the hint
    # lingers across this and any subsequent construction.
    real_unlink = Path.unlink
    hint = tmp_path / "boot-hint"
    hint.write_text("setup\n")

    def deny_unlink(self, *a, **k):
        if self == hint:
            raise PermissionError("denied")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", deny_unlink)
    monkeypatch.setenv("OPENMARQUEE_BOOT_HINT_PATH", str(hint))

    dependencies._network_supervisor_singleton.cache_clear()
    try:
        # First construction: SETUP → forced SETUP (no-op, already SETUP).
        sup1 = dependencies.get_network_supervisor()
        assert sup1.current_state == SupervisorState.SETUP
        # Second construction (backend restart, hint still lingering):
        # still SETUP, no crash, no churn.
        dependencies._network_supervisor_singleton.cache_clear()
        sup2 = dependencies.get_network_supervisor()
        assert sup2.current_state == SupervisorState.SETUP
        assert hint.exists()  # deletion was denied; clear-timer owns cleanup
    finally:
        dependencies._network_supervisor_singleton.cache_clear()


def test_no_hint_leaves_probed_online(tmp_path: Path, monkeypatch):
    """Control: without a hint, the probed ONLINE stands (no spurious
    SETUP)."""
    import openmarquee.network_supervisor as ns
    from openmarquee import dependencies
    from openmarquee.network_supervisor import (
        NetworkSupervisor,
        SupervisorEvent,
        SupervisorState,
    )

    monkeypatch.setattr(ns, "DEFAULT_STATE_FILE", tmp_path / "network-state.json")

    def _seed_online(self, probe_fn=None):
        self.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        self.apply_event(SupervisorEvent.STA_ASSOCIATED)
        self.apply_event(SupervisorEvent.LINGER_TIMER_EXPIRED)
        return True

    monkeypatch.setattr(NetworkSupervisor, "probe_and_seed_from_existing_connection", _seed_online)
    monkeypatch.setenv("OPENMARQUEE_BOOT_HINT_PATH", str(tmp_path / "absent"))

    dependencies._network_supervisor_singleton.cache_clear()
    try:
        sup = dependencies.get_network_supervisor()
        assert sup.current_state == SupervisorState.ONLINE
    finally:
        dependencies._network_supervisor_singleton.cache_clear()
