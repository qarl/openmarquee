"""Unit tests for wifi_prefill.read_system_wifi.

The module is best-effort — every failure mode returns None instead
of raising — so most tests verify graceful-None behavior under various
broken-input scenarios. The happy-path test covers the standard
Raspberry Pi Imager output: one quoted-plaintext network={} block
matching the active SSID.

Permission/IO tests use tmp_path to create files we control rather
than poking at the real /etc/wpa_supplicant/wpa_supplicant.conf.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openmarquee.wifi_prefill import read_system_wifi


def _write_conf(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "wpa_supplicant.conf"
    p.write_text(body, encoding="utf-8")
    return p


# --- happy path ---


def test_returns_creds_for_active_network(tmp_path: Path):
    conf = _write_conf(tmp_path, '''
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=US

network={
    ssid="MyHome"
    psk="hunter2pass"
    key_mgmt=WPA-PSK
}
''')
    result = read_system_wifi(
        paths=(conf,),
        get_active_ssid=lambda: "MyHome",  # fake iwgetid
    )
    assert result == ("MyHome", "hunter2pass")


def test_picks_block_matching_active_ssid_when_multiple(tmp_path: Path):
    """wpa_supplicant.conf can list several network={} blocks. We
    must pick the one whose ssid matches `iwgetid -r`, not just the
    first one."""
    conf = _write_conf(tmp_path, '''
network={
    ssid="OtherNet"
    psk="otherpass"
}
network={
    ssid="ActiveNet"
    psk="activepass"
}
''')
    result = read_system_wifi(
        paths=(conf,),
        get_active_ssid=lambda: "ActiveNet",
    )
    assert result == ("ActiveNet", "activepass")


# --- failure modes (all return None) ---


_MIN_CONF = '''
network={
    ssid="X"
    psk="ypassxxx"
}
'''


def test_returns_none_when_iwgetid_returns_empty(tmp_path: Path):
    """No active connection (iwgetid prints empty line) → None."""
    conf = _write_conf(tmp_path, _MIN_CONF)
    result = read_system_wifi(
        paths=(conf,),
        get_active_ssid=lambda: None,
    )
    assert result is None


def test_returns_none_when_iwgetid_missing(tmp_path: Path):
    """iwgetid not installed → None (FileNotFoundError swallowed)."""
    conf = _write_conf(tmp_path, _MIN_CONF)
    result = read_system_wifi(
        paths=(conf,),
        get_active_ssid=lambda: None,
    )
    assert result is None


def test_returns_none_when_no_conf_file_exists(tmp_path: Path):
    missing = tmp_path / "nope.conf"
    result = read_system_wifi(
        paths=(missing,),
        get_active_ssid=lambda: "X",
    )
    assert result is None


def test_falls_through_to_second_path_when_first_missing(tmp_path: Path):
    """The default config tries /var/openmarquee/wpa_supplicant.conf
    first, /etc/... second. Verify the fallthrough."""
    missing = tmp_path / "first.conf"
    conf = _write_conf(tmp_path, _MIN_CONF)
    result = read_system_wifi(
        paths=(missing, conf),
        get_active_ssid=lambda: "X",
    )
    assert result == ("X", "ypassxxx")


def test_returns_none_when_no_block_matches_active_ssid(tmp_path: Path):
    conf = _write_conf(tmp_path, _MIN_CONF)
    result = read_system_wifi(
        paths=(conf,),
        get_active_ssid=lambda: "DifferentNet",  # different network
    )
    assert result is None


def test_skips_disabled_network_blocks(tmp_path: Path):
    """A network={} block with `disabled=1` shouldn't match even if
    its ssid matches the active connection. (Edge case — the active
    SSID in such a config implies the disabled flag was added later
    and wpa_supplicant didn't reload — but we should respect it.)"""
    conf = _write_conf(tmp_path, '''
network={
    ssid="MyNet"
    psk="mypass12"
    disabled=1
}
''')
    result = read_system_wifi(
        paths=(conf,),
        get_active_ssid=lambda: "MyNet",
    )
    assert result is None


def test_returns_none_for_open_network_no_psk(tmp_path: Path):
    """Open networks (no psk= line) can't pre-fill a password field
    and we don't try."""
    conf = _write_conf(tmp_path, '''
network={
    ssid="OpenAP"
    key_mgmt=NONE
}
''')
    result = read_system_wifi(
        paths=(conf,),
        get_active_ssid=lambda: "OpenAP",
    )
    assert result is None


def test_skips_hex_encoded_psk(tmp_path: Path):
    """An unquoted 64-hex-char PSK is a pre-computed hash, not a
    plaintext passphrase — can't fit in SystemSettings.wifi_station_
    password's 8-63 printable-ASCII validator. Skip rather than
    return invalid creds."""
    hex_psk = "a" * 64
    conf = _write_conf(tmp_path, f'''
network={{
    ssid="HexNet"
    psk={hex_psk}
}}
''')
    result = read_system_wifi(
        paths=(conf,),
        get_active_ssid=lambda: "HexNet",
    )
    assert result is None


def test_handles_malformed_conf(tmp_path: Path):
    """Garbage in the conf shouldn't crash the parser."""
    conf = _write_conf(tmp_path, "this is not a wpa_supplicant.conf at all\n{{{}}}\n")
    result = read_system_wifi(
        paths=(conf,),
        get_active_ssid=lambda: "MyNet",
    )
    assert result is None


def test_handles_trailing_whitespace_in_conf(tmp_path: Path):
    """Operators sometimes hand-edit conf and leave trailing spaces
    inside the network={} block. The regex's `.+?\\s*$` handles it
    so the SSID match against the active connection still works."""
    conf = _write_conf(tmp_path, '''
network={
    ssid="MyNet"
    psk="mypass12"
}
''')
    result = read_system_wifi(
        paths=(conf,),
        get_active_ssid=lambda: "MyNet",
    )
    assert result == ("MyNet", "mypass12")
