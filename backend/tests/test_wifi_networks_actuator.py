"""Unit tests for openmarquee.wifi_networks_actuator (Phase B1 —
qarl handover 2026-07-03).

Two paths under test:
  * import_existing_wifi_profiles — adopts existing NM wifi profiles
    into the wifi_networks list. Skips the setup-AP. Read-only.
  * apply_wifi_networks — reconciles NM state to match the list.
    2026-07-03 QA FIX 1: writes (add/modify/delete) now route
    through the netctl socket daemon rather than direct nmcli
    subprocess calls (blocked by NoNewPrivileges).

Tests spy on:
  * subprocess.run inside wifi_networks_actuator for READ probes
    (list, detail, GENERAL.STATE) — these stay unprivileged and
    unmediated.
  * _netctl_send inside network_supervisor_actuator for WRITE ops
    (add/modify/delete) — these went through the daemon after QA
    FIX 1.
"""

from __future__ import annotations

from openmarquee import wifi_networks_actuator
from openmarquee.settings import WifiNetworkEntry


class _FakeCompleted:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.args: list[str] = []


class _NetctlSpy:
    """Record every _netctl_send(...) call. Each entry:
    (subcommand, payload_bytes)
    """

    def __init__(self):
        self.calls: list[tuple[str, bytes]] = []
        self.raise_error: type[Exception] | None = None

    def _send(self, subcommand, payload, *, timeout_s=None, error_cls=RuntimeError):
        self.calls.append((subcommand, payload))
        if self.raise_error is not None:
            raise error_cls("simulated netctl failure")


def _install_netctl_spy(monkeypatch) -> _NetctlSpy:
    spy = _NetctlSpy()
    monkeypatch.setattr(
        "openmarquee.network_supervisor_actuator._netctl_send",
        spy._send,
    )
    return spy


class TestSplitTerseRow:
    def test_plain_row(self):
        assert wifi_networks_actuator._split_terse_row("a:b:c") == ["a", "b", "c"]

    def test_escaped_colon_survives(self):
        # nmcli escapes `:` in values with `\:`.
        assert wifi_networks_actuator._split_terse_row(r"openmarquee-x:wifi:Home\:Router") == [
            "openmarquee-x",
            "wifi",
            "Home:Router",
        ]

    def test_empty_trailing_column_kept(self):
        assert wifi_networks_actuator._split_terse_row("wlan0:wifi:") == ["wlan0", "wifi", ""]


class TestIsSetupApRow:
    def test_matches_by_name_prefix(self):
        assert wifi_networks_actuator._is_setup_ap_row(
            name="openmarquee-SETUP-abc", iface="wlan0", ssid="anything", ap_ssid=None
        )

    def test_matches_by_iface(self):
        assert wifi_networks_actuator._is_setup_ap_row(
            name="openmarquee-mgmt-wifi", iface="ap0", ssid="whatever", ap_ssid=None
        )

    def test_matches_by_ssid_equals_ap_ssid(self):
        assert wifi_networks_actuator._is_setup_ap_row(
            name="openmarquee-mgmt-wifi",
            iface="wlan0",
            ssid="openMarquee-SETUP",
            ap_ssid="openMarquee-SETUP",
        )

    def test_ordinary_wifi_profile_is_not_setup_ap(self):
        assert not wifi_networks_actuator._is_setup_ap_row(
            name="openmarquee-mgmt-wifi",
            iface="wlan0",
            ssid="qarl",
            ap_ssid="openMarquee-SETUP",
        )


