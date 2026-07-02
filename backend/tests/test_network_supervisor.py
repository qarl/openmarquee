"""P1.1 network-supervisor tests.

Cover:
  * SupervisorState transition table — every (state, event,
    fallback_mutex) tuple defined in network_supervisor.next_state.
  * freq_to_channel parity with the shell snippet (Python re-mirror
    of the P1.0 test).
  * DiagnosticsRingBuffer push + eviction + snapshot.
  * decide_channel_follow's 4 cases.
  * hysteresis_allows_switch + in_band_ssh_guard_safe_to_switch
    (the r60 acceptance criteria).
  * PersistedState round-trip + corrupt-file recovery.
  * parse_wpa_event for canonical CTRL-EVENT-* shapes.
  * NetworkSupervisor.apply_event end-to-end (state machine + ring
    buffer + persistence).
  * NetworkSupervisor.apply_sta_freq fires actuator only on
    channel change.
  * supervisor_to_dict shape for the API.

The wpa_supplicant socket I/O isn't exercised here (would need a
mock DGRAM server); it's a thin shim around the parser + state
machine, both of which ARE exercised.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openmarquee.network_supervisor import (
    DEFAULT_HYSTERESIS_SIGNAL,
    ChannelFollowDecision,
    DiagnosticsRingBuffer,
    NetworkSupervisor,
    PersistedState,
    SupervisorConfig,
    SupervisorEvent,
    SupervisorState,
    decide_channel_follow,
    freq_to_channel,
    hysteresis_allows_switch,
    in_band_ssh_guard_safe_to_switch,
    load_persisted_state,
    next_state,
    parse_wpa_event,
    save_persisted_state,
    supervisor_to_dict,
)

# ============================================================
# next_state — the transition table
# ============================================================


class TestNextState:
    def test_setup_to_connecting_on_creds(self):
        assert (
            next_state(SupervisorState.SETUP, SupervisorEvent.HAS_STORED_CREDENTIALS)
            == SupervisorState.CONNECTING
        )

    def test_connecting_to_linger_in_concurrent_regime(self):
        assert (
            next_state(SupervisorState.CONNECTING, SupervisorEvent.STA_ASSOCIATED)
            == SupervisorState.LINGER
        )

    def test_connecting_to_online_in_mutex_regime(self):
        assert (
            next_state(
                SupervisorState.CONNECTING,
                SupervisorEvent.STA_ASSOCIATED,
                fallback_mutex=True,
            )
            == SupervisorState.ONLINE
        )

    def test_connecting_to_setup_on_auth_fail(self):
        # Auth fail keeps AP up; portal surfaces error. Same in both
        # regimes.
        for mutex in (False, True):
            assert (
                next_state(
                    SupervisorState.CONNECTING,
                    SupervisorEvent.STA_AUTH_FAILED,
                    fallback_mutex=mutex,
                )
                == SupervisorState.SETUP
            )

    def test_linger_timer_expired_to_online(self):
        assert (
            next_state(SupervisorState.LINGER, SupervisorEvent.LINGER_TIMER_EXPIRED)
            == SupervisorState.ONLINE
        )

    def test_linger_disconnect_to_degraded(self):
        assert (
            next_state(SupervisorState.LINGER, SupervisorEvent.STA_DISCONNECTED)
            == SupervisorState.DEGRADED
        )

    def test_online_disconnect_to_degraded(self):
        assert (
            next_state(SupervisorState.ONLINE, SupervisorEvent.STA_DISCONNECTED)
            == SupervisorState.DEGRADED
        )

    def test_degraded_reassociates_to_linger_in_concurrent(self):
        assert (
            next_state(SupervisorState.DEGRADED, SupervisorEvent.STA_ASSOCIATED)
            == SupervisorState.LINGER
        )

    def test_degraded_reassociates_to_online_in_mutex(self):
        assert (
            next_state(
                SupervisorState.DEGRADED,
                SupervisorEvent.STA_ASSOCIATED,
                fallback_mutex=True,
            )
            == SupervisorState.ONLINE
        )

    @pytest.mark.parametrize(
        "from_state",
        [
            SupervisorState.CONNECTING,
            SupervisorState.LINGER,
            SupervisorState.ONLINE,
            SupervisorState.DEGRADED,
        ],
    )
    def test_operator_setup_mode_from_anywhere_but_setup(self, from_state):
        assert (
            next_state(from_state, SupervisorEvent.OPERATOR_REQUESTED_SETUP_MODE)
            == SupervisorState.SETUP
        )

    def test_operator_setup_mode_noop_when_already_in_setup(self):
        # next_state returns None when no transition applies; the
        # supervisor treats this as a logged no-op.
        assert (
            next_state(SupervisorState.SETUP, SupervisorEvent.OPERATOR_REQUESTED_SETUP_MODE) is None
        )

    def test_no_creds_snaps_to_setup_from_any_active_state(self):
        # If creds disappear (operator deletes them via API) the
        # supervisor must drop back to SETUP from anywhere.
        for s in (
            SupervisorState.CONNECTING,
            SupervisorState.LINGER,
            SupervisorState.ONLINE,
            SupervisorState.DEGRADED,
        ):
            assert next_state(s, SupervisorEvent.NO_STORED_CREDENTIALS) == SupervisorState.SETUP

    def test_undefined_pair_returns_none(self):
        # The full Cartesian product is large; assert a few clearly
        # undefined pairs return None so caller's log-and-ignore
        # branch fires correctly.
        assert next_state(SupervisorState.ONLINE, SupervisorEvent.STA_AUTH_FAILED) is None
        assert next_state(SupervisorState.SETUP, SupervisorEvent.LINGER_TIMER_EXPIRED) is None


# ============================================================
# freq_to_channel — Python mirror of the shell math
# ============================================================


@pytest.mark.parametrize(
    "freq_mhz,expected",
    [
        (2412, 1),
        (2437, 6),
        (2462, 11),
        (2472, 13),
        (2484, 14),  # Japan-only special case
        (5180, None),
        (2400, None),  # below 2.4 GHz band
        (3000, None),  # arbitrary out-of-band
    ],
)
def test_freq_to_channel_mirrors_shell(freq_mhz, expected):
    assert freq_to_channel(freq_mhz) == expected


# ============================================================
# DiagnosticsRingBuffer
# ============================================================


class TestDiagnosticsRingBuffer:
    def test_push_records_event(self):
        rb = DiagnosticsRingBuffer(window_seconds=300.0)
        rb.push("state_machine", "info", "hello", now=100.0)
        events = rb.snapshot(now=100.0)
        assert len(events) == 1
        assert events[0].source == "state_machine"
        assert events[0].message == "hello"

    def test_eviction_drops_expired_entries(self):
        rb = DiagnosticsRingBuffer(window_seconds=10.0)
        rb.push("state_machine", "info", "old", now=0.0)
        rb.push("state_machine", "info", "new", now=20.0)
        events = rb.snapshot(now=20.0)
        assert len(events) == 1
        assert events[0].message == "new"

    def test_to_dict_relative_seconds_ago(self):
        rb = DiagnosticsRingBuffer(window_seconds=300.0)
        rb.push("state_machine", "info", "at_zero", now=100.0)
        events = rb.snapshot(now=130.0)
        d = events[0].to_dict(now=130.0)
        assert d["seconds_ago"] == 30.0
        assert d["source"] == "state_machine"

    def test_window_must_be_positive(self):
        with pytest.raises(ValueError):
            DiagnosticsRingBuffer(window_seconds=0.0)
        with pytest.raises(ValueError):
            DiagnosticsRingBuffer(window_seconds=-1.0)


# ============================================================
# decide_channel_follow
# ============================================================


class TestDecideChannelFollow:
    def test_sta_not_associated_keeps_fallback_when_already_there(self):
        d = decide_channel_follow(sta_freq_mhz=None, current_ap_channel=6)
        assert d.target_channel == 6
        assert d.regenerate_needed is False

    def test_sta_not_associated_moves_to_fallback_when_off(self):
        d = decide_channel_follow(sta_freq_mhz=None, current_ap_channel=11)
        assert d.target_channel == 6
        assert d.regenerate_needed is True
        assert "not_associated" in d.reason

    def test_sta_5ghz_falls_back(self):
        d = decide_channel_follow(sta_freq_mhz=5180, current_ap_channel=6)
        assert d.target_channel == 6
        assert d.regenerate_needed is False
        assert "2_4ghz" in d.reason

    def test_sta_2_4ghz_already_matches(self):
        d = decide_channel_follow(sta_freq_mhz=2437, current_ap_channel=6)
        assert d.target_channel == 6
        assert d.regenerate_needed is False
        assert "already" in d.reason

    def test_sta_2_4ghz_needs_follow(self):
        d = decide_channel_follow(sta_freq_mhz=2462, current_ap_channel=6)
        assert d.target_channel == 11
        assert d.regenerate_needed is True
        assert "follow_sta" in d.reason


# ============================================================
# r60 acceptance contracts — hysteresis + in-band SSH guard
# ============================================================


class TestHysteresisContract:
    def test_must_be_strictly_better_by_threshold(self):
        assert hysteresis_allows_switch(50, 41) is True  # +9 >= 8
        assert hysteresis_allows_switch(50, 42) is True  # +8 >= 8
        assert hysteresis_allows_switch(50, 43) is False  # +7 < 8
        assert hysteresis_allows_switch(50, 50) is False  # equal
        assert hysteresis_allows_switch(30, 50) is False  # worse

    def test_threshold_override(self):
        assert hysteresis_allows_switch(50, 49, threshold=1) is True
        assert hysteresis_allows_switch(50, 49, threshold=2) is False

    def test_out_of_range_signal_rejected(self):
        with pytest.raises(ValueError):
            hysteresis_allows_switch(101, 50)
        with pytest.raises(ValueError):
            hysteresis_allows_switch(50, -1)

    def test_default_threshold_is_r60_value(self):
        assert DEFAULT_HYSTERESIS_SIGNAL == 8


class TestInBandSshGuard:
    def test_tailscale_session_makes_switch_safe(self):
        # tailnet survives the wifi blip.
        assert (
            in_band_ssh_guard_safe_to_switch(has_tailscale_session=True, has_lan_only_session=True)
            is True
        )

    def test_lan_only_session_blocks_switch(self):
        assert (
            in_band_ssh_guard_safe_to_switch(has_tailscale_session=False, has_lan_only_session=True)
            is False
        )

    def test_no_sessions_allows_switch(self):
        assert (
            in_band_ssh_guard_safe_to_switch(
                has_tailscale_session=False, has_lan_only_session=False
            )
            is True
        )


# ============================================================
# State persistence
# ============================================================


class TestStatePersistence:
    def test_round_trip_via_atomic_rename(self, tmp_path: Path):
        state_file = tmp_path / "network-state.json"
        original = PersistedState(
            state=SupervisorState.ONLINE,
            last_sta_ssid="qarl",
            last_sta_channel=11,
            last_transition_monotonic=123.456,
        )
        save_persisted_state(original, path=state_file)
        loaded = load_persisted_state(state_file)
        assert loaded is not None
        assert loaded.state == SupervisorState.ONLINE
        assert loaded.last_sta_ssid == "qarl"
        assert loaded.last_sta_channel == 11
        # schema_version survives the round-trip so future format
        # bumps can detect old-shape files + migrate. P1.2-B bumped
        # the version 1 -> 2 to mark the addition of `takeover_active`
        # + `rollback_fired_at`.
        assert loaded.schema_version == 2
        # tmp file should be cleaned up by os.replace
        assert not state_file.with_suffix(state_file.suffix + ".tmp").exists()

    def test_missing_file_returns_none(self, tmp_path: Path):
        assert load_persisted_state(tmp_path / "nope.json") is None

    def test_corrupt_file_returns_none_not_raises(self, tmp_path: Path):
        state_file = tmp_path / "network-state.json"
        state_file.write_text("not json at all { )")
        # Must not raise; supervisor's defensive recovery treats
        # corrupt state as "no state" + starts from SETUP.
        assert load_persisted_state(state_file) is None

    def test_unknown_state_value_returns_none(self, tmp_path: Path):
        state_file = tmp_path / "network-state.json"
        state_file.write_text(json.dumps({"state": "TELEPORTING"}))
        assert load_persisted_state(state_file) is None

    def test_missing_required_field_returns_none(self, tmp_path: Path):
        state_file = tmp_path / "network-state.json"
        # No `state` key.
        state_file.write_text(json.dumps({"schema_version": 1}))
        assert load_persisted_state(state_file) is None


# ============================================================
# parse_wpa_event
# ============================================================


class TestParseWpaEvent:
    def test_ctrl_event_connected(self):
        ev, fields = parse_wpa_event(
            "<3>CTRL-EVENT-CONNECTED - Connection to 11:22:33:44:55:66 completed [id=0 id_str=]"
        )
        assert ev == SupervisorEvent.STA_ASSOCIATED
        assert fields.get("id") == "0"

    def test_ctrl_event_disconnected(self):
        ev, _ = parse_wpa_event(
            "<3>CTRL-EVENT-DISCONNECTED bssid=11:22:33:44:55:66 reason=3 locally_generated=1"
        )
        assert ev == SupervisorEvent.STA_DISCONNECTED

    def test_ctrl_event_auth_reject(self):
        ev, _ = parse_wpa_event(
            "<3>CTRL-EVENT-AUTH-REJECT - Connection refused, locally_generated=1"
        )
        assert ev == SupervisorEvent.STA_AUTH_FAILED

    def test_ctrl_event_ssid_temp_disabled_maps_to_auth_fail(self):
        ev, _ = parse_wpa_event(
            '<3>CTRL-EVENT-SSID-TEMP-DISABLED id=0 ssid="qarl" auth_failures=1 duration=10'
        )
        assert ev == SupervisorEvent.STA_AUTH_FAILED

    def test_unrecognised_event_returns_none(self):
        ev, _ = parse_wpa_event("<3>CTRL-EVENT-SCAN-RESULTS")
        assert ev is None

    def test_handles_missing_priority_prefix(self):
        # If the event arrives without the <N> prefix the parser
        # should still recover.
        ev, _ = parse_wpa_event("CTRL-EVENT-CONNECTED")
        assert ev == SupervisorEvent.STA_ASSOCIATED


# ============================================================
# NetworkSupervisor end-to-end
# ============================================================


class TestNetworkSupervisor:
    def _make(self, tmp_path: Path, *, fallback_mutex: bool = False):
        config = SupervisorConfig(
            fallback_mutex_mode=fallback_mutex,
            state_file=tmp_path / "network-state.json",
        )
        return NetworkSupervisor(config=config)

    def test_starts_in_setup_when_no_state_file(self, tmp_path: Path):
        sup = self._make(tmp_path)
        assert sup.current_state == SupervisorState.SETUP

    def test_resumes_from_persisted_state(self, tmp_path: Path):
        # Pre-populate state file.
        state_file = tmp_path / "network-state.json"
        save_persisted_state(
            PersistedState(state=SupervisorState.ONLINE),
            path=state_file,
        )
        config = SupervisorConfig(state_file=state_file)
        sup = NetworkSupervisor(config=config)
        assert sup.current_state == SupervisorState.ONLINE

    def test_apply_event_drives_state_machine(self, tmp_path: Path):
        sup = self._make(tmp_path)
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        assert sup.current_state == SupervisorState.CONNECTING
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)
        assert sup.current_state == SupervisorState.LINGER

    def test_apply_event_persists_to_disk(self, tmp_path: Path):
        sup = self._make(tmp_path)
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        # Reload via fresh supervisor — should pick up CONNECTING.
        sup2 = NetworkSupervisor(config=sup.config)
        assert sup2.current_state == SupervisorState.CONNECTING

    def test_apply_event_records_diagnostics(self, tmp_path: Path):
        sup = self._make(tmp_path)
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        events = sup.snapshot_diagnostics()
        # Boot event + transition event.
        assert len(events) >= 2
        assert any("transition" in e.message and "CONNECTING" in e.message for e in events)

    def test_unrecognised_event_logged_not_crashed(self, tmp_path: Path):
        sup = self._make(tmp_path)
        # SETUP doesn't handle LINGER_TIMER_EXPIRED — should log + no-op.
        before = sup.current_state
        sup.apply_event(SupervisorEvent.LINGER_TIMER_EXPIRED)
        assert sup.current_state == before
        events = sup.snapshot_diagnostics()
        assert any("no transition" in e.message for e in events)

    def test_apply_sta_freq_fires_actuator_on_change_then_settles(self, tmp_path: Path):
        """P1.2-A.1 (QA soak finding): after a successful actuator
        return, `_current_ap_channel` advances to the target so the
        NEXT poll sees `already_on_target` instead of re-firing.
        Without this fix the observe-only soak emitted
        regenerate_needed=True every 10 s (and P1.2-B would
        restart hostapd in a loop).
        """
        invocations: list[ChannelFollowDecision] = []
        config = SupervisorConfig(state_file=tmp_path / "network-state.json")
        sup = NetworkSupervisor(
            config=config,
            channel_follow_actuator=invocations.append,
        )
        decision = sup.apply_sta_freq(2462)  # channel 11
        assert decision.regenerate_needed is True
        assert decision.target_channel == 11
        assert len(invocations) == 1
        # P1.2-A.1: successful actuation (no exception raised by
        # `list.append`) should advance the supervisor's cached AP
        # channel to the target.
        assert sup.current_ap_channel == 11
        # Second call to same freq is a no-op: `already_on_target`.
        invocations.clear()
        decision2 = sup.apply_sta_freq(2462)
        assert decision2.regenerate_needed is False
        assert "already" in decision2.reason
        assert len(invocations) == 0

    def test_actuator_failure_does_not_advance_cached_ap_channel(self, tmp_path: Path):
        """P1.2-A.1: a failing actuator must NOT advance the
        cached AP channel; the next poll then RE-tries (the
        correct retry-on-failure behavior). For P1.2-B this means
        a failed hostapd-restart causes the supervisor to retry on
        the next STA-freq poll rather than wedging the AP on the
        old channel forever.
        """
        config = SupervisorConfig(state_file=tmp_path / "network-state.json")

        def _failing_actuator(decision: ChannelFollowDecision) -> None:
            raise RuntimeError("simulated hostapd restart failure")

        sup = NetworkSupervisor(
            config=config,
            channel_follow_actuator=_failing_actuator,
        )
        # AP should NOT advance because actuator raised.
        sup.apply_sta_freq(2462)
        assert sup.current_ap_channel is None
        # Subsequent freq poll fires the actuator AGAIN (correct
        # retry behavior; for a real flapping hostapd this would
        # keep retrying until success, with the DEGRADED branch
        # taking over the broader recovery).
        decision = sup.apply_sta_freq(2462)
        assert decision.regenerate_needed is True
        # Warn diagnostic must be in the ring buffer so QA can grep
        # for actuator failures in the journal.
        warn_messages = [
            e.message
            for e in sup.snapshot_diagnostics()
            if e.severity == "warn" and "actuator failed" in e.message
        ]
        assert len(warn_messages) == 2, f"expected 2 warn lines, got {warn_messages}"

    def test_observe_only_default_actuator_advances_simulated_state(self, tmp_path: Path):
        """P1.2-A.1: the OBSERVE-ONLY default actuator returns
        normally (no exception), so `apply_sta_freq` should still
        advance `_current_ap_channel`. The QA-flagged soak issue
        was specifically against this default actuator path.
        """
        config = SupervisorConfig(state_file=tmp_path / "network-state.json")
        sup = NetworkSupervisor(config=config)  # default actuator
        sup.apply_sta_freq(2447)  # channel 8 — QA's pikazo capture
        assert sup.current_ap_channel == 8
        # The very next poll for the same freq is a no-op.
        decision = sup.apply_sta_freq(2447)
        assert decision.regenerate_needed is False

    def test_fallback_mutex_mode_skips_linger(self, tmp_path: Path):
        sup = self._make(tmp_path, fallback_mutex=True)
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)
        # In mutex mode, CONNECTING -> ONLINE directly (no LINGER).
        assert sup.current_state == SupervisorState.ONLINE

    def test_supervisor_to_dict_shape(self, tmp_path: Path):
        sup = self._make(tmp_path)
        d = supervisor_to_dict(sup)
        assert d["state"] == "SETUP"
        assert d["fallback_mutex_mode"] is False
        assert "diagnostics" in d
        assert isinstance(d["diagnostics"], list)


# ============================================================
# P1.3 (2026-06-27) STA_ASSOCIATED -> power_save_actuator re-fire
# ============================================================


class TestPowerSaveRefireOnAssoc:
    """The supervisor fires the configured power_save_actuator on
    every STA_ASSOCIATED event (spec §A#2 — brcmfmac resets
    power_save on reassociation, so the boot one-shot alone is
    insufficient).
    """

    def _make(self, tmp_path: Path, *, actuator):
        config = SupervisorConfig(
            state_file=tmp_path / "network-state.json",
        )
        return NetworkSupervisor(
            config=config,
            power_save_actuator=actuator,
        )

    def test_sta_associated_fires_actuator(self, tmp_path: Path):
        calls = []

        def _fake():
            calls.append(1)

        sup = self._make(tmp_path, actuator=_fake)
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)  # → CONNECTING
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)  # → LINGER
        assert calls == [1]
        # A second STA_ASSOCIATED event (router-side reassoc while in
        # LINGER) MUST re-fire the actuator even though the state
        # machine has no transition for (LINGER, STA_ASSOCIATED).
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)
        assert calls == [1, 1]

    def test_non_assoc_events_do_not_fire_actuator(self, tmp_path: Path):
        calls = []

        def _fake():
            calls.append(1)

        sup = self._make(tmp_path, actuator=_fake)
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        sup.apply_event(SupervisorEvent.STA_AUTH_FAILED)
        sup.apply_event(SupervisorEvent.LINGER_TIMER_EXPIRED)
        sup.apply_event(SupervisorEvent.OPERATOR_REQUESTED_SETUP_MODE)
        sup.apply_event(SupervisorEvent.STA_DISCONNECTED)
        assert calls == []

    def test_actuator_exception_emits_warn_diag_does_not_wedge(self, tmp_path: Path):
        def _boom():
            raise RuntimeError("netctl socket not found")

        sup = self._make(tmp_path, actuator=_boom)
        # Transition still happens despite actuator failure.
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)
        assert sup.current_state == SupervisorState.LINGER
        warn = [
            e
            for e in sup.snapshot_diagnostics()
            if e.severity == "warn"
            and e.source == "power_save"
            and "actuator failed on STA_ASSOCIATED" in e.message
        ]
        assert len(warn) == 1, f"expected one warn diag, got: {warn}"

    def test_observe_only_default_emits_would_fire_diag(self, tmp_path: Path):
        """When no power_save_actuator is passed, the default stub
        emits an info-level 'would re-fire' line so QA can confirm
        the supervisor IS handling STA_ASSOCIATED on hosts without
        the netctl socket."""
        config = SupervisorConfig(state_file=tmp_path / "network-state.json")
        sup = NetworkSupervisor(config=config)  # no actuator → default stub
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)
        info_would = [
            e
            for e in sup.snapshot_diagnostics()
            if e.severity == "info" and e.source == "power_save" and "would re-fire" in e.message
        ]
        assert len(info_would) == 1

    def test_set_power_save_actuator_swaps_at_runtime(self, tmp_path: Path):
        """Mirror of set_channel_follow_actuator: dependencies.py +
        the take-over orchestrator can hot-swap the actuator after
        the supervisor is already running."""
        calls = []

        def _first():
            calls.append("a")

        def _second():
            calls.append("b")

        config = SupervisorConfig(state_file=tmp_path / "network-state.json")
        sup = NetworkSupervisor(config=config, power_save_actuator=_first)
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)
        assert calls == ["a"]
        sup.set_power_save_actuator(_second)
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)
        assert calls == ["a", "b"]

    def test_power_save_fire_log_line_has_supervisor_prefix(self, tmp_path: Path, caplog):
        """Sacred-review NIT #5: QA's grep convention
        `grep '\\[network-supervisor\\]'` MUST also catch the
        power-save fire line. The supervisor's _emit emits that
        prefix for every diagnostic — pin it on this code path so a
        future refactor that bypasses _emit fails loudly.
        """

        def _ok():
            return None

        sup = self._make(tmp_path, actuator=_ok)
        with caplog.at_level("INFO", logger="openmarquee.network_supervisor"):
            sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
            sup.apply_event(SupervisorEvent.STA_ASSOCIATED)
        fire_lines = [
            r.getMessage()
            for r in caplog.records
            if "actuator fired on STA_ASSOCIATED" in r.getMessage()
        ]
        assert len(fire_lines) == 1, f"expected one fire line, got: {fire_lines}"
        line = fire_lines[0]
        assert line.startswith("[network-supervisor]"), line
        assert "source=power_save" in line
        assert "severity=info" in line


# ============================================================
# P2 (2026-06-27) _on_transition AP lifecycle + LINGER timer
# ============================================================


class _RecordingApActuator:
    """Test double exposing the (stop, start) call sequence so
    transition-effect tests can pin which side fired."""

    def __init__(self, *, fail_on: str | None = None):
        self.calls: list[str] = []
        self.fail_on = fail_on

    def stop(self) -> None:
        self.calls.append("stop")
        if self.fail_on == "stop":
            raise RuntimeError("synthetic stop failure")

    def start(self) -> None:
        self.calls.append("start")
        if self.fail_on == "start":
            raise RuntimeError("synthetic start failure")


class TestApLifecycleTransitions:
    """The supervisor's transition-effect dispatch fires
    `ap_lifecycle_actuator.stop()` when entering ONLINE and
    `start()` when exiting it. Spec §"Onboarding state machine":
    ONLINE is the default steady state with the AP torn down.
    """

    def _make(self, tmp_path, *, actuator, fallback_mutex=False):
        config = SupervisorConfig(
            state_file=tmp_path / "network-state.json",
            fallback_mutex_mode=fallback_mutex,
        )
        return NetworkSupervisor(config=config, ap_lifecycle_actuator=actuator)

    def test_linger_to_online_fires_stop(self, tmp_path: Path):
        actuator = _RecordingApActuator()
        sup = self._make(tmp_path, actuator=actuator)
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)  # → LINGER
        sup.apply_event(SupervisorEvent.LINGER_TIMER_EXPIRED)  # → ONLINE
        assert actuator.calls == ["stop"]

    def test_online_to_degraded_fires_start(self, tmp_path: Path):
        actuator = _RecordingApActuator()
        sup = self._make(tmp_path, actuator=actuator)
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)
        sup.apply_event(SupervisorEvent.LINGER_TIMER_EXPIRED)  # → ONLINE (stop)
        sup.apply_event(SupervisorEvent.STA_DISCONNECTED)  # → DEGRADED (start)
        assert actuator.calls == ["stop", "start"]

    def test_online_to_setup_fires_start(self, tmp_path: Path):
        actuator = _RecordingApActuator()
        sup = self._make(tmp_path, actuator=actuator)
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)
        sup.apply_event(SupervisorEvent.LINGER_TIMER_EXPIRED)  # → ONLINE
        sup.apply_event(SupervisorEvent.OPERATOR_REQUESTED_SETUP_MODE)
        assert actuator.calls == ["stop", "start"]

    def test_connecting_to_linger_does_not_fire_actuator(self, tmp_path: Path):
        """AP is up in SETUP + CONNECTING + LINGER + DEGRADED — only
        the ONLINE boundary moves it."""
        actuator = _RecordingApActuator()
        sup = self._make(tmp_path, actuator=actuator)
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)  # → LINGER
        sup.apply_event(SupervisorEvent.STA_DISCONNECTED)  # → DEGRADED
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)  # → LINGER
        assert actuator.calls == []

    def test_mutex_mode_skips_linger_and_fires_stop_on_connecting_to_online(self, tmp_path: Path):
        """In fallback mutex regime, CONNECTING goes directly to
        ONLINE on STA_ASSOCIATED — the stop must fire there."""
        actuator = _RecordingApActuator()
        sup = self._make(tmp_path, actuator=actuator, fallback_mutex=True)
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)  # → ONLINE (mutex)
        assert actuator.calls == ["stop"]

    def test_setup_to_online_does_not_fire_teardown(self, tmp_path: Path):
        """QA cross-lane review (PR2 NIT N1): SETUP_MODE_TIMER_EXPIRED
        edge (SETUP -> ONLINE) is currently unwired but WILL be wired
        in a follow-up PR. When it fires, STA is NOT associated — the
        AP must NOT be torn down or the user is stranded. Lock the
        contract NOW so the future timer wiring is safe by
        construction.
        """
        actuator = _RecordingApActuator()
        sup = self._make(tmp_path, actuator=actuator)
        assert sup.current_state == SupervisorState.SETUP
        sup.apply_event(SupervisorEvent.SETUP_MODE_TIMER_EXPIRED)
        assert sup.current_state == SupervisorState.ONLINE
        # No teardown — STA was not associated.
        assert actuator.calls == []

    def test_stop_actuator_failure_warn_diag_and_state_advances(self, tmp_path: Path):
        """A netctl outage must NOT wedge the state machine. The
        supervisor catches the failure + emits a warn diagnostic
        and the state still transitions to ONLINE."""
        actuator = _RecordingApActuator(fail_on="stop")
        sup = self._make(tmp_path, actuator=actuator)
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)
        sup.apply_event(SupervisorEvent.LINGER_TIMER_EXPIRED)
        assert sup.current_state == SupervisorState.ONLINE
        warn = [
            e
            for e in sup.snapshot_diagnostics()
            if e.severity == "warn"
            and e.source == "ap_lifecycle"
            and "stop actuator failed" in e.message
        ]
        assert len(warn) == 1

    def test_default_observe_only_actuator_emits_would_lines(self, tmp_path: Path):
        """When no actuator is passed, the supervisor wires a stub
        that just emits diagnostic 'would stop/start' lines — useful
        on dev hosts without netctl. The stub is paired with the
        supervisor's ring buffer via a back-pointer."""
        config = SupervisorConfig(state_file=tmp_path / "network-state.json")
        sup = NetworkSupervisor(config=config)
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)
        sup.apply_event(SupervisorEvent.LINGER_TIMER_EXPIRED)
        # 'would stop' line surfaces in the diagnostics ring buffer.
        would_stop = [
            e
            for e in sup.snapshot_diagnostics()
            if e.source == "ap_lifecycle" and "would stop" in e.message
        ]
        assert len(would_stop) == 1

    def test_set_ap_lifecycle_actuator_swaps_at_runtime(self, tmp_path: Path):
        first = _RecordingApActuator()
        second = _RecordingApActuator()
        config = SupervisorConfig(state_file=tmp_path / "network-state.json")
        sup = NetworkSupervisor(config=config, ap_lifecycle_actuator=first)
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)
        sup.apply_event(SupervisorEvent.LINGER_TIMER_EXPIRED)
        assert first.calls == ["stop"]
        sup.set_ap_lifecycle_actuator(second)
        sup.apply_event(SupervisorEvent.STA_DISCONNECTED)  # → DEGRADED (start)
        assert second.calls == ["start"]
        # First actuator was NOT called for the second transition.
        assert first.calls == ["stop"]


