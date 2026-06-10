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
        # bumps can detect old-shape files + migrate (sacred review
        # NIT #2; per docs/onboarding-rework-plan.md §D.1).
        assert loaded.schema_version == 1
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
