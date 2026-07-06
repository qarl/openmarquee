"""Tests for the mDNS identity-URL derivation (boot-identity-card).

The boot identity card + the CONNECTED card show the device's real
hostname-derived URL instead of a hardcoded ``openmarquee.local``.
``mdns_url()`` is the single source for that string.
"""

from openmarquee.mdns import mdns_hostname, mdns_url


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