class TestLingerTimer:
    """The LINGER timer arms on entry into LINGER + the observe loop
    polls check_linger_timeout() each tick to fire
    LINGER_TIMER_EXPIRED when the grace window elapses.
    """

    def _make(self, tmp_path, *, linger_seconds=120.0):
        config = SupervisorConfig(
            state_file=tmp_path / "network-state.json",
            linger_seconds=linger_seconds,
        )
        return NetworkSupervisor(config=config)

    def test_arm_on_entry_into_linger(self, tmp_path: Path):
        sup = self._make(tmp_path)
        assert sup.linger_entered_at is None
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        assert sup.linger_entered_at is None  # CONNECTING, not LINGER
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)
        assert sup.current_state == SupervisorState.LINGER
        assert sup.linger_entered_at is not None

    def test_disarm_on_exit_from_linger(self, tmp_path: Path):
        sup = self._make(tmp_path)
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)
        assert sup.linger_entered_at is not None
        sup.apply_event(SupervisorEvent.LINGER_TIMER_EXPIRED)  # → ONLINE
        assert sup.linger_entered_at is None

    def test_check_returns_false_outside_linger(self, tmp_path: Path):
        sup = self._make(tmp_path)
        # In SETUP — not LINGER.
        assert sup.check_linger_timeout() is False
        assert sup.check_linger_timeout(now=10_000.0) is False

    def test_check_returns_false_before_window_elapses(self, tmp_path: Path):
        sup = self._make(tmp_path, linger_seconds=120.0)
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)
        ref = sup.linger_entered_at
        assert sup.check_linger_timeout(now=ref + 60.0) is False
        assert sup.check_linger_timeout(now=ref + 119.99) is False

    def test_check_returns_true_after_window_elapses(self, tmp_path: Path):
        sup = self._make(tmp_path, linger_seconds=120.0)
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)
        ref = sup.linger_entered_at
        assert sup.check_linger_timeout(now=ref + 120.0) is True
        assert sup.check_linger_timeout(now=ref + 200.0) is True

    def test_check_idempotent_after_transition_out(self, tmp_path: Path):
        """Once the state machine transitions out of LINGER (e.g. via
        LINGER_TIMER_EXPIRED that the loop drove), subsequent calls
        to check_linger_timeout must return False — even though
        linger_entered_at is cleared, defensive double-firing must
        not occur."""
        sup = self._make(tmp_path, linger_seconds=10.0)
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)
        ref = sup.linger_entered_at
        assert sup.check_linger_timeout(now=ref + 10.0) is True
        # Fire the transition (what the loop would do).
        sup.apply_event(SupervisorEvent.LINGER_TIMER_EXPIRED)
        assert sup.current_state == SupervisorState.ONLINE
        # Subsequent polls return False — state is no longer LINGER,
        # entered_at is cleared.
        assert sup.check_linger_timeout(now=ref + 200.0) is False

    def test_resumed_linger_state_seeds_entered_at(self, tmp_path: Path):
        """Sacred-review BLOCKER fix (PR2): if disk has state=LINGER
        when the supervisor boots, _linger_entered_at MUST seed to
        now() so check_linger_timeout works (otherwise it returns
        False forever + AP stays up indefinitely, contradicting the
        spec's ONLINE-is-steady-state intent).
        """
        # Persist a state file pinned to LINGER.
        state_file = tmp_path / "network-state.json"
        save_persisted_state(
            PersistedState(state=SupervisorState.LINGER),
            path=state_file,
        )
        config = SupervisorConfig(state_file=state_file, linger_seconds=120.0)
        sup = NetworkSupervisor(config=config)
        assert sup.current_state == SupervisorState.LINGER
        assert sup.linger_entered_at is not None
        # check_linger_timeout works: fresh window starts NOW.
        assert sup.check_linger_timeout(now=sup.linger_entered_at + 119.0) is False
        assert sup.check_linger_timeout(now=sup.linger_entered_at + 121.0) is True

    def test_resumed_non_linger_state_does_not_seed_entered_at(self, tmp_path: Path):
        """The seed is gated on state == LINGER — every other
        persisted state leaves _linger_entered_at None."""
        state_file = tmp_path / "network-state.json"
        for s in (
            SupervisorState.SETUP,
            SupervisorState.CONNECTING,
            SupervisorState.ONLINE,
            SupervisorState.DEGRADED,
        ):
            save_persisted_state(PersistedState(state=s), path=state_file)
            config = SupervisorConfig(state_file=state_file)
            sup = NetworkSupervisor(config=config)
            assert sup.current_state == s
            assert sup.linger_entered_at is None, f"state={s} should not seed entered_at"


