"""Unit tests for openmarquee.wifi_networks_actuator (Phase B1 —
qarl handover 2026-07-03).

Two paths under test:
  * import_existing_wifi_profiles — adopts existing NM wifi profiles
    into the wifi_networks list. Skips the setup-AP.
  * apply_wifi_networks — reconciles NM state to match the list.
    Adds new, updates existing, deletes stale openmarquee-managed
    profiles. NEVER touches profiles it doesn't own.
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
            # args = [nmcli_path, ...cmd...]. Look for the "connection show"
            # arg vector to pick which stdout to return.
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
        imported = wifi_networks_actuator.import_existing_wifi_profiles(ap_ssid="openMarquee-SETUP")
        # Should adopt 3 (NEBULA, qarl, admin) and skip the setup-AP + ethernet.
        assert len(imported) == 3
        ssids = {entry["ssid"] for entry in imported}
        assert ssids == {"NEBULA", "qarl", "admin"}
        psks = {entry["ssid"]: entry["password"] for entry in imported}
        assert psks["NEBULA"] == "nebula-pw-here"
        assert psks["qarl"] == "qarl-pw-here"
        assert psks["admin"] == "admin-pw-here"

    def test_returns_empty_when_nmcli_missing(self, monkeypatch):
        monkeypatch.setattr(
            "openmarquee.wifi_networks_actuator.shutil.which",
            lambda _name: None,
        )
        assert wifi_networks_actuator.import_existing_wifi_profiles() == []

    def test_skips_hidden_or_empty_ssid_profiles(self, monkeypatch):
        list_stdout = "openmarquee-hidden:802-11-wireless\n"
        detail_stdouts = {
            "openmarquee-hidden": "802-11-wireless.ssid:\nconnection.interface-name:wlan0\n",
        }
        self._install_fake_nmcli(monkeypatch, list_stdout, detail_stdouts)
        assert wifi_networks_actuator.import_existing_wifi_profiles() == []


class TestApplyWifiNetworks:
    """The reconcile path: verify add/modify/delete calls fire on
    the right SSIDs and that we never touch a non-openmarquee-owned
    profile."""

    def test_adds_new_network_when_no_matching_profile(self, monkeypatch):
        calls: list[list[str]] = []

        def _fake_run(args, **_kwargs):
            calls.append(list(args[1:]))
            if args[1:6] == ["-t", "-f", "NAME,TYPE", "connection", "show"]:
                return _FakeCompleted(stdout="", returncode=0)
            return _FakeCompleted(returncode=0)

        monkeypatch.setattr("openmarquee.wifi_networks_actuator.subprocess.run", _fake_run)
        monkeypatch.setattr(
            "openmarquee.wifi_networks_actuator.shutil.which",
            lambda name: "/usr/bin/nmcli" if name == "nmcli" else None,
        )
        network = WifiNetworkEntry(ssid="NewWifi", password="new-password-here")
        wifi_networks_actuator.apply_wifi_networks([network])
        # There should be a connection add call.
        assert any("add" in c and "NewWifi" in c for c in calls)

    def test_never_deletes_non_openmarquee_profile(self, monkeypatch):
        """A hand-added `nmcli con add` profile (e.g. `MyHomeWifi`)
        that isn't managed by us must NEVER be deleted, even if
        it isn't in the settings list."""
        calls: list[list[str]] = []

        def _fake_run(args, **_kwargs):
            calls.append(list(args[1:]))
            cmd = list(args[1:])
            if cmd[:5] == ["-t", "-f", "NAME,TYPE", "connection", "show"]:
                return _FakeCompleted(
                    stdout=(
                        "MyHandAddedWifi:802-11-wireless\nopenmarquee-old-wifi:802-11-wireless\n"
                    ),
                    returncode=0,
                )
            if cmd[:3] == ["-t", "-s", "-f"] and "show" in cmd:
                name = cmd[-1]
                if name == "MyHandAddedWifi":
                    return _FakeCompleted(
                        stdout="802-11-wireless.ssid:HomeSSID\nconnection.interface-name:wlan0\n",
                        returncode=0,
                    )
                if name == "openmarquee-old-wifi":
                    return _FakeCompleted(
                        stdout="802-11-wireless.ssid:OldSSID\nconnection.interface-name:wlan0\n",
                        returncode=0,
                    )
                return _FakeCompleted(stdout="", returncode=0)
            return _FakeCompleted(returncode=0)

        monkeypatch.setattr("openmarquee.wifi_networks_actuator.subprocess.run", _fake_run)
        monkeypatch.setattr(
            "openmarquee.wifi_networks_actuator.shutil.which",
            lambda name: "/usr/bin/nmcli" if name == "nmcli" else None,
        )
        wifi_networks_actuator.apply_wifi_networks([])
        # openmarquee-old-wifi may be deleted, but MyHandAddedWifi
        # must NEVER appear in a delete call.
        delete_targets = [c[-1] for c in calls if "delete" in c]
        assert "MyHandAddedWifi" not in delete_targets

    def test_skips_when_nmcli_missing(self, monkeypatch):
        """Dev host without nmcli: no subprocess calls fire."""
        called = {"count": 0}

        def _fake_run(*_a, **_k):
            called["count"] += 1
            return _FakeCompleted(returncode=0)

        monkeypatch.setattr("openmarquee.wifi_networks_actuator.subprocess.run", _fake_run)
        monkeypatch.setattr("openmarquee.wifi_networks_actuator.shutil.which", lambda _n: None)
        wifi_networks_actuator.apply_wifi_networks(
            [WifiNetworkEntry(ssid="X", password="pw-here-1234")]
        )
        assert called["count"] == 0
