"""Unit tests for openmarquee.name_actuator (Phase B1 —
qarl handover 2026-07-03).

`apply_sign_name` propagates the device name to four consumers:
hostnamectl, tailscale, avahi.conf + restart, hostapd.conf + restart.
Every sub-actuator must FAIL SOFT so a missing binary / missing
conf file / non-zero exit leaves the caller unharmed."""

from __future__ import annotations

from openmarquee import name_actuator


class _FakeCompleted:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class TestFailSoftPaths:
    """Every sub-actuator must be a no-op (never raise) when its
    dependency isn't available on the host."""

    def test_hostnamectl_no_op_when_binary_missing(self, monkeypatch):
        called = {"n": 0}

        def _fake_run(*_a, **_k):
            called["n"] += 1
            return _FakeCompleted()

        monkeypatch.setattr("openmarquee.name_actuator.subprocess.run", _fake_run)
        monkeypatch.setattr("openmarquee.name_actuator.shutil.which", lambda _n: None)
        # Must not raise.
        name_actuator._apply_hostnamectl("JasonsSign1")
        # subprocess.run was never invoked.
        assert called["n"] == 0

    def test_tailscale_no_op_when_binary_missing(self, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(
            "openmarquee.name_actuator.subprocess.run",
            lambda *_a, **_k: called.__setitem__("n", called["n"] + 1) or _FakeCompleted(),
        )
        monkeypatch.setattr("openmarquee.name_actuator.shutil.which", lambda _n: None)
        name_actuator._apply_tailscale_hostname("JasonsSign1")
        assert called["n"] == 0

    def test_avahi_no_op_when_conf_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "openmarquee.name_actuator._AVAHI_CONF",
            tmp_path / "avahi-daemon.conf",  # doesn't exist
        )
        # Must not raise.
        name_actuator._apply_avahi_hostname("JasonsSign1")

    def test_hostapd_no_op_when_conf_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "openmarquee.name_actuator._HOSTAPD_CONF",
            tmp_path / "hostapd.conf",  # doesn't exist
        )
        name_actuator._apply_hostapd_ssid("JasonsSign1")


class TestAvahiRewrite:
    """The mDNS rewrite must set `host-name=<name>` correctly + not
    lose the surrounding conf lines."""

    def test_rewrites_existing_host_name_line(self, monkeypatch, tmp_path):
        conf = tmp_path / "avahi-daemon.conf"
        conf.write_text("[server]\n#host-name=openmarquee\ndomain-name=local\n")
        monkeypatch.setattr("openmarquee.name_actuator._AVAHI_CONF", conf)
        # Stub systemctl to a no-op.
        monkeypatch.setattr("openmarquee.name_actuator.shutil.which", lambda name: None)
        name_actuator._apply_avahi_hostname("JasonsSign1")
        contents = conf.read_text()
        assert "host-name=JasonsSign1" in contents
        # Surrounding lines survive.
        assert "[server]" in contents
        assert "domain-name=local" in contents
        # No lingering commented-out line.
        assert "#host-name=" not in contents

    def test_appends_host_name_when_absent(self, monkeypatch, tmp_path):
        conf = tmp_path / "avahi-daemon.conf"
        conf.write_text("[server]\ndomain-name=local\n")
        monkeypatch.setattr("openmarquee.name_actuator._AVAHI_CONF", conf)
        monkeypatch.setattr("openmarquee.name_actuator.shutil.which", lambda _n: None)
        name_actuator._apply_avahi_hostname("JasonsSign1")
        assert "host-name=JasonsSign1" in conf.read_text()

    def test_no_write_when_value_unchanged(self, monkeypatch, tmp_path):
        """If the conf already has the target host-name, don't
        trigger a systemctl restart. Detect by checking the
        systemctl-side spy."""
        conf = tmp_path / "avahi-daemon.conf"
        conf.write_text("[server]\nhost-name=JasonsSign1\n")
        monkeypatch.setattr("openmarquee.name_actuator._AVAHI_CONF", conf)
        systemctl_calls: list = []
        monkeypatch.setattr(
            "openmarquee.name_actuator.shutil.which",
            lambda name: "/usr/bin/systemctl" if name == "systemctl" else None,
        )
        monkeypatch.setattr(
            "openmarquee.name_actuator.subprocess.run",
            lambda *a, **k: systemctl_calls.append(a) or _FakeCompleted(),
        )
        name_actuator._apply_avahi_hostname("JasonsSign1")
        assert systemctl_calls == []


class TestHostapdRewrite:
    def test_rewrites_existing_ssid_line(self, monkeypatch, tmp_path):
        conf = tmp_path / "hostapd.conf"
        conf.write_text("interface=ap0\nssid=openMarquee-SETUP\nchannel=6\n")
        monkeypatch.setattr("openmarquee.name_actuator._HOSTAPD_CONF", conf)
        monkeypatch.setattr("openmarquee.name_actuator.shutil.which", lambda _n: None)
        name_actuator._apply_hostapd_ssid("JasonsSign1")
        contents = conf.read_text()
        assert "ssid=JasonsSign1" in contents
        # Original AP settings intact.
        assert "interface=ap0" in contents
        assert "channel=6" in contents


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