# ============================================================
# PR3 (2026-06-27) — supervisor system-card publisher on transitions.
# ============================================================


class _RecordingSystemCardPublisher:
    """Recording stub for the SystemCardPublisher contract: exposes
    `.render(params)` and `.clear()` and appends every call to a list
    so tests can assert on the emit sequence."""

    def __init__(self):
        self.render_calls: list[dict] = []
        self.clear_calls: int = 0

    def render(self, params: dict) -> None:
        self.render_calls.append(dict(params))

    def clear(self) -> None:
        self.clear_calls += 1


class TestSystemCardOnTransition:
    """The supervisor's `_on_transition` publishes a RenderSystemCard
    matching the new state OR a ClearSystemCard when the new state
    is ONLINE (spec §"Onboarding state machine": ONLINE = AP off, no
    overlay). Every path to ONLINE must clear."""

    def _make(self, tmp_path: Path, publisher, *, fallback_mutex: bool = False):
        config = SupervisorConfig(
            fallback_mutex_mode=fallback_mutex,
            state_file=tmp_path / "network-state.json",
        )
        return NetworkSupervisor(config=config, system_card_publisher=publisher)

    def test_setup_to_connecting_renders_connecting_card(self, tmp_path: Path):
        pub = _RecordingSystemCardPublisher()
        sup = self._make(tmp_path, pub)
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        assert sup.current_state == SupervisorState.CONNECTING
        kinds = [c.get("kind") for c in pub.render_calls]
        assert kinds == ["CONNECTING"]
        assert pub.clear_calls == 0

    def test_connecting_to_linger_renders_connected_card(self, tmp_path: Path):
        pub = _RecordingSystemCardPublisher()
        sup = self._make(tmp_path, pub)
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)
        assert sup.current_state == SupervisorState.LINGER
        kinds = [c.get("kind") for c in pub.render_calls]
        assert kinds == ["CONNECTING", "CONNECTED"]

    def test_linger_to_online_clears_card(self, tmp_path: Path):
        """Concurrent-regime path to ONLINE — the canonical spec
        path."""
        pub = _RecordingSystemCardPublisher()
        sup = self._make(tmp_path, pub)
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)  # → LINGER
        sup.apply_event(SupervisorEvent.LINGER_TIMER_EXPIRED)  # → ONLINE
        assert sup.current_state == SupervisorState.ONLINE
        assert pub.clear_calls == 1

    def test_connecting_to_online_in_mutex_mode_clears_card(self, tmp_path: Path):
        """Mutex-regime path to ONLINE: CONNECTING skips LINGER and
        goes straight to ONLINE on STA_ASSOCIATED. Card must clear."""
        pub = _RecordingSystemCardPublisher()
        sup = self._make(tmp_path, pub, fallback_mutex=True)
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)  # → CONNECTING
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)  # → ONLINE (mutex)
        assert sup.current_state == SupervisorState.ONLINE
        assert pub.clear_calls == 1

    def test_degraded_to_online_in_mutex_mode_clears_card(self, tmp_path: Path):
        """Mutex-regime DEGRADED reassociates directly to ONLINE. Card
        must clear on that path too."""
        pub = _RecordingSystemCardPublisher()
        sup = self._make(tmp_path, pub, fallback_mutex=True)
        # Get to DEGRADED via CONNECTING -> ONLINE -> STA_DISCONNECTED.
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)  # → ONLINE
        sup.apply_event(SupervisorEvent.STA_DISCONNECTED)  # → DEGRADED
        assert sup.current_state == SupervisorState.DEGRADED
        clears_before = pub.clear_calls
        # Re-associate → back to ONLINE (mutex path).
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)
        assert sup.current_state == SupervisorState.ONLINE
        assert pub.clear_calls == clears_before + 1

    def test_linger_to_degraded_renders_degraded_card(self, tmp_path: Path):
        pub = _RecordingSystemCardPublisher()
        sup = self._make(tmp_path, pub)
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)  # → LINGER
        sup.apply_event(SupervisorEvent.STA_DISCONNECTED)  # → DEGRADED
        assert sup.current_state == SupervisorState.DEGRADED
        # The last render was DEGRADED with variant "lost".
        assert pub.render_calls[-1].get("kind") == "DEGRADED"
        assert pub.render_calls[-1].get("variant") == "lost"

    def test_online_to_degraded_renders_degraded_card(self, tmp_path: Path):
        """ONLINE→DEGRADED must render the DEGRADED card (portal
        recovery path). Also confirms a clear from the earlier
        LINGER→ONLINE happened."""
        pub = _RecordingSystemCardPublisher()
        sup = self._make(tmp_path, pub)
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)  # → LINGER
        sup.apply_event(SupervisorEvent.LINGER_TIMER_EXPIRED)  # → ONLINE
        assert pub.clear_calls == 1
        sup.apply_event(SupervisorEvent.STA_DISCONNECTED)  # → DEGRADED
        assert sup.current_state == SupervisorState.DEGRADED
        assert pub.render_calls[-1].get("kind") == "DEGRADED"

    def test_degraded_variant_defaults_to_lost_when_no_reason_recorded(self, tmp_path: Path):
        """2026-07-01 (audit 4b): the DEGRADED card's variant field
        used to be hard-coded to 'lost'. Now threaded from the last
        STA-level event. When the ONLY reason recorded is a plain
        STA_DISCONNECTED (no AUTH_REJECT ever seen), the variant
        stays 'lost' — matches the pre-fix default."""
        pub = _RecordingSystemCardPublisher()
        sup = self._make(tmp_path, pub)
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)  # → LINGER
        sup.apply_event(SupervisorEvent.LINGER_TIMER_EXPIRED)  # → ONLINE
        sup.apply_event(SupervisorEvent.STA_DISCONNECTED)  # → DEGRADED
        assert sup.current_state == SupervisorState.DEGRADED
        assert pub.render_calls[-1].get("variant") == "lost"

    def test_degraded_variant_carries_auth_fail_when_seen_earlier(
        self,
        tmp_path: Path,
    ):
        """2026-07-01 (audit 4b): if wpa_supplicant emitted an
        AUTH-REJECT (STA_AUTH_FAILED) earlier, that reason must
        propagate to any later DEGRADED render — the operator
        should see 'wrong password' on the wall instead of a
        generic 'wifi lost.'

        NB: the state machine currently routes STA_AUTH_FAILED
        (from CONNECTING) directly to SETUP, NOT DEGRADED. This
        test drives the field through the observable side channel
        (a fresh association + a subsequent disconnect) — the
        _last_degraded_variant field is the persistent hook a
        future PR that adds an ONLINE→DEGRADED-on-auth-fail edge
        would pick up. For now the test guards the plumbing: a
        variant recorded on STA_AUTH_FAILED survives until
        STA_ASSOCIATED clears it or a STA_DISCONNECTED overwrites
        it.
        """
        pub = _RecordingSystemCardPublisher()
        sup = self._make(tmp_path, pub)
        # Simulate an auth-fail during CONNECTING → SETUP.
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)  # → CONNECTING
        sup.apply_event(SupervisorEvent.STA_AUTH_FAILED)  # → SETUP
        assert sup._last_degraded_variant == "auth_fail"
        # A successful association would clear the reason: verify.
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)  # → LINGER (clears)
        assert sup._last_degraded_variant is None

    def test_degraded_variant_resets_to_none_on_successful_reconnect(
        self,
        tmp_path: Path,
    ):
        """STA_ASSOCIATED must clear the last-degraded-variant so a
        later drop doesn't inherit a stale reason (auth_fail from
        weeks ago wouldn't apply to a wall-plug pull today)."""
        pub = _RecordingSystemCardPublisher()
        sup = self._make(tmp_path, pub)
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)  # → LINGER
        sup.apply_event(SupervisorEvent.LINGER_TIMER_EXPIRED)  # → ONLINE
        sup.apply_event(SupervisorEvent.STA_DISCONNECTED)  # → DEGRADED lost
        assert sup._last_degraded_variant == "lost"
        # Reconnect: clears the reason.
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)  # → LINGER
        assert sup._last_degraded_variant is None
        # Fresh drop must NOT inherit 'lost' from before; it re-records
        # 'lost' via the STA_DISCONNECTED handler. Semantically the
        # SAME variant either way, but the important invariant is that
        # the field went through None on reconnect (proven above).

    def test_operator_setup_mode_from_online_renders_setup_card(self, tmp_path: Path):
        """Operator-driven ONLINE→SETUP (Setup Mode re-entry) must
        also render the SETUP card so the sign shows the join QR."""
        pub = _RecordingSystemCardPublisher()
        sup = self._make(tmp_path, pub)
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        sup.apply_event(SupervisorEvent.STA_ASSOCIATED)  # → LINGER
        sup.apply_event(SupervisorEvent.LINGER_TIMER_EXPIRED)  # → ONLINE
        sup.apply_event(SupervisorEvent.OPERATOR_REQUESTED_SETUP_MODE)
        assert sup.current_state == SupervisorState.SETUP
        assert pub.render_calls[-1].get("kind") == "SETUP"

    def test_publisher_failure_is_downgraded_to_warn_diag(self, tmp_path: Path):
        """A publisher that raises must NOT wedge apply_event; the
        transition still lands and a warn diagnostic is emitted."""

        class BustedPublisher:
            def render(self, params: dict) -> None:
                raise RuntimeError("simulated renderer death")

            def clear(self) -> None:
                raise RuntimeError("simulated renderer death")

        sup = self._make(tmp_path, BustedPublisher())
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        assert sup.current_state == SupervisorState.CONNECTING
        # No exception propagated. Diag ring buffer records the warn.
        warns = [
            e
            for e in sup.snapshot_diagnostics()
            if e.severity == "warn"
            and e.source == "system_card"
            and "publisher failed" in e.message
        ]
        assert warns, "expected a system_card warn diagnostic on publisher failure"

    def test_default_stub_records_diagnostic_only(self, tmp_path: Path):
        """When no publisher is injected the default observe-only
        stub emits a diagnostic per transition — so a supervisor
        without a Renderer wired still produces a grep-able trail."""
        config = SupervisorConfig(state_file=tmp_path / "network-state.json")
        sup = NetworkSupervisor(config=config)  # default publisher
        sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        infos = [
            e
            for e in sup.snapshot_diagnostics()
            if e.severity == "info"
            and e.source == "system_card"
            and "would render kind=CONNECTING" in e.message
        ]
        assert infos, "expected a would-render info diagnostic"
