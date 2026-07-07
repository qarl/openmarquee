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


def _install_password(monkeypatch, password, hostname="sign"):
    monkeypatch.setattr("socket.gethostname", lambda: hostname)

    class _Settings:
        wifi_password = password

    class _Storage:
        def load(self):
            return _Settings()

    monkeypatch.setattr("openmarquee.dependencies.get_settings_storage", lambda: _Storage())


def test_pin_from_live_wifi_password(monkeypatch):
    _install_password(monkeypatch, "hunter2-passphrase")
    assert setup_card_credentials() == {
        "ssid": "sign",
        "pin": "hunter2-passphrase",
        "qr_payload": "WIFI:T:WPA;S:sign;P:hunter2-passphrase;;",
    }


def test_qr_payload_encodes_wifi_join(monkeypatch):
    # A phone camera reads this to hop straight onto the setup AP.
    _install_password(monkeypatch, "correcthorse", hostname="JasonsSign1")
    assert setup_card_credentials()["qr_payload"] == "WIFI:T:WPA;S:JasonsSign1;P:correcthorse;;"


def test_qr_payload_escapes_special_chars(monkeypatch):
    # A passphrase with WIFI: URI metacharacters must be backslash-escaped
    # or the QR is unparseable.
    _install_password(monkeypatch, "a;b:c", hostname="signx")
    assert setup_card_credentials()["qr_payload"] == "WIFI:T:WPA;S:signx;P:a\\;b\\:c;;"


def test_qr_payload_omitted_without_password(monkeypatch):
    # No password → no join QR (and no bogus half-QR).
    monkeypatch.setattr("socket.gethostname", lambda: "sign")

    def _boom():
        raise RuntimeError("no settings")

    monkeypatch.setattr("openmarquee.dependencies.get_settings_storage", _boom)
    creds = setup_card_credentials()
    assert "qr_payload" not in creds
    assert creds == {"ssid": "sign"}


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
