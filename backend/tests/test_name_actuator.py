"""Unit tests for openmarquee.name_actuator (Phase B1 —
qarl handover 2026-07-03).

`apply_sign_name` propagates the device name to four consumers via
the netctl socket daemon (2026-07-03 QA FIX 1: NoNewPrivileges on
the backend blocks direct subprocess calls to hostnamectl /
tailscale / systemctl, so the actuator ships payloads over the
already-sanctioned root socket instead):

  1. hostnamectl-set-hostname     (name payload)
  2. tailscale-set-hostname       (name payload)
  3. avahi-write-and-restart      (full rendered conf payload)
  4. hostapd-write-and-restart    (full rendered conf payload —
     REUSES the existing subcommand)

Every sub-actuator must FAIL SOFT so a netctl connect error /
non-zero response leaves the caller unharmed.
"""

from __future__ import annotations

from openmarquee import name_actuator


class _NetctlSpy:
    """Record all _netctl_send calls made through the actuator.
    Each call captured as (subcommand, payload) tuple."""

    def __init__(self):
        self.calls: list[tuple[str, bytes]] = []
        self.raise_error: type[Exception] | None = None

    def _send(self, subcommand, payload, *, timeout_s=None, error_cls=RuntimeError):
        self.calls.append((subcommand, payload))
        if self.raise_error is not None:
            raise error_cls("simulated netctl failure")


def _install_netctl_spy(monkeypatch) -> _NetctlSpy:
    """Patch the `_netctl_send` import in `openmarquee.network_supervisor_actuator`
    (name_actuator does inline `from … import _netctl_send` inside each
    sub-actuator, so we patch the source module)."""
    spy = _NetctlSpy()
    monkeypatch.setattr(
        "openmarquee.network_supervisor_actuator._netctl_send",
        spy._send,
    )
    return spy


class TestHostnamectlPath:
    def test_sends_hostnamectl_set_hostname_via_netctl(self, monkeypatch):
        spy = _install_netctl_spy(monkeypatch)
        name_actuator._apply_hostnamectl("JasonsSign1")
        assert spy.calls == [("hostnamectl-set-hostname", b"JasonsSign1\n")]

    def test_hostnamectl_fail_soft_on_netctl_error(self, monkeypatch):
        spy = _install_netctl_spy(monkeypatch)
        spy.raise_error = RuntimeError
        # Must not raise even when the crossing fails.
        name_actuator._apply_hostnamectl("JasonsSign1")
        assert spy.calls == [("hostnamectl-set-hostname", b"JasonsSign1\n")]


class TestTailscalePath:
    def test_sends_tailscale_set_hostname_via_netctl(self, monkeypatch):
        spy = _install_netctl_spy(monkeypatch)
        name_actuator._apply_tailscale_hostname("JasonsSign1")
        assert spy.calls == [("tailscale-set-hostname", b"JasonsSign1\n")]

    def test_tailscale_fail_soft_on_netctl_error(self, monkeypatch):
        spy = _install_netctl_spy(monkeypatch)
        spy.raise_error = RuntimeError
        name_actuator._apply_tailscale_hostname("JasonsSign1")
        assert spy.calls == [("tailscale-set-hostname", b"JasonsSign1\n")]


class TestAvahiPath:
    """The avahi sub-actuator reads the existing conf, rewrites the
    host-name line locally, and ships the rewritten conf as payload."""

    def test_no_op_when_conf_missing(self, monkeypatch, tmp_path):
        spy = _install_netctl_spy(monkeypatch)
        monkeypatch.setattr(
            "openmarquee.name_actuator._AVAHI_CONF",
            tmp_path / "avahi-daemon.conf",  # doesn't exist
        )
        name_actuator._apply_avahi_hostname("JasonsSign1")
        assert spy.calls == []

    def test_rewrites_and_ships_full_conf(self, monkeypatch, tmp_path):
        spy = _install_netctl_spy(monkeypatch)
        conf = tmp_path / "avahi-daemon.conf"
        conf.write_text("[server]\n#host-name=openmarquee\ndomain-name=local\n")
        monkeypatch.setattr("openmarquee.name_actuator._AVAHI_CONF", conf)
        name_actuator._apply_avahi_hostname("JasonsSign1")
        assert len(spy.calls) == 1
        subcommand, payload = spy.calls[0]
        assert subcommand == "avahi-write-and-restart"
        text = payload.decode("utf-8")
        assert "host-name=JasonsSign1" in text
        assert "[server]" in text
        assert "domain-name=local" in text
        assert "#host-name=" not in text

    def test_no_ship_when_value_unchanged(self, monkeypatch, tmp_path):
        """When the conf already has the target host-name, don't
        make a netctl round-trip (avoids a spurious avahi restart)."""
        spy = _install_netctl_spy(monkeypatch)
        conf = tmp_path / "avahi-daemon.conf"
        conf.write_text("[server]\nhost-name=JasonsSign1\n")
        monkeypatch.setattr("openmarquee.name_actuator._AVAHI_CONF", conf)
        name_actuator._apply_avahi_hostname("JasonsSign1")
        assert spy.calls == []

    def test_avahi_fail_soft_on_netctl_error(self, monkeypatch, tmp_path):
        spy = _install_netctl_spy(monkeypatch)
        spy.raise_error = RuntimeError
        conf = tmp_path / "avahi-daemon.conf"
        conf.write_text("[server]\nhost-name=old\n")
        monkeypatch.setattr("openmarquee.name_actuator._AVAHI_CONF", conf)
        # Must not raise.
        name_actuator._apply_avahi_hostname("JasonsSign1")


