"""Batch 19.4 / sweep #10 #9: test the MAC-derivation snippet in
system/openmarquee-ap0-setup.sh.

The shell script invokes an inline python3 script that derives the ap0
interface's MAC from wlan0's. Before 19.4 the code was `m[0] |= 0x02;
m[0] ^= 0x02` -- a no-op when bit 0x02 was clear (which it always is
on IEEE-assigned MACs). 19.4 fixes it to actually set the locally-
administered bit.

This test execs the same python expression against synthetic input
MACs and asserts:
  - The locally-administered bit (0x02 in octet 0) is SET on the
    output regardless of whether it was set on the input.
  - The output's last octet differs from the input's by exactly 1
    (xor 0x01).
  - All other octets are unchanged.
"""

from __future__ import annotations

import subprocess


def _derive_ap0_mac(wlan0_mac: str) -> str:
    """Mirror of the inline python3 -c block in
    system/openmarquee-ap0-setup.sh. Kept in lock-step with the
    shell script -- update both together when the derivation
    changes. Calling python3 via subprocess is what the shell does,
    so the test exercises the actual interpreter path."""
    out = subprocess.run(
        [
            "python3",
            "-c",
            """
import sys
m = [int(o, 16) for o in sys.argv[1].split(':')]
m[0] |= 0x02   # set locally-administered bit
m[5] ^= 0x01   # differ from wlan0 in last octet
print(':'.join(f'{o:02x}' for o in m))
""",
            wlan0_mac,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def test_la_bit_set_on_ieee_assigned_input():
    """A typical IEEE-assigned wlan0 MAC has bit 0x02 of octet 0
    clear (it IS in the IEEE OUI registry). Output must set it.
    """
    wlan0 = "b8:27:eb:12:34:56"  # Raspberry Pi OUI shape
    assert int(wlan0.split(":")[0], 16) & 0x02 == 0  # sanity: bit clear on input
    ap0 = _derive_ap0_mac(wlan0)
    assert int(ap0.split(":")[0], 16) & 0x02 == 0x02


def test_la_bit_preserved_when_already_set():
    """If the input MAC already has the LA bit, output still has it
    (`|=` is idempotent). No accidental flip-back."""
    wlan0 = "ba:27:eb:12:34:56"  # 0xba has bit 0x02 already set
    assert int(wlan0.split(":")[0], 16) & 0x02 == 0x02
    ap0 = _derive_ap0_mac(wlan0)
    assert int(ap0.split(":")[0], 16) & 0x02 == 0x02


def test_last_octet_differs_by_one():
    """The ^=0x01 trick on octet 5 distinguishes the two MACs while
    keeping them adjacent / debuggable."""
    wlan0 = "b8:27:eb:12:34:56"
    ap0 = _derive_ap0_mac(wlan0)
    wlan0_last = int(wlan0.split(":")[-1], 16)
    ap0_last = int(ap0.split(":")[-1], 16)
    assert ap0_last == wlan0_last ^ 0x01


def test_middle_octets_unchanged():
    """Only octet 0 and octet 5 are touched. Octets 1..4 round-trip
    verbatim."""
    wlan0 = "b8:27:eb:12:34:56"
    ap0 = _derive_ap0_mac(wlan0)
    assert ap0.split(":")[1:5] == wlan0.split(":")[1:5]


def test_output_format_is_lowercase_hex():
    """Output keeps the canonical XX:XX:XX:XX:XX:XX shape, lowercase
    so it matches what `ip link set ... address` prints back."""
    ap0 = _derive_ap0_mac("B8:27:EB:12:34:56")
    parts = ap0.split(":")
    assert len(parts) == 6
    for p in parts:
        assert len(p) == 2
        assert p == p.lower()
