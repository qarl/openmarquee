"""P1.0 onboarding-rework deliverables: regression locks for the
diagnostics-first wins shipped in the audit + plan dispatch.

Scope (per docs/onboarding-rework-plan.md §D):
  1. hostapd.conf gains country_code=US + ht_capab pin (20 MHz only).
  2. openmarquee-ap0-setup.sh logs sta_channel + ap_channel at every
     bring-up. The freq-to-channel math (channel 1=2412, +5 MHz/ch up
     to 14=2484) is the only non-trivial logic; mirror it here.
  3. openmarquee-wifi-powersave-off.{service,sh} are installed by
     install.sh's systemd-units list + sh-helpers chmod list +
     systemctl enable list.

These tests pin the deliverables so a future refactor can't silently
drop the diagnostics surface. P1.1's supervisor will absorb most of
this; pre-supervisor we rely on systemd + scripts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SYSTEM_DIR = REPO_ROOT / "system"
SCRIPTS_DIR = REPO_ROOT / "scripts"


# ============================================================
# 2.4 GHz freq -> channel: pure math mirroring the shell snippet
# in openmarquee-ap0-setup.sh. The shell uses awk + integer
# division; this Python mirror is the canonical reference.
# ============================================================


def _freq_to_channel(freq_mhz: int) -> str:
    """Mirror of the shell:
        if 2412 <= freq <= 2484:
            if freq == 2484: channel=14
            else:            channel=(freq-2412)/5 + 1
        else:
            channel='unknown_5ghz_or_other'

    Returns either an int-as-str (canonical 2.4 GHz channels 1-14)
    or the literal "unknown_5ghz_or_other" so test assertions can
    treat both shapes uniformly.
    """
    if 2412 <= freq_mhz <= 2484:
        if freq_mhz == 2484:
            return "14"
        return str((freq_mhz - 2412) // 5 + 1)
    return "unknown_5ghz_or_other"


@pytest.mark.parametrize(
    "freq_mhz,expected_channel",
    [
        # Channel 1 -- the canonical 2.4 GHz start.
        (2412, "1"),
        # Channel 6 -- the default in hostapd.conf.
        (2437, "6"),
        # Channel 11 -- common US router default.
        (2462, "11"),
        # Channel 13 -- EU max for hostapd country=US would warn;
        # this asserts the math, not the policy.
        (2472, "13"),
        # Channel 14 -- Japan-only, special-cased in the shell.
        (2484, "14"),
        # 5 GHz frequencies fall through to the unknown bucket.
        (5180, "unknown_5ghz_or_other"),
        (5240, "unknown_5ghz_or_other"),
        (5825, "unknown_5ghz_or_other"),
    ],
)
def test_freq_to_channel_mirrors_shell_math(freq_mhz, expected_channel):
    assert _freq_to_channel(freq_mhz) == expected_channel


# ============================================================
# hostapd.conf P1.0 additions: country_code + ht_capab.
# ============================================================


def test_hostapd_conf_has_explicit_country_code():
    """P1.0 §A contributor #5: hostapd MUST have country_code set so
    it can beacon on whichever channel wlan0's STA chose. country=US
    was already in wpa_supplicant; the hostapd half was missing.
    """
    conf = (SYSTEM_DIR / "hostapd.conf").read_text()
    assert "country_code=US" in conf, "hostapd.conf must declare country_code=US (P1.0 §A #5)"


def test_hostapd_conf_pins_minimal_ht_capab():
    """P1.0 spec §"hostapd.conf template" Notes: 40 MHz on 2.4 GHz
    buys nothing at the AP+STA-same-channel constraint and adds
    coexistence failures. Pin [SHORT-GI-20] as the baseline.
    """
    conf = (SYSTEM_DIR / "hostapd.conf").read_text()
    assert "ht_capab=[SHORT-GI-20]" in conf, (
        'hostapd.conf must pin ht_capab=[SHORT-GI-20] (P1.0 spec §"hostapd.conf template" Notes)'
    )
    # Defensive: no 40 MHz advertisement that would slip through.
    assert "HT40" not in conf, (
        "hostapd.conf must NOT advertise HT40 capabilities on 2.4 GHz "
        "(coexistence failures per spec)"
    )


# ============================================================
# openmarquee-ap0-setup.sh: channel-value logging additions.
# ============================================================


def test_ap0_setup_logs_sta_and_ap_channels():
    """P1.0 §D item #3: ap0 setup logs STA channel + AP channel at
    every bring-up so journal-side diff is the smoking gun. Pinned
    by string presence so a future refactor can't silently drop the
    diagnostics.
    """
    setup = (SYSTEM_DIR / "openmarquee-ap0-setup.sh").read_text()
    assert "sta_channel iface=" in setup, (
        "ap0-setup must log sta_channel (channel-mismatch diagnostic)"
    )
    assert "ap_channel iface=" in setup, (
        "ap0-setup must log ap_channel (channel-mismatch diagnostic)"
    )
    # The freq-to-channel branch points: 2412/2484/5 should all
    # appear (the math the test above mirrors).
    assert "2412" in setup
    assert "2484" in setup


# ============================================================
# openmarquee-wifi-powersave-off.{service,sh} installation.
# ============================================================


def test_powersave_off_service_file_exists():
    """P1.0 §D item #4: ship a unit that enforces power_save=off on
    wlan0 + ap0 at boot. Pre-P1.0 only the watchdog WARNed.
    """
    assert (SYSTEM_DIR / "openmarquee-wifi-powersave-off.service").exists(), (
        "system/openmarquee-wifi-powersave-off.service must exist (P1.0 §D #4)"
    )
    assert (SYSTEM_DIR / "openmarquee-wifi-powersave-off.sh").exists(), (
        "system/openmarquee-wifi-powersave-off.sh must exist (P1.0 §D #4)"
    )


def test_powersave_off_script_handles_missing_iface_gracefully():
    """The script may run before ap0 exists (boot ordering) or when
    ap0 is masked (r60 ships AP off by default). It must log
    missing-iface and continue rather than failing the unit.
    """
    sh = (SYSTEM_DIR / "openmarquee-wifi-powersave-off.sh").read_text()
    # Defensive shape: ip-link probe before iw-set, with a log line
    # on missing iface.
    assert "ip link show" in sh
    assert "reason=missing_link" in sh
    # No `set -e` exit on a single missing iface.
    assert "return 0" in sh, "missing-iface branch must return 0 (idempotent + non-fatal)"


def test_install_sh_installs_powersave_unit_and_helper():
    """install.sh must add the new unit + script to its install
    pipelines + enable the systemd unit.
    """
    install = (SCRIPTS_DIR / "install.sh").read_text()
    assert "openmarquee-wifi-powersave-off.service" in install, (
        "install.sh must reference openmarquee-wifi-powersave-off.service"
    )
    assert "openmarquee-wifi-powersave-off.sh" in install, (
        "install.sh must reference openmarquee-wifi-powersave-off.sh"
    )
    # Explicit enable: not just stage the file, actually wire into
    # boot.
    assert "systemctl enable openmarquee-wifi-powersave-off.service" in install, (
        "install.sh must `systemctl enable openmarquee-wifi-powersave-off.service`"
    )


# ============================================================
# Plan doc presence: anchor the regression-lock to its own audit.
# ============================================================


def test_onboarding_plan_doc_exists():
    """The plan doc lives in the code repo per the QA dispatch's
    'committed plan doc SHA' deliverable. Regression-locked so a
    future cleanup can't accidentally delete it.
    """
    plan = REPO_ROOT / "docs" / "onboarding-rework-plan.md"
    assert plan.exists(), "docs/onboarding-rework-plan.md must exist (QA-DISPATCH onboarding-P0)"
    contents = plan.read_text()
    # Doc structure: §A audit, §B phases, §C ambiguities, §D
    # deliverables, §E constraints, §F open question.
    for section in ["§A", "§B", "§C", "§D", "§E", "§F"]:
        assert section in contents, f"plan doc must include {section}"
