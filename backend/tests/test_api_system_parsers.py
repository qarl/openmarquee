"""Unit tests for the wifi-scan output parsers in api_system.

`_parse_iw_scan` consumes `iw dev wlan0 scan` output (Linux/Pi
production path); `_parse_airport_scan` handles macOS `airport -s`
output (dev convenience). Both: regex extraction + dedupe-by-
strongest-signal + sort by (-signal, ssid).

Sweep #3 found these had zero unit tests despite running on every
/api/system/wifi-scan call. This file fixes the gap with realistic
captured-output fixtures + edge cases.
"""

from __future__ import annotations

from openmarquee.api_system import _parse_airport_scan, _parse_iw_scan

# --- iw scan parser ----------------------------------------------------------


# Realistic iw output -- two BSS blocks, dedupe-by-strongest test,
# typical signal value shape. SSID strings carry leading whitespace
# that the regex's `(.*)$` captures (then .strip() on use).
IW_TWO_NETWORKS = """\
BSS aa:bb:cc:dd:ee:ff(on wlan0)
\tTSF: 12345
\tfreq: 2412
\tsignal: -53.00 dBm
\tcapability: ESS Privacy
\tSSID: HomeNet
\tSupported rates: 1.0 2.0 5.5
BSS 11:22:33:44:55:66(on wlan0)
\tTSF: 67890
\tfreq: 2437
\tsignal: -67.00 dBm
\tcapability: ESS
\tSSID: NeighborNet
"""


IW_DUPLICATE_STRONGER_LATER = """\
BSS aa:bb:cc:dd:ee:ff
\tsignal: -80.00 dBm
\tSSID: MeshA
BSS 11:22:33:44:55:66
\tsignal: -55.00 dBm
\tSSID: MeshA
"""


IW_EMPTY_SSID_SKIPPED = """\
BSS aa:bb:cc:dd:ee:ff
\tsignal: -50.00 dBm
\tSSID:
BSS 11:22:33:44:55:66
\tsignal: -60.00 dBm
\tSSID: GoodNet
"""


IW_MALFORMED_SIGNAL = """\
BSS aa:bb:cc:dd:ee:ff
\tsignal: garbage dBm
\tSSID: NoSignalNet
BSS 11:22:33:44:55:66
\tsignal: -42.00 dBm
\tSSID: SignedNet
"""


def test_iw_parses_two_distinct_networks():
    nets = _parse_iw_scan(IW_TWO_NETWORKS)
    assert [n.ssid for n in nets] == ["HomeNet", "NeighborNet"]
    assert nets[0].signal_dbm == -53
    assert nets[1].signal_dbm == -67


def test_iw_sort_is_strongest_first_then_alpha():
    """Sort key is (-signal, ssid) so -53 (stronger) comes before
    -67 (weaker). Ties break by SSID alphabetical."""
    nets = _parse_iw_scan(IW_TWO_NETWORKS)
    assert nets[0].ssid == "HomeNet"
    assert nets[1].ssid == "NeighborNet"
    assert nets[0].signal_dbm > nets[1].signal_dbm


def test_iw_dedupes_keeping_strongest_signal():
    """Same SSID appearing in two BSS blocks: keep the stronger
    signal. Order in input shouldn't matter."""
    nets = _parse_iw_scan(IW_DUPLICATE_STRONGER_LATER)
    assert len(nets) == 1
    assert nets[0].ssid == "MeshA"
    assert nets[0].signal_dbm == -55  # the stronger of -80 vs -55


def test_iw_skips_empty_ssid():
    """An empty SSID: line (hidden network) is filtered out."""
    nets = _parse_iw_scan(IW_EMPTY_SSID_SKIPPED)
    assert [n.ssid for n in nets] == ["GoodNet"]


def test_iw_empty_input_returns_empty_list():
    assert _parse_iw_scan("") == []


def test_iw_malformed_signal_skipped_silently():
    """A non-numeric signal line doesn't match the regex; the
    SSID's signal stays None (no current_signal carried over)."""
    nets = _parse_iw_scan(IW_MALFORMED_SIGNAL)
    by_ssid = {n.ssid: n for n in nets}
    # SignedNet has a clean signal.
    assert by_ssid["SignedNet"].signal_dbm == -42
    # NoSignalNet's "garbage" line didn't match; signal is None.
    assert by_ssid["NoSignalNet"].signal_dbm is None
    # The None-signal network sorts AFTER the signed one
    # ((-(-42), 'SignedNet') < (-(None or -999), 'NoSignalNet')).
    assert nets[0].ssid == "SignedNet"


def test_iw_truncates_fractional_signal_via_int_float():
    """`int(float("-53.99"))` -> -53 (truncates toward zero).
    `int(float("-53.00"))` -> -53. Pin this so a future refactor
    to round-half-to-nearest gets caught."""
    out = """\
BSS aa
\tsignal: -53.99 dBm
\tSSID: TruncTest
"""
    nets = _parse_iw_scan(out)
    assert nets[0].signal_dbm == -53


def test_iw_signal_zero_is_kept_not_none():
    """signal=0 is a valid edge case; the regex captures it and
    `current_signal` becomes 0. `existing.signal_dbm is None` is
    the truthiness check, so 0 is correctly treated as a real
    signal value (not falsy-coerced to None)."""
    out = """\
BSS aa
\tsignal: 0 dBm
\tSSID: ZeroSig
"""
    nets = _parse_iw_scan(out)
    assert nets[0].signal_dbm == 0


