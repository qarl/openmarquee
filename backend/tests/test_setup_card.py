"""Tests for setup-card join credentials (the SSID + PIN the SETUP
system card shows so someone at the sign can join the setup AP).
2026-07-07: added when the SETUP card was rendering placeholder
`openMarquee-Setup` / `----` instead of the real network + password.
"""

from openmarquee.setup_card import setup_card_credentials


def test_ssid_from_hostname(monkeypatch):
    monkeypatch.setattr("socket.gethostname", lambda: "JasonsSign1")
    assert setup_card_credentials().get("ssid") == "JasonsSign1"


def test_ssid_strips_domain_suffix(monkeypatch):
    monkeypatch.setattr("socket.gethostname", lambda: "JasonsSign1.local")
    assert setup_card_credentials().get("ssid") == "JasonsSign1"


def test_pin_from_live_wifi_password(monkeypatch):
    monkeypatch.setattr("socket.gethostname", lambda: "sign")

    class _Settings:
        wifi_password = "hunter2-passphrase"

    class _Storage:
        def load(self):
            return _Settings()

    monkeypatch.setattr("openmarquee.dependencies.get_settings_storage", lambda: _Storage())
    assert setup_card_credentials() == {"ssid": "sign", "pin": "hunter2-passphrase"}


def test_fail_soft_on_settings_error_omits_pin(monkeypatch):
    monkeypatch.setattr("socket.gethostname", lambda: "sign")

    def _boom():
        raise RuntimeError("no settings storage")

    monkeypatch.setattr("openmarquee.dependencies.get_settings_storage", _boom)
    # SSID still present; pin omitted; no raise.
    assert setup_card_credentials() == {"ssid": "sign"}


def test_omits_ssid_when_hostname_empty(monkeypatch):
    monkeypatch.setattr("socket.gethostname", lambda: "")

    class _Storage:
        def load(self):
            raise RuntimeError()

    monkeypatch.setattr("openmarquee.dependencies.get_settings_storage", lambda: _Storage())
    assert setup_card_credentials() == {}