class TestHostapdPath:
    """The hostapd sub-actuator reads the existing conf, rewrites the
    ssid= line locally, then ships via the existing (pre-Phase-B1)
    hostapd-write-and-restart netctl subcommand."""

    def test_no_op_when_conf_missing(self, monkeypatch, tmp_path):
        spy = _install_netctl_spy(monkeypatch)
        monkeypatch.setattr(
            "openmarquee.name_actuator._HOSTAPD_CONF",
            tmp_path / "hostapd.conf",  # doesn't exist
        )
        name_actuator._apply_hostapd_ssid("JasonsSign1")
        assert spy.calls == []

    def test_rewrites_and_ships_via_existing_subcommand(self, monkeypatch, tmp_path):
        spy = _install_netctl_spy(monkeypatch)
        conf = tmp_path / "hostapd.conf"
        conf.write_text("interface=ap0\nssid=openMarquee-SETUP\nchannel=6\n")
        monkeypatch.setattr("openmarquee.name_actuator._HOSTAPD_CONF", conf)
        name_actuator._apply_hostapd_ssid("JasonsSign1")
        assert len(spy.calls) == 1
        subcommand, payload = spy.calls[0]
        # 2026-07-03 (QA FIX 1): reuse the EXISTING subcommand — do
        # NOT add a fresh "hostapd-set-ssid" one.
        assert subcommand == "hostapd-write-and-restart"
        text = payload.decode("utf-8")
        assert "ssid=JasonsSign1" in text
        assert "interface=ap0" in text
        assert "channel=6" in text

    def test_no_ship_when_ssid_unchanged(self, monkeypatch, tmp_path):
        spy = _install_netctl_spy(monkeypatch)
        conf = tmp_path / "hostapd.conf"
        conf.write_text("interface=ap0\nssid=JasonsSign1\n")
        monkeypatch.setattr("openmarquee.name_actuator._HOSTAPD_CONF", conf)
        name_actuator._apply_hostapd_ssid("JasonsSign1")
        assert spy.calls == []

    def test_ap_ssid_clamped_to_32_octets(self, monkeypatch, tmp_path):
        """2026-07-03 (QA HARDEN A): hostapd's `ssid=` line is capped at
        32 octets per 802.11. sign_name can be up to 63 chars (RFC 1123
        hostname). A longer sign_name must be TRUNCATED before the AP
        SSID rewrite so hostapd doesn't fail-to-restart and take the
        recovery-AP down.

        The hostname / Tailscale / mDNS consumers keep the full name;
        only the AP SSID clamp is scoped to the hostapd sub-actuator."""
        spy = _install_netctl_spy(monkeypatch)
        conf = tmp_path / "hostapd.conf"
        conf.write_text("interface=ap0\nssid=openMarquee-SETUP\nchannel=6\n")
        monkeypatch.setattr("openmarquee.name_actuator._HOSTAPD_CONF", conf)
        # 40 chars, well over the 32-octet cap.
        long_name = "A-Really-Long-Sign-Name-That-Overflows-XY"
        assert len(long_name) == 41
        name_actuator._apply_hostapd_ssid(long_name)
        assert len(spy.calls) == 1
        _, payload = spy.calls[0]
        text = payload.decode("utf-8")
        # The AP SSID line MUST be present, truncated to 32 chars.
        expected_clamped = long_name[:32]
        assert len(expected_clamped) == 32
        assert f"ssid={expected_clamped}\n" in text
        # And the FULL untruncated name must NOT appear anywhere.
        assert long_name not in text

    def test_exactly_32_chars_not_clamped(self, monkeypatch, tmp_path):
        """Boundary check: a 32-octet sign_name is exactly at the cap
        and MUST NOT be truncated."""
        spy = _install_netctl_spy(monkeypatch)
        conf = tmp_path / "hostapd.conf"
        conf.write_text("interface=ap0\nssid=old\n")
        monkeypatch.setattr("openmarquee.name_actuator._HOSTAPD_CONF", conf)
        boundary_name = "X" * 32
        name_actuator._apply_hostapd_ssid(boundary_name)
        _, payload = spy.calls[0]
        assert f"ssid={boundary_name}\n" in payload.decode("utf-8")