def test_iw_unicode_ssid():
    """Unicode SSIDs (emoji + accented + CJK) pass through the
    regex untouched."""
    out = """\
BSS aa
\tsignal: -50.00 dBm
\tSSID: Café 🎉 网络
"""
    nets = _parse_iw_scan(out)
    assert nets[0].ssid == "Café 🎉 网络"


def test_iw_signal_resets_between_blocks():
    """`current_signal` is set to None after each SSID match.
    The third block has no signal line; the parser doesn't carry
    the previous block's signal forward."""
    out = """\
BSS aa
\tsignal: -50.00 dBm
\tSSID: First
BSS bb
\tsignal: -60.00 dBm
\tSSID: Second
BSS cc
\tSSID: NoSignal
"""
    nets = _parse_iw_scan(out)
    by_ssid = {n.ssid: n for n in nets}
    assert by_ssid["First"].signal_dbm == -50
    assert by_ssid["Second"].signal_dbm == -60
    assert by_ssid["NoSignal"].signal_dbm is None


# --- airport scan parser -----------------------------------------------------


# Realistic airport -s output (header line + body). Note: airport
# uses fixed-column-ish formatting but the parser keys on the
# BSSID pattern, not column positions.
AIRPORT_TWO_NETWORKS = """\
                            SSID BSSID             RSSI CHANNEL HT CC SECURITY (auth/unicast/group)
                         HomeNet aa:bb:cc:dd:ee:ff -53  6       Y  US WPA2(PSK/AES/AES)
                     NeighborNet 11:22:33:44:55:66 -67  11      Y  US WPA(PSK/AES,TKIP/TKIP)
"""


AIRPORT_DUPLICATE = """\
                            SSID BSSID             RSSI
                           MeshA aa:bb:cc:dd:ee:ff -80
                           MeshA 11:22:33:44:55:66 -55
"""


AIRPORT_HEADER_ONLY = """\
                            SSID BSSID             RSSI CHANNEL
"""


AIRPORT_NO_RSSI_COLUMN = """\
                            SSID BSSID
                         HomeNet aa:bb:cc:dd:ee:ff
"""


def test_airport_parses_two_networks():
    nets = _parse_airport_scan(AIRPORT_TWO_NETWORKS)
    assert [n.ssid for n in nets] == ["HomeNet", "NeighborNet"]
    assert nets[0].signal_dbm == -53
    assert nets[1].signal_dbm == -67


def test_airport_dedupes_keeping_strongest():
    nets = _parse_airport_scan(AIRPORT_DUPLICATE)
    assert len(nets) == 1
    assert nets[0].ssid == "MeshA"
    assert nets[0].signal_dbm == -55


def test_airport_skips_header_line():
    nets = _parse_airport_scan(AIRPORT_HEADER_ONLY)
    assert nets == []


def test_airport_empty_input():
    assert _parse_airport_scan("") == []


def test_airport_no_rssi_returns_none_signal():
    """If the line has BSSID but no trailing tokens, RSSI is
    None, not 0 or -999."""
    nets = _parse_airport_scan(AIRPORT_NO_RSSI_COLUMN)
    assert len(nets) == 1
    assert nets[0].ssid == "HomeNet"
    assert nets[0].signal_dbm is None


def test_airport_malformed_rssi_returns_none():
    """The RSSI cell parses via int(); a non-int leaves rssi as
    None per the except ValueError branch."""
    out = """\
                            SSID BSSID             RSSI
                          BadRSS aa:bb:cc:dd:ee:ff garbage
"""
    nets = _parse_airport_scan(out)
    assert nets[0].signal_dbm is None


def test_airport_skips_lines_without_bssid():
    """Body lines that don't match the BSSID regex (e.g. trailing
    blank, footer note) are skipped."""
    out = """\
                            SSID BSSID             RSSI
                         HomeNet aa:bb:cc:dd:ee:ff -53

(found 1 network)
"""
    nets = _parse_airport_scan(out)
    assert [n.ssid for n in nets] == ["HomeNet"]


def test_airport_sort_strongest_first():
    nets = _parse_airport_scan(AIRPORT_TWO_NETWORKS)
    assert nets[0].signal_dbm > nets[1].signal_dbm


def test_airport_ssid_with_internal_whitespace():
    """SSIDs can contain spaces; the parser captures everything
    up to the BSSID match. rstrip() removes any trailing padding
    but leaves internal whitespace intact."""
    out = """\
                            SSID BSSID             RSSI
                  My Home Net 5G aa:bb:cc:dd:ee:ff -50
"""
    nets = _parse_airport_scan(out)
    assert nets[0].ssid == "My Home Net 5G"


def test_airport_dedupe_lower_signal_first():
    """Same SSID, weaker signal seen first then stronger: the
    stronger replaces. The replacement requires `existing.signal_dbm
    is None or rssi > existing.signal_dbm` -- the latter clause
    drives the swap."""
    out = """\
                            SSID BSSID             RSSI
                          MyNet aa:bb:cc:dd:ee:ff -90
                          MyNet 11:22:33:44:55:66 -40
"""
    nets = _parse_airport_scan(out)
    assert nets[0].signal_dbm == -40


def test_airport_uppercase_bssid_matched():
    """The BSSID regex tolerates [a-fA-F]; verify uppercase
    BSSIDs match."""
    out = """\
                            SSID BSSID             RSSI
                          UpCase AA:BB:CC:DD:EE:FF -45
"""
    nets = _parse_airport_scan(out)
    assert nets[0].ssid == "UpCase"
    assert nets[0].signal_dbm == -45
