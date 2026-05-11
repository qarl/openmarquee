"""End-to-end tests for system/openmarquee-firstboot.sh (Phase B.4).

The script is a self-contained shell program; we exercise it by
running it with ROOT_PREFIX=<tmpdir> against a fake filesystem
layout, then asserting the post-run state of files.

Covers:
- AP credentials generated with the right entropy + charset
- wifi.json written 0600 with both fields
- hostapd.conf SSID + passphrase lines templated in place
- welcome.html {{AP_SSID}} + {{AP_PASSWORD}} placeholders filled
- .bootstrapped marker touched
- Idempotency: second run reuses wifi.json instead of regenerating
- QR templating when qrencode is available (Linux CI) -- soft-skipped
  on macOS dev boxes where qrencode usually isn't installed
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _REPO_ROOT / "system" / "openmarquee-firstboot.sh"
_SRC_HOSTAPD = _REPO_ROOT / "system" / "hostapd.conf"
_SRC_WELCOME = _REPO_ROOT / "ui" / "welcome.html"


@pytest.fixture
def fakefs(tmp_path: Path) -> Path:
    """Build a minimal /etc + /var + /opt layout under tmp_path so
    the script's ROOT_PREFIX-redirected paths all exist."""
    (tmp_path / "var" / "openmarquee").mkdir(parents=True)
    (tmp_path / "etc" / "hostapd").mkdir(parents=True)
    (tmp_path / "opt" / "openmarquee" / "ui").mkdir(parents=True)
    shutil.copy(_SRC_HOSTAPD, tmp_path / "etc" / "hostapd" / "hostapd.conf")
    shutil.copy(_SRC_WELCOME, tmp_path / "opt" / "openmarquee" / "ui" / "welcome.html")
    return tmp_path