class TestApplySignName:
    """The top-level orchestrator calls all four sub-actuators
    regardless of individual failures."""

    def test_all_four_sub_actuators_run(self, monkeypatch):
        called: list[str] = []
        monkeypatch.setattr(
            "openmarquee.name_actuator._apply_hostnamectl",
            lambda name: called.append(f"hostnamectl:{name}"),
        )
        monkeypatch.setattr(
            "openmarquee.name_actuator._apply_tailscale_hostname",
            lambda name: called.append(f"tailscale:{name}"),
        )
        monkeypatch.setattr(
            "openmarquee.name_actuator._apply_avahi_hostname",
            lambda name: called.append(f"avahi:{name}"),
        )
        monkeypatch.setattr(
            "openmarquee.name_actuator._apply_hostapd_ssid",
            lambda name: called.append(f"hostapd:{name}"),
        )
        name_actuator.apply_sign_name("JasonsSign1")
        assert called == [
            "hostnamectl:JasonsSign1",
            "tailscale:JasonsSign1",
            "avahi:JasonsSign1",
            "hostapd:JasonsSign1",
        ]


class TestReconcileHostapdSsidAtBoot:
    """2026-07-07: boot-time reconcile of the setup-AP SSID from the
    CURRENT hostname, so an out-of-band rename (hostnamectl direct,
    bypassing the settings-PUT name-change flow — as on the
    fireplaceSign -> JasonsSign1 rename) doesn't leave hostapd
    broadcasting the old name."""

    def test_reconciles_drifted_ssid_to_current_hostname(self, monkeypatch, tmp_path):
        # The core regression: hostapd stuck on the OLD name while the
        # hostname is the new name -> reconcile rewrites it.
        spy = _install_netctl_spy(monkeypatch)
        conf = tmp_path / "hostapd.conf"
        conf.write_text("interface=ap0\nssid=fireplaceSign\nchannel=6\n")
        monkeypatch.setattr("openmarquee.name_actuator._HOSTAPD_CONF", conf)
        monkeypatch.setattr(name_actuator.socket, "gethostname", lambda: "JasonsSign1")
        name_actuator.reconcile_hostapd_ssid_at_boot()
        assert len(spy.calls) == 1
        subcommand, payload = spy.calls[0]
        assert subcommand == "hostapd-write-and-restart"
        assert "ssid=JasonsSign1" in payload.decode("utf-8")

    def test_idempotent_when_ssid_already_matches_hostname(self, monkeypatch, tmp_path):
        spy = _install_netctl_spy(monkeypatch)
        conf = tmp_path / "hostapd.conf"
        conf.write_text("interface=ap0\nssid=JasonsSign1\n")
        monkeypatch.setattr("openmarquee.name_actuator._HOSTAPD_CONF", conf)
        monkeypatch.setattr(name_actuator.socket, "gethostname", lambda: "JasonsSign1")
        name_actuator.reconcile_hostapd_ssid_at_boot()
        assert spy.calls == []  # no rewrite/restart when already correct

    def test_strips_domain_suffix_from_hostname(self, monkeypatch, tmp_path):
        spy = _install_netctl_spy(monkeypatch)
        conf = tmp_path / "hostapd.conf"
        conf.write_text("ssid=old\n")
        monkeypatch.setattr("openmarquee.name_actuator._HOSTAPD_CONF", conf)
        monkeypatch.setattr(name_actuator.socket, "gethostname", lambda: "JasonsSign1.local")
        name_actuator.reconcile_hostapd_ssid_at_boot()
        assert "ssid=JasonsSign1" in spy.calls[0][1].decode("utf-8")

    def test_noop_on_empty_hostname(self, monkeypatch, tmp_path):
        spy = _install_netctl_spy(monkeypatch)
        conf = tmp_path / "hostapd.conf"
        conf.write_text("ssid=old\n")
        monkeypatch.setattr("openmarquee.name_actuator._HOSTAPD_CONF", conf)
        monkeypatch.setattr(name_actuator.socket, "gethostname", lambda: "")
        name_actuator.reconcile_hostapd_ssid_at_boot()
        assert spy.calls == []
