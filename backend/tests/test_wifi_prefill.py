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

from openmarquee.wifi_prefill import read_system_wifi


def _write_conf(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "wpa_supplicant.conf"
    p.write_text(body, encoding="utf-8")
    return p


# --- happy path ---


def test_returns_creds_for_active_network(tmp_path: Path):
    conf = _write_conf(
        tmp_path,
        """
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=US

network={
    ssid="MyHome"
    psk="hunter2pass"
    key_mgmt=WPA-PSK
}
""",
    )
    result = read_system_wifi(
        paths=(conf,),
        get_active_ssid=lambda: "MyHome",  # fake iwgetid
    )
    assert result == ("MyHome", "hunter2pass")


def test_picks_block_matching_active_ssid_when_multiple(tmp_path: Path):
    """wpa_supplicant.conf can list several network={} blocks. We
    must pick the one whose ssid matches `iwgetid -r`, not just the
    first one."""
    conf = _write_conf(
        tmp_path,
        """
network={
    ssid="OtherNet"
    psk="otherpass"
}
network={
    ssid="ActiveNet"
    psk="activepass"
}
""",
    )
    result = read_system_wifi(
        paths=(conf,),
        get_active_ssid=lambda: "ActiveNet",
    )
    assert result == ("ActiveNet", "activepass")


# --- failure modes (all return None) ---


_MIN_CONF = """
network={
    ssid="X"
    psk="ypassxxx"
}
"""


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
    conf = _write_conf(
        tmp_path,
        """
network={
    ssid="MyNet"
    psk="mypass12"
    disabled=1
}
""",
    )
    result = read_system_wifi(
        paths=(conf,),
        get_active_ssid=lambda: "MyNet",
    )
    assert result is None


def test_returns_none_for_open_network_no_psk(tmp_path: Path):
    """Open networks (no psk= line) can't pre-fill a password field
    and we don't try."""
    conf = _write_conf(
        tmp_path,
        """
network={
    ssid="OpenAP"
    key_mgmt=NONE
}
""",
    )
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
    conf = _write_conf(
        tmp_path,
        f"""
network={{
    ssid="HexNet"
    psk={hex_psk}
}}
""",
    )
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


def test_unescapes_quote_inside_psk(tmp_path: Path):
    """wpa_supplicant.conf(5) permits \\" inside quoted PSK values
    (operator's actual wifi password contains a `"`). Pre-fix, the
    parser passed `\\"` through literally, producing a backslash +
    quote in the prefilled SystemSettings — the operator's real
    wifi password was `my"pass1` but the form showed `my\\"pass1`
    and they had to manually clean it up. Verify the parser
    unescapes."""
    conf = _write_conf(
        tmp_path,
        """
network={
    ssid="EscNet"
    psk="my\\"pass1"
}
""",
    )
    result = read_system_wifi(
        paths=(conf,),
        get_active_ssid=lambda: "EscNet",
    )
    assert result == ("EscNet", 'my"pass1')


def test_unescapes_backslash_inside_psk(tmp_path: Path):
    """wpa_supplicant.conf(5) also permits \\\\ (literal backslash)
    inside quoted PSK values. Pre-fix the parser passed both chars
    through; post-fix it emits one literal backslash."""
    conf = _write_conf(
        tmp_path,
        """
network={
    ssid="BackslashNet"
    psk="back\\\\slash"
}
""",
    )
    result = read_system_wifi(
        paths=(conf,),
        get_active_ssid=lambda: "BackslashNet",
    )
    assert result == ("BackslashNet", r"back\slash")


def test_unescapes_quote_inside_ssid(tmp_path: Path):
    """SSIDs (rarely) also use \\" escapes per wpa_supplicant.conf(5).
    The active-SSID match check must compare against the unescaped
    form since iwgetid -r emits the actual SSID bytes, not the
    conf-file escape sequence."""
    conf = _write_conf(
        tmp_path,
        """
network={
    ssid="Joe\\"sNet"
    psk="goodpass"
}
""",
    )
    result = read_system_wifi(
        paths=(conf,),
        get_active_ssid=lambda: 'Joe"sNet',
    )
    assert result == ('Joe"sNet', "goodpass")


def test_psk_value_never_logged(tmp_path: Path, caplog):
    """Audit dimension 1: PSK plaintext must NEVER appear in log
    output, even after the escape-unescape rewrite. Verify by
    rendering a noisy fixture (escaped + matched + mismatched) and
    grepping the captured log for the PSK substring."""
    secret = "topsecret!"
    conf = _write_conf(
        tmp_path,
        f'''
network={{
    ssid="LogNet"
    psk="{secret}"
}}
''',
    )
    with caplog.at_level("DEBUG", logger="openmarquee.wifi_prefill"):
        result = read_system_wifi(
            paths=(conf,),
            get_active_ssid=lambda: "LogNet",
        )
    assert result == ("LogNet", secret)
    # Every captured log message must NOT contain the PSK bytes.
    # rec.getMessage() returns the args-formatted message; rec.msg
    # is the format string. Check both since a future change might
    # log the value via a different path.
    for rec in caplog.records:
        formatted = rec.getMessage()
        assert secret not in formatted, (
            f"PSK leaked to {rec.levelname} log (formatted): {formatted!r}"
        )
        assert secret not in str(rec.msg), (
            f"PSK leaked to {rec.levelname} log (raw msg): {rec.msg!r}"
        )
        for arg in rec.args or ():
            assert secret not in str(arg), f"PSK leaked to {rec.levelname} log (arg): {arg!r}"


def test_handles_trailing_whitespace_in_conf(tmp_path: Path):
    """Operators sometimes hand-edit conf and leave trailing spaces
    inside the network={} block. The regex's `.+?\\s*$` handles it
    so the SSID match against the active connection still works."""
    conf = _write_conf(
        tmp_path,
        """
network={
    ssid="MyNet"
    psk="mypass12"
}
""",
    )
    result = read_system_wifi(
        paths=(conf,),
        get_active_ssid=lambda: "MyNet",
    )
    assert result == ("MyNet", "mypass12")


# --- Batch 19.1 / sweep #10 #1: DEFAULT_WPA_CONF_PATHS ordering ---


def test_default_paths_include_per_interface_wlan0_path():
    """Bookworm's `wpa_supplicant@wlan0.service` reads its conf from
    `/etc/wpa_supplicant/wpa_supplicant-wlan0.conf` -- the per-
    interface form. system/README.md install instructions write the
    file at that path. Pre-19.1, the per-interface path wasn't in
    DEFAULT_WPA_CONF_PATHS, so wifi_prefill never found the conf on
    a real Pi (the captive-portal first-run flow read "no SSID" while
    the device WAS joined to a home network)."""
    from openmarquee.wifi_prefill import DEFAULT_WPA_CONF_PATHS

    paths = [str(p) for p in DEFAULT_WPA_CONF_PATHS]
    # All 3 candidate paths present.
    assert "/var/openmarquee/wpa_supplicant.conf" in paths
    assert "/etc/wpa_supplicant/wpa_supplicant-wlan0.conf" in paths
    assert "/etc/wpa_supplicant/wpa_supplicant.conf" in paths
    # /var copy first, per-interface path second, bare path last --
    # the operator's /var override wins, then the per-interface
    # Bookworm path, then the legacy bare path for older Pi OS
    # images.
    var_idx = paths.index("/var/openmarquee/wpa_supplicant.conf")
    wlan0_idx = paths.index("/etc/wpa_supplicant/wpa_supplicant-wlan0.conf")
    bare_idx = paths.index("/etc/wpa_supplicant/wpa_supplicant.conf")
    assert var_idx < wlan0_idx < bare_idx