def _run_oneshot(fakefs: Path, env: dict[str, str] | None = None) -> str:
    """Invoke the oneshot with ROOT_PREFIX redirected to fakefs."""
    full_env = {
        **os.environ,
        "ROOT_PREFIX": str(fakefs),
        # PHY_IFACE is read via /sys/class/net/... which doesn't
        # exist on macOS. The script falls back to /dev/urandom
        # derivation in that case -- exactly what we want for tests.
        "PHY_IFACE": "nonexistent-iface-for-test",
    }
    if env:
        full_env.update(env)
    result = subprocess.run(
        ["bash", str(_SCRIPT)],
        env=full_env,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


# --- Script shape ---


def test_script_exists_and_is_executable() -> None:
    """Systemd unit's ExecStart needs +x on the script."""
    assert _SCRIPT.exists()
    mode = _SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR


# --- Wifi.json + AP credential generation ---


def test_wifi_json_written_with_both_fields(fakefs: Path) -> None:
    _run_oneshot(fakefs)
    wifi_path = fakefs / "var" / "openmarquee" / "wifi.json"
    assert wifi_path.exists()
    payload = json.loads(wifi_path.read_text())
    assert "ssid" in payload
    assert "passphrase" in payload
    assert payload["ssid"]
    assert payload["passphrase"]


def test_wifi_json_is_0600(fakefs: Path) -> None:
    """The AP passphrase is a secret; anyone with read access to
    wifi.json can join the captive-portal network without scanning
    the QR. 0600 means owner-only."""
    _run_oneshot(fakefs)
    wifi_path = fakefs / "var" / "openmarquee" / "wifi.json"
    mode = wifi_path.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0600, got 0{mode:o}"


def test_passphrase_is_16_chars(fakefs: Path) -> None:
    """16 chars * log2(64-char alphabet) ~ 96 bits of entropy."""
    _run_oneshot(fakefs)
    payload = json.loads(
        (fakefs / "var" / "openmarquee" / "wifi.json").read_text()
    )
    assert len(payload["passphrase"]) == 16


def test_passphrase_uses_safe_charset(fakefs: Path) -> None:
    """Charset: A-Z a-z 0-9 + - _ . @. No quotes, backslash, or
    ampersand -- those break shell + hostapd.conf parsing."""
    _run_oneshot(fakefs)
    payload = json.loads(
        (fakefs / "var" / "openmarquee" / "wifi.json").read_text()
    )
    assert re.match(r"^[A-Za-z0-9+_.@\-]{16}$", payload["passphrase"]), (
        f"passphrase has forbidden chars: {payload['passphrase']!r}"
    )


def test_ssid_uses_openmarquee_prefix(fakefs: Path) -> None:
    """sign name + AP SSID stays in sync principle (feedback memo).
    SSID must start with `openMarquee-`."""
    _run_oneshot(fakefs)
    payload = json.loads(
        (fakefs / "var" / "openmarquee" / "wifi.json").read_text()
    )
    assert payload["ssid"].startswith("openMarquee-"), (
        f"SSID lost prefix: {payload['ssid']!r}"
    )


# --- hostapd.conf templating ---


def test_hostapd_ssid_line_templated(fakefs: Path) -> None:
    """Source ships `ssid=openMarquee-SETUP`; oneshot must replace
    with the generated SSID."""
    _run_oneshot(fakefs)
    payload = json.loads(
        (fakefs / "var" / "openmarquee" / "wifi.json").read_text()
    )
    hostapd = (fakefs / "etc" / "hostapd" / "hostapd.conf").read_text()
    assert f"ssid={payload['ssid']}" in hostapd
    # And the placeholder is gone.
    assert "ssid=openMarquee-SETUP" not in hostapd


def test_hostapd_passphrase_line_templated(fakefs: Path) -> None:
    """Source ships `wpa_passphrase=change-me-at-first-boot`; oneshot
    must replace. This is the sweep #5 #2 closure -- without this
    the device ships with a known passphrase."""
    _run_oneshot(fakefs)
    payload = json.loads(
        (fakefs / "var" / "openmarquee" / "wifi.json").read_text()
    )
    hostapd = (fakefs / "etc" / "hostapd" / "hostapd.conf").read_text()
    assert f"wpa_passphrase={payload['passphrase']}" in hostapd
    assert "wpa_passphrase=change-me-at-first-boot" not in hostapd


# --- welcome.html templating ---


def test_welcome_html_ssid_placeholder_filled(fakefs: Path) -> None:
    _run_oneshot(fakefs)
    payload = json.loads(
        (fakefs / "var" / "openmarquee" / "wifi.json").read_text()
    )
    welcome = (fakefs / "opt" / "openmarquee" / "ui" / "welcome.html").read_text()
    assert "{{AP_SSID}}" not in welcome
    assert payload["ssid"] in welcome


def test_welcome_html_password_placeholder_filled(fakefs: Path) -> None:
    _run_oneshot(fakefs)
    payload = json.loads(
        (fakefs / "var" / "openmarquee" / "wifi.json").read_text()
    )
    welcome = (fakefs / "opt" / "openmarquee" / "ui" / "welcome.html").read_text()
    assert "{{AP_PASSWORD}}" not in welcome
    assert payload["passphrase"] in welcome


def test_welcome_html_qr_substituted_when_qrencode_available(fakefs: Path) -> None:
    """If qrencode is on the runner, the {{AP_PASSWORD_QR}} marker is
    replaced with a real SVG AND the qr-placeholder class is stripped.
    Skip on systems without qrencode (most dev macs)."""
    if shutil.which("qrencode") is None:
        pytest.skip("qrencode not installed")
    _run_oneshot(fakefs)
    welcome = (fakefs / "opt" / "openmarquee" / "ui" / "welcome.html").read_text()
    assert "{{AP_PASSWORD_QR}}" not in welcome
    # A real qrencode SVG has <svg ... and a bunch of <rect>s.
    assert "<svg" in welcome
    assert "qr-placeholder" not in welcome


def test_welcome_html_qr_left_alone_when_qrencode_missing(fakefs: Path) -> None:
    """Fallback path -- if qrencode isn't installed, welcome.js's
    dynamic QR generation handles it at runtime, so the static
    placeholder stays. The SSID/password placeholders MUST still be
    substituted regardless."""
    if shutil.which("qrencode") is not None:
        pytest.skip("qrencode is present; tested elsewhere")
    _run_oneshot(fakefs)
    welcome = (fakefs / "opt" / "openmarquee" / "ui" / "welcome.html").read_text()
    # SSID/password still substituted even without qrencode.
    assert "{{AP_SSID}}" not in welcome
    assert "{{AP_PASSWORD}}" not in welcome
    # The QR marker stays in place + the qr-placeholder class persists,
    # so welcome.js's dynamic generation has a target and the
    # PLACEHOLDER watermark CSS still applies.
    assert "{{AP_PASSWORD_QR}}" in welcome
    assert "qr-placeholder" in welcome


# --- Marker + idempotency ---


def test_bootstrap_marker_touched(fakefs: Path) -> None:
    _run_oneshot(fakefs)
    assert (fakefs / "var" / "openmarquee" / ".bootstrapped").exists()


def test_second_run_reuses_wifi_json(fakefs: Path) -> None:
    """Idempotency: if wifi.json exists, re-use the SSID + passphrase
    rather than regenerating. Protects against interrupted runs and
    against accidental re-invocation."""
    _run_oneshot(fakefs)
    payload_1 = json.loads(
        (fakefs / "var" / "openmarquee" / "wifi.json").read_text()
    )
    _run_oneshot(fakefs)
    payload_2 = json.loads(
        (fakefs / "var" / "openmarquee" / "wifi.json").read_text()
    )
    assert payload_1 == payload_2, (
        "second run regenerated credentials -- not idempotent"
    )


def test_second_run_keeps_hostapd_conf_consistent(fakefs: Path) -> None:
    """Idempotency for the templated outputs too. The hostapd.conf
    after a second run should match what the first run produced --
    not get re-templated against a different passphrase."""
    _run_oneshot(fakefs)
    hostapd_1 = (fakefs / "etc" / "hostapd" / "hostapd.conf").read_text()
    _run_oneshot(fakefs)
    hostapd_2 = (fakefs / "etc" / "hostapd" / "hostapd.conf").read_text()
    assert hostapd_1 == hostapd_2