class TestImportExistingWifiProfiles:
    """Simulate the Jason device: three openmarquee-*-wifi profiles
    (NEBULA, qarl, admin) + the setup-AP + an unrelated ethernet
    connection. The importer must adopt the 3 wifi profiles + skip
    the setup-AP + skip the ethernet."""

    def _install_fake_nmcli(self, monkeypatch, list_stdout: str, detail_stdouts: dict[str, str]):
        """Monkey-patch subprocess.run inside wifi_networks_actuator
        with a scripted response set.
          * `nmcli -t -f NAME,TYPE connection show` returns list_stdout.
          * `nmcli … connection show <name>` returns detail_stdouts[name].
        """

        def _fake_run(args, **_kwargs):
            cmd = list(args[1:])
            if cmd[:5] == ["-t", "-f", "NAME,TYPE", "connection", "show"]:
                return _FakeCompleted(stdout=list_stdout, returncode=0)
            # Detail query is `-t -s -f <fields> connection show <name>`.
            if cmd[:3] == ["-t", "-s", "-f"] and "connection" in cmd and "show" in cmd:
                name = cmd[-1]
                return _FakeCompleted(stdout=detail_stdouts.get(name, ""), returncode=0)
            return _FakeCompleted(returncode=1, stderr=f"unexpected cmd: {cmd}")

        monkeypatch.setattr("openmarquee.wifi_networks_actuator.subprocess.run", _fake_run)
        monkeypatch.setattr(
            "openmarquee.wifi_networks_actuator.shutil.which",
            lambda name: "/usr/bin/nmcli" if name == "nmcli" else None,
        )

    def test_adopts_jason_device_three_wifi_profiles(self, monkeypatch):
        list_stdout = (
            "openmarquee-sign-wifi:802-11-wireless\n"
            "openmarquee-mgmt-wifi:802-11-wireless\n"
            "openmarquee-admin-wifi:802-11-wireless\n"
            "openmarquee-SETUP-A7F:802-11-wireless\n"
            "Wired connection 1:802-3-ethernet\n"
        )
        detail_stdouts = {
            "openmarquee-sign-wifi": (
                "802-11-wireless.ssid:NEBULA\n"
                "connection.interface-name:wlan0\n"
                "802-11-wireless-security.psk:nebula-pw-here\n"
            ),
            "openmarquee-mgmt-wifi": (
                "802-11-wireless.ssid:qarl\n"
                "connection.interface-name:wlan0\n"
                "802-11-wireless-security.psk:qarl-pw-here\n"
            ),
            "openmarquee-admin-wifi": (
                "802-11-wireless.ssid:admin\n"
                "connection.interface-name:wlan0\n"
                "802-11-wireless-security.psk:admin-pw-here\n"
            ),
            "openmarquee-SETUP-A7F": (
                "802-11-wireless.ssid:openMarquee-SETUP\nconnection.interface-name:ap0\n"
            ),
        }
        self._install_fake_nmcli(monkeypatch, list_stdout, detail_stdouts)
        probe_ok, imported = wifi_networks_actuator.import_existing_wifi_profiles(
            ap_ssid="openMarquee-SETUP"
        )
        assert probe_ok is True
        assert len(imported) == 3
        ssids = {entry["ssid"] for entry in imported}
        assert ssids == {"NEBULA", "qarl", "admin"}
        psks = {entry["ssid"]: entry["password"] for entry in imported}
        assert psks["NEBULA"] == "nebula-pw-here"
        assert psks["qarl"] == "qarl-pw-here"
        assert psks["admin"] == "admin-pw-here"

    def test_returns_probe_failed_when_nmcli_missing(self, monkeypatch):
        """2026-07-03 (QA HARDEN B): nmcli-missing is a probe
        failure, not a success-with-no-profiles. Caller must be
        able to tell them apart so a transient nmcli failure
        doesn't flip `wifi_networks_seeded_from_nm`."""
        monkeypatch.setattr(
            "openmarquee.wifi_networks_actuator.shutil.which",
            lambda _name: None,
        )
        probe_ok, imported = wifi_networks_actuator.import_existing_wifi_profiles()
        assert probe_ok is False
        assert imported == []

    def test_skips_hidden_or_empty_ssid_profiles(self, monkeypatch):
        list_stdout = "openmarquee-hidden:802-11-wireless\n"
        detail_stdouts = {
            "openmarquee-hidden": "802-11-wireless.ssid:\nconnection.interface-name:wlan0\n",
        }
        self._install_fake_nmcli(monkeypatch, list_stdout, detail_stdouts)
        probe_ok, imported = wifi_networks_actuator.import_existing_wifi_profiles()
        # Probe DID succeed — no profiles matched the filter, but nmcli
        # responded cleanly. This is the "genuinely no wifi profiles"
        # case QA HARDEN B distinguishes from a transient failure.
        assert probe_ok is True
        assert imported == []

    def test_colon_in_psk_round_trips(self, monkeypatch):
        """2026-07-03 (QA FIX 3): a PSK containing a `:` must
        round-trip through the terse-detail parser without
        corruption. Before the fix, `.split(':',1)` dropped the
        second colon-fragment; after the fix, `_split_terse_row`
        (nmcli-escape-aware) preserves the whole value.

        The detail row nmcli emits for a PSK with an embedded colon
        will show it escaped as `\\:`; the parser must un-escape it
        cleanly. Test both the escaped-colon case + a leading-space
        variant (a PSK is allowed to start with printable-ASCII
        whitespace and MUST NOT be stripped)."""
        weird_psk = "P@ss:with:colons"
        weird_ssid = r"Cafe:Wifi"
        # nmcli terse detail rows escape `:` and `\` in values.
        escaped_psk = weird_psk.replace(":", r"\:")
        escaped_ssid = weird_ssid.replace(":", r"\:")
        list_stdout = "openmarquee-cafe:802-11-wireless\n"
        detail_stdouts = {
            "openmarquee-cafe": (
                f"802-11-wireless.ssid:{escaped_ssid}\n"
                f"connection.interface-name:wlan0\n"
                f"802-11-wireless-security.psk:{escaped_psk}\n"
            ),
        }
        self._install_fake_nmcli(monkeypatch, list_stdout, detail_stdouts)
        probe_ok, imported = wifi_networks_actuator.import_existing_wifi_profiles()
        assert probe_ok is True
        assert len(imported) == 1
        assert imported[0]["ssid"] == weird_ssid
        assert imported[0]["password"] == weird_psk


