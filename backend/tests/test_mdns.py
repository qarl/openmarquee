"""Tests for the mDNS identity-URL derivation (boot-identity-card).

The boot identity card + the CONNECTED card show the device's real
hostname-derived URL instead of a hardcoded ``openmarquee.local``.
``mdns_url()`` is the single source for that string.
"""

from openmarquee.mdns import mdns_hostname, mdns_url, wlan0_ipv4


def test_mdns_url_uses_env_override(monkeypatch):
    monkeypatch.setenv("OPENMARQUEE_MDNS_HOSTNAME", "JasonsSign1")
    assert mdns_url() == "http://jasonssign1.local"


def test_mdns_hostname_lowercases_and_strips_domain(monkeypatch):
    # An FQDN from a resolver is reduced to the leaf label, lowercased.
    monkeypatch.setenv("OPENMARQUEE_MDNS_HOSTNAME", "JasonsSign1.local.")
    assert mdns_hostname() == "jasonssign1"


def test_mdns_url_derives_from_live_hostname(monkeypatch):
    # No override -> the live system hostname is the source of truth,
    # so a rename (fireplacesign -> JasonsSign1) tracks automatically.
    monkeypatch.delenv("OPENMARQUEE_MDNS_HOSTNAME", raising=False)
    monkeypatch.setattr("socket.gethostname", lambda: "FirePlaceSign")
    assert mdns_url() == "http://fireplacesign.local"


def test_mdns_hostname_falls_back_when_empty(monkeypatch):
    monkeypatch.delenv("OPENMARQUEE_MDNS_HOSTNAME", raising=False)
    monkeypatch.setattr("socket.gethostname", lambda: "")
    assert mdns_url() == "http://openmarquee.local"


def test_mdns_hostname_falls_back_on_oserror(monkeypatch):
    monkeypatch.delenv("OPENMARQUEE_MDNS_HOSTNAME", raising=False)

    def _boom():
        raise OSError("no hostname")

    monkeypatch.setattr("socket.gethostname", _boom)
    assert mdns_hostname() == "openmarquee"


# --- wlan0_ipv4 (boot-card IP line, 2026-07-07) ---


def test_wlan0_ipv4_env_override(monkeypatch):
    monkeypatch.setenv("OPENMARQUEE_WLAN0_IP", "10.0.0.42")
    assert wlan0_ipv4() == "10.0.0.42"


def test_wlan0_ipv4_empty_override_is_none(monkeypatch):
    # Empty override models "no IPv4 yet" — the card omits the line.
    monkeypatch.setenv("OPENMARQUEE_WLAN0_IP", "")
    assert wlan0_ipv4() is None


def test_wlan0_ipv4_none_on_read_error(monkeypatch):
    # No override + the socket/ioctl path raises (non-Linux / no wlan0)
    # → None (fail-soft; card omits the line).
    from openmarquee import mdns

    monkeypatch.delenv("OPENMARQUEE_WLAN0_IP", raising=False)

    def _boom(*args, **kwargs):
        raise OSError("no wlan0")

    monkeypatch.setattr(mdns.socket, "socket", _boom)
    assert wlan0_ipv4() is None


def test_wlan0_ipv4_parses_ioctl_result(monkeypatch):
    # The SIOCGIFADDR ifreq carries the IPv4 at bytes 20-24.
    import fcntl
    import socket as _socket

    from openmarquee import mdns

    monkeypatch.delenv("OPENMARQUEE_WLAN0_IP", raising=False)

    class _FakeSock:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def fileno(self):
            return 3

    monkeypatch.setattr(mdns.socket, "socket", lambda *a, **k: _FakeSock())
    ifreq = bytearray(32)
    ifreq[20:24] = _socket.inet_aton("192.168.1.67")
    monkeypatch.setattr(fcntl, "ioctl", lambda *a, **k: bytes(ifreq))
    assert wlan0_ipv4() == "192.168.1.67"