class TestApplyWifiNetworks:
    """The reconcile path: verify add/modify/delete calls route
    through netctl AND that we never touch a non-openmarquee-owned
    profile OR an activated connection."""

    def _install_read_only_nmcli(
        self,
        monkeypatch,
        *,
        list_stdout: str,
        detail_stdouts: dict[str, str] | None = None,
        state_stdouts: dict[str, str] | None = None,
    ):
        """Fake subprocess.run for the READ probes only: list,
        detail, and GENERAL.STATE. Write ops go through _netctl_send
        (spied separately via _install_netctl_spy)."""
        detail_stdouts = detail_stdouts or {}
        state_stdouts = state_stdouts or {}

        def _fake_run(args, **_kwargs):
            cmd = list(args[1:])
            if cmd[:5] == ["-t", "-f", "NAME,TYPE", "connection", "show"]:
                return _FakeCompleted(stdout=list_stdout, returncode=0)
            if cmd[:3] == ["-t", "-s", "-f"] and "show" in cmd:
                name = cmd[-1]
                return _FakeCompleted(stdout=detail_stdouts.get(name, ""), returncode=0)
            if cmd[:5] == ["-t", "-f", "GENERAL.STATE", "connection", "show"]:
                # `nmcli -t -f GENERAL.STATE connection show <name>`
                name = cmd[-1]
                return _FakeCompleted(stdout=state_stdouts.get(name, ""), returncode=0)
            return _FakeCompleted(returncode=0)

        monkeypatch.setattr("openmarquee.wifi_networks_actuator.subprocess.run", _fake_run)
        monkeypatch.setattr(
            "openmarquee.wifi_networks_actuator.shutil.which",
            lambda name: "/usr/bin/nmcli" if name == "nmcli" else None,
        )

    def test_adds_new_network_via_netctl(self, monkeypatch):
        spy = _install_netctl_spy(monkeypatch)
        self._install_read_only_nmcli(monkeypatch, list_stdout="")
        network = WifiNetworkEntry(ssid="NewWifi", password="new-password-here")
        wifi_networks_actuator.apply_wifi_networks([network])
        # Should see BOTH add-wifi AND modify-wifi crossings (add
        # first, then re-run modify to land autoconnect + priority).
        subcommands = [c[0] for c in spy.calls]
        assert "nm-connection-add-wifi" in subcommands
        assert "nm-connection-modify-wifi" in subcommands
        # Payload for the add crossing must contain the con-name +
        # SSID + PSK on their own lines.
        add_payload = next(
            payload for (sub, payload) in spy.calls if sub == "nm-connection-add-wifi"
        )
        add_text = add_payload.decode("utf-8")
        assert "openmarquee-NewWifi\n" in add_text
        assert "NewWifi\n" in add_text
        assert "new-password-here\n" in add_text

    def test_never_deletes_non_openmarquee_profile(self, monkeypatch):
        """A hand-added `nmcli con add` profile (e.g.
        `MyHandAddedWifi`) that isn't managed by us must NEVER be
        deleted, even if it isn't in the settings list."""
        spy = _install_netctl_spy(monkeypatch)
        self._install_read_only_nmcli(
            monkeypatch,
            list_stdout=("MyHandAddedWifi:802-11-wireless\nopenmarquee-old-wifi:802-11-wireless\n"),
            detail_stdouts={
                "MyHandAddedWifi": (
                    "802-11-wireless.ssid:HomeSSID\nconnection.interface-name:wlan0\n"
                ),
                "openmarquee-old-wifi": (
                    "802-11-wireless.ssid:OldSSID\nconnection.interface-name:wlan0\n"
                ),
            },
            state_stdouts={
                "openmarquee-old-wifi": "GENERAL.STATE:",  # empty → not activated
            },
        )
        wifi_networks_actuator.apply_wifi_networks([])
        # openmarquee-old-wifi may be deleted, but MyHandAddedWifi
        # must NEVER appear in a delete-crossing payload.
        delete_payloads = [
            payload.decode("utf-8") for (sub, payload) in spy.calls if sub == "nm-connection-delete"
        ]
        for payload in delete_payloads:
            assert "MyHandAddedWifi" not in payload

    def test_never_deletes_active_connection(self, monkeypatch):
        """2026-07-03 (QA FIX 2 required test): a profile whose
        `GENERAL.STATE` is `activated` must NEVER be deleted, even
        when it's openmarquee-owned + absent from the settings
        list. Blowing up the ACTIVE uplink is the drop-NEBULA
        landmine we're guarding against.

        Two openmarquee-* profiles exist; both are absent from the
        settings list; one is ACTIVATED, the other is not. Only
        the non-activated one may be deleted."""
        spy = _install_netctl_spy(monkeypatch)
        self._install_read_only_nmcli(
            monkeypatch,
            list_stdout=(
                "openmarquee-active-wifi:802-11-wireless\nopenmarquee-idle-wifi:802-11-wireless\n"
            ),
            detail_stdouts={
                "openmarquee-active-wifi": (
                    "802-11-wireless.ssid:ActiveSSID\nconnection.interface-name:wlan0\n"
                ),
                "openmarquee-idle-wifi": (
                    "802-11-wireless.ssid:IdleSSID\nconnection.interface-name:wlan0\n"
                ),
            },
            state_stdouts={
                "openmarquee-active-wifi": "GENERAL.STATE:activated\n",
                "openmarquee-idle-wifi": "GENERAL.STATE:\n",
            },
        )
        wifi_networks_actuator.apply_wifi_networks([])
        delete_payloads = [
            payload.decode("utf-8") for (sub, payload) in spy.calls if sub == "nm-connection-delete"
        ]
        # The idle profile SHOULD be deleted; the active profile
        # MUST NOT.
        joined = "".join(delete_payloads)
        assert "openmarquee-idle-wifi" in joined
        assert "openmarquee-active-wifi" not in joined

    def test_state_probe_failure_treated_as_activated(self, monkeypatch):
        """Fail-safe posture (comment on _is_connection_activated):
        when the GENERAL.STATE probe returns non-zero / unparseable
        output, treat the connection as activated to skip the
        delete. Better to leave a stale profile than accidentally
        kill the uplink."""
        spy = _install_netctl_spy(monkeypatch)

        def _fake_run(args, **_kwargs):
            cmd = list(args[1:])
            if cmd[:5] == ["-t", "-f", "NAME,TYPE", "connection", "show"]:
                return _FakeCompleted(
                    stdout="openmarquee-mystery-wifi:802-11-wireless\n", returncode=0
                )
            if cmd[:3] == ["-t", "-s", "-f"] and "show" in cmd:
                return _FakeCompleted(
                    stdout="802-11-wireless.ssid:MysterySSID\nconnection.interface-name:wlan0\n",
                    returncode=0,
                )
            if cmd[:5] == ["-t", "-f", "GENERAL.STATE", "connection", "show"]:
                # Simulate a probe failure.
                return _FakeCompleted(returncode=1, stderr="unknown connection")
            return _FakeCompleted(returncode=0)

        monkeypatch.setattr("openmarquee.wifi_networks_actuator.subprocess.run", _fake_run)
        monkeypatch.setattr(
            "openmarquee.wifi_networks_actuator.shutil.which",
            lambda name: "/usr/bin/nmcli" if name == "nmcli" else None,
        )
        wifi_networks_actuator.apply_wifi_networks([])
        delete_calls = [c for c in spy.calls if c[0] == "nm-connection-delete"]
        assert delete_calls == []

    def test_skips_when_nmcli_missing(self, monkeypatch):
        """Dev host without nmcli: no netctl calls fire either."""
        spy = _install_netctl_spy(monkeypatch)
        monkeypatch.setattr("openmarquee.wifi_networks_actuator.shutil.which", lambda _n: None)
        wifi_networks_actuator.apply_wifi_networks(
            [WifiNetworkEntry(ssid="X", password="pw-here-1234")]
        )
        assert spy.calls == []

    def test_reconcile_noop_on_probe_failure(self, monkeypatch):
        """2026-07-03 (QA HARDEN B v2, F5): if the enumerate probe
        fails (`_list_nm_wifi_connections` returns `(False, [])`),
        the reconcile MUST be a no-op — no add / modify / delete
        crossings fire.

        Motivation: without this guard, `existing=[]` from a
        transient failure would make every wanted SSID look
        un-provisioned + trigger `_apply_add`, producing duplicate
        `openmarquee-<ssid>` profiles on the device. Better to leave
        the previous reconcile in place + retry on next PUT.
        """
        spy = _install_netctl_spy(monkeypatch)

        def _fake_run(args, **_kwargs):
            cmd = list(args[1:])
            if cmd[:5] == ["-t", "-f", "NAME,TYPE", "connection", "show"]:
                # Simulate a probe failure.
                return _FakeCompleted(returncode=1, stderr="transient error")
            return _FakeCompleted(returncode=0)

        monkeypatch.setattr("openmarquee.wifi_networks_actuator.subprocess.run", _fake_run)
        monkeypatch.setattr(
            "openmarquee.wifi_networks_actuator.shutil.which",
            lambda name: "/usr/bin/nmcli" if name == "nmcli" else None,
        )
        wifi_networks_actuator.apply_wifi_networks(
            [
                WifiNetworkEntry(ssid="NEBULA", password="nebula-pw-here"),
                WifiNetworkEntry(ssid="qarl", password="qarl-pw-here"),
            ]
        )
        # NO netctl crossings — the reconcile bailed out on the probe
        # failure before hitting the upsert or delete loops.
        assert spy.calls == []

    def test_modify_fires_on_existing_ssid_match(self, monkeypatch):
        """When a profile with the same SSID already exists, the
        reconcile MODIFIES it (preserves the existing con-name),
        rather than delete+recreate — matches the `openmarquee-
        sign-wifi` happy path that QA called out as good behavior."""
        spy = _install_netctl_spy(monkeypatch)
        self._install_read_only_nmcli(
            monkeypatch,
            list_stdout="openmarquee-sign-wifi:802-11-wireless\n",
            detail_stdouts={
                "openmarquee-sign-wifi": (
                    "802-11-wireless.ssid:NEBULA\nconnection.interface-name:wlan0\n"
                    "802-11-wireless-security.psk:old-pw\n"
                ),
            },
        )
        network = WifiNetworkEntry(ssid="NEBULA", password="new-pw-1234")
        wifi_networks_actuator.apply_wifi_networks([network])
        subcommands = [c[0] for c in spy.calls]
        # NO add call — modify only.
        assert "nm-connection-add-wifi" not in subcommands
        assert "nm-connection-modify-wifi" in subcommands
        # The modify payload targets the existing con-name (NOT
        # openmarquee-NEBULA).
        modify_payload = next(
            payload for (sub, payload) in spy.calls if sub == "nm-connection-modify-wifi"
        )
        modify_text = modify_payload.decode("utf-8")
        assert "openmarquee-sign-wifi\n" in modify_text
        assert "NEBULA\n" in modify_text
        assert "new-pw-1234\n" in modify_text

    def test_add_failure_fail_soft(self, monkeypatch):
        """A netctl-side error on add is logged + swallowed — the
        reconcile continues with the next network."""
        spy = _install_netctl_spy(monkeypatch)
        spy.raise_error = RuntimeError
        self._install_read_only_nmcli(monkeypatch, list_stdout="")
        # Must NOT raise.
        wifi_networks_actuator.apply_wifi_networks(
            [WifiNetworkEntry(ssid="Foo", password="foopw-1234")]
        )
