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
    # Pre-seed /etc/hostname + /etc/hosts so the firstboot hostname
    # rewrite has something to overwrite + update.
    (tmp_path / "etc" / "hostname").write_text("openmarquee-bootstrap\n")
    (tmp_path / "etc" / "hosts").write_text(
        "127.0.0.1\tlocalhost\n127.0.1.1\topenmarquee-bootstrap\n"
    )
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
    payload = json.loads((fakefs / "var" / "openmarquee" / "wifi.json").read_text())
    assert len(payload["passphrase"]) == 16


def test_passphrase_uses_safe_charset(fakefs: Path) -> None:
    """Charset: A-Z a-z 0-9 + - _ . @. No quotes, backslash, or
    ampersand -- those break shell + hostapd.conf parsing."""
    _run_oneshot(fakefs)
    payload = json.loads((fakefs / "var" / "openmarquee" / "wifi.json").read_text())
    assert re.match(r"^[A-Za-z0-9+_.@\-]{16}$", payload["passphrase"]), (
        f"passphrase has forbidden chars: {payload['passphrase']!r}"
    )


def test_ssid_is_mysign_device_id(fakefs: Path) -> None:
    """qarl 2026-05-12: SSID is now the MySignXXX device_id verbatim.
    Replaces the prior openMarquee-<MAC-suffix> form. The same
    identifier appears as /etc/hostname + identity.json device_id."""
    _run_oneshot(fakefs)
    payload = json.loads((fakefs / "var" / "openmarquee" / "wifi.json").read_text())
    assert re.match(r"^MySign[A-Z0-9]{3}$", payload["ssid"]), (
        f"SSID {payload['ssid']!r} doesn't match MySign[A-Z0-9]{{3}}"
    )


def test_identity_json_written_with_device_id(fakefs: Path) -> None:
    """qarl 2026-05-12: identity.json is the single source of truth
    for MySignXXX. Written 0644 (public ID, not a secret)."""
    _run_oneshot(fakefs)
    identity_path = fakefs / "var" / "openmarquee" / "identity.json"
    assert identity_path.exists(), "identity.json not written"
    payload = json.loads(identity_path.read_text())
    assert "device_id" in payload
    assert re.match(r"^MySign[A-Z0-9]{3}$", payload["device_id"])


def test_identity_json_is_0644(fakefs: Path) -> None:
    """Public ID, not a secret. 0644 lets the openmarquee service
    user read it for /api/system/info; 0600 would force a sudo path."""
    _run_oneshot(fakefs)
    identity_path = fakefs / "var" / "openmarquee" / "identity.json"
    mode = identity_path.stat().st_mode & 0o777
    assert mode == 0o644, f"identity.json mode {oct(mode)} != 0644"


def test_identity_ssid_hostname_all_match(fakefs: Path) -> None:
    """Cross-source consistency: identity.json device_id, wifi.json
    ssid, /etc/hostname, and hostapd.conf ssid line MUST all hold the
    same MySignXXX string. The single-source-of-truth claim only
    holds if the four representations stay in lockstep."""
    _run_oneshot(fakefs)
    identity = json.loads((fakefs / "var" / "openmarquee" / "identity.json").read_text())
    wifi = json.loads((fakefs / "var" / "openmarquee" / "wifi.json").read_text())
    hostname = (fakefs / "etc" / "hostname").read_text().strip()
    hostapd = (fakefs / "etc" / "hostapd" / "hostapd.conf").read_text()
    device_id = identity["device_id"]
    assert wifi["ssid"] == device_id
    assert hostname == device_id
    assert f"ssid={device_id}" in hostapd


def test_etc_hosts_127_0_1_1_points_at_mysign(fakefs: Path) -> None:
    """Without /etc/hosts mirroring the hostname, `sudo` warns
    "unable to resolve host MySignXXX" on every invocation."""
    _run_oneshot(fakefs)
    hosts = (fakefs / "etc" / "hosts").read_text()
    identity = json.loads((fakefs / "var" / "openmarquee" / "identity.json").read_text())
    device_id = identity["device_id"]
    # The 127.0.1.1 line should point at MySignXXX, not at the
    # pre-firstboot bootstrap hostname.
    assert re.search(rf"^127\.0\.1\.1\s+{device_id}$", hosts, re.MULTILINE), (
        f"127.0.1.1 line not retargeted in /etc/hosts:\n{hosts}"
    )


def test_idempotent_rerun_preserves_device_id(fakefs: Path) -> None:
    """Second run reads existing identity.json instead of generating
    a new MySignXXX. Without this property an interrupted first run
    (power-cycle between identity.json + wifi.json writes) would
    rotate the device_id on the next boot and decouple the operator's
    welcome-card sticker from the device."""
    _run_oneshot(fakefs)
    first = json.loads((fakefs / "var" / "openmarquee" / "identity.json").read_text())["device_id"]
    _run_oneshot(fakefs)
    second = json.loads((fakefs / "var" / "openmarquee" / "identity.json").read_text())["device_id"]
    assert first == second


# --- hostapd.conf templating ---


def test_hostapd_ssid_line_templated(fakefs: Path) -> None:
    """Source ships `ssid=openMarquee-SETUP`; oneshot must replace
    with the generated SSID."""
    _run_oneshot(fakefs)
    payload = json.loads((fakefs / "var" / "openmarquee" / "wifi.json").read_text())
    hostapd = (fakefs / "etc" / "hostapd" / "hostapd.conf").read_text()
    assert f"ssid={payload['ssid']}" in hostapd
    # And the placeholder is gone.
    assert "ssid=openMarquee-SETUP" not in hostapd


def test_hostapd_passphrase_line_templated(fakefs: Path) -> None:
    """Source ships `wpa_passphrase=change-me-at-first-boot`; oneshot
    must replace. This is the sweep #5 #2 closure -- without this
    the device ships with a known passphrase."""
    _run_oneshot(fakefs)
    payload = json.loads((fakefs / "var" / "openmarquee" / "wifi.json").read_text())
    hostapd = (fakefs / "etc" / "hostapd" / "hostapd.conf").read_text()
    assert f"wpa_passphrase={payload['passphrase']}" in hostapd
    assert "wpa_passphrase=change-me-at-first-boot" not in hostapd


# --- welcome.html templating ---


def test_welcome_html_ssid_placeholder_filled(fakefs: Path) -> None:
    _run_oneshot(fakefs)
    payload = json.loads((fakefs / "var" / "openmarquee" / "wifi.json").read_text())
    welcome = (fakefs / "opt" / "openmarquee" / "ui" / "welcome.html").read_text()
    assert "{{AP_SSID}}" not in welcome
    assert payload["ssid"] in welcome


def test_welcome_html_password_placeholder_filled(fakefs: Path) -> None:
    _run_oneshot(fakefs)
    payload = json.loads((fakefs / "var" / "openmarquee" / "wifi.json").read_text())
    welcome = (fakefs / "opt" / "openmarquee" / "ui" / "welcome.html").read_text()
    assert "{{AP_PASSWORD}}" not in welcome
    assert payload["passphrase"] in welcome


def test_welcome_html_device_id_placeholder_filled(fakefs: Path) -> None:
    """qarl 2026-05-12: welcome.html greets the operator with
    "Your sign: MySignXXX" so they have something memorable to
    reference. {{DEVICE_ID}} must be substituted."""
    _run_oneshot(fakefs)
    identity = json.loads((fakefs / "var" / "openmarquee" / "identity.json").read_text())
    welcome = (fakefs / "opt" / "openmarquee" / "ui" / "welcome.html").read_text()
    assert "{{DEVICE_ID}}" not in welcome
    assert identity["device_id"] in welcome


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


def test_widens_wpa_supplicant_conf_for_wifi_prefill(fakefs: Path) -> None:
    """Phase C closure: pi-gen lays /etc/wpa_supplicant/wpa_supplicant.conf
    as 600 root:root by default. The openmarquee service user can't read
    it, so wifi_prefill.py's pre-fill of settings.json fails silently.
    Oneshot widens the file to 644 so the prefill works without operator
    chmod. Safe: contents are pi-gen-baked at image build, AP-side hostapd
    + wifi.json are already on the same trust tier."""
    wpa_dir = fakefs / "etc" / "wpa_supplicant"
    wpa_dir.mkdir(parents=True, exist_ok=True)
    wpa_conf = wpa_dir / "wpa_supplicant.conf"
    # Match the pi-gen default: 600 root:root with operator's pre-flash creds.
    wpa_conf.write_text(
        "ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\n"
        "country=US\n"
        "update_config=1\n"
        'network={\n  ssid="pikazo"\n  psk="Picasso!"\n}\n'
    )
    wpa_conf.chmod(0o600)

    _run_oneshot(fakefs)

    mode = wpa_conf.stat().st_mode & 0o777
    assert mode == 0o644, f"expected 0644 after oneshot, got 0{mode:o}"
    # Content preserved -- chmod doesn't corrupt. wifi_prefill.py:129-140
    # documents the operator hint that closes the loop on the read-side.
    content = wpa_conf.read_text()
    assert "pikazo" in content
    assert "Picasso!" in content


def test_skips_wpa_widen_when_file_absent(fakefs: Path) -> None:
    """If pi-gen didn't lay wpa_supplicant.conf (operator built the image
    without WPA_ESSID), the chmod step must NOT fail the oneshot. wifi-
    prefill returns None silently; that's the absent-creds path."""
    wpa_conf = fakefs / "etc" / "wpa_supplicant" / "wpa_supplicant.conf"
    assert not wpa_conf.exists()
    _run_oneshot(fakefs)
    assert not wpa_conf.exists()


def test_second_run_reuses_wifi_json(fakefs: Path) -> None:
    """Idempotency: if wifi.json exists, re-use the SSID + passphrase
    rather than regenerating. Protects against interrupted runs and
    against accidental re-invocation."""
    _run_oneshot(fakefs)
    payload_1 = json.loads((fakefs / "var" / "openmarquee" / "wifi.json").read_text())
    _run_oneshot(fakefs)
    payload_2 = json.loads((fakefs / "var" / "openmarquee" / "wifi.json").read_text())
    assert payload_1 == payload_2, "second run regenerated credentials -- not idempotent"


def test_second_run_keeps_hostapd_conf_consistent(fakefs: Path) -> None:
    """Idempotency for the templated outputs too. The hostapd.conf
    after a second run should match what the first run produced --
    not get re-templated against a different passphrase."""
    _run_oneshot(fakefs)
    hostapd_1 = (fakefs / "etc" / "hostapd" / "hostapd.conf").read_text()
    _run_oneshot(fakefs)
    hostapd_2 = (fakefs / "etc" / "hostapd" / "hostapd.conf").read_text()
    assert hostapd_1 == hostapd_2


# --- Phase 4e-b: operator WiFi pre-config via NM keyfile -----------------


def _stage_bootfs_keyfile(fakefs: Path, ssid: str, psk: str) -> Path:
    """Drop a faux /boot/firmware/openmarquee-wifi.nmconnection in the
    fakefs so the firstboot detection path fires."""
    keyfile = fakefs / "boot" / "firmware" / "openmarquee-wifi.nmconnection"
    keyfile.parent.mkdir(parents=True, exist_ok=True)
    keyfile.write_text(
        "[connection]\n"
        f"id=openmarquee-wifi\n"
        "type=wifi\n"
        "interface-name=wlan0\n"
        "\n"
        "[wifi]\n"
        "mode=infrastructure\n"
        f"ssid={ssid}\n"
        "\n"
        "[wifi-security]\n"
        "key-mgmt=wpa-psk\n"
        f"psk={psk}\n"
    )
    return keyfile


def test_nm_keyfile_moved_from_bootfs_to_system_connections(
    fakefs: Path,
) -> None:
    _stage_bootfs_keyfile(fakefs, ssid="HomeWifi", psk="hunter2-test")
    _run_oneshot(fakefs)
    dst = fakefs / "etc" / "NetworkManager" / "system-connections" / "openmarquee-wifi.nmconnection"
    assert dst.exists(), "keyfile not copied to system-connections/"
    body = dst.read_text()
    assert "ssid=HomeWifi" in body
    assert "psk=hunter2-test" in body


def test_nm_keyfile_bootfs_copy_removed_after_move(fakefs: Path) -> None:
    """The plaintext psk on bootfs would be readable to anyone who
    mounts the SD card on another host -- remove the bootfs copy
    once it's been promoted to system-connections/."""
    keyfile = _stage_bootfs_keyfile(fakefs, ssid="x", psk="y")
    assert keyfile.exists()
    _run_oneshot(fakefs)
    assert not keyfile.exists(), "bootfs keyfile not removed after move"


def test_nm_keyfile_chmod_600_after_move(fakefs: Path) -> None:
    """NM silently rejects keyfiles wider than 0600 -- chmod is
    load-bearing."""
    _stage_bootfs_keyfile(fakefs, ssid="x", psk="y")
    _run_oneshot(fakefs)
    dst = fakefs / "etc" / "NetworkManager" / "system-connections" / "openmarquee-wifi.nmconnection"
    mode = stat.S_IMODE(dst.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_no_keyfile_no_op(fakefs: Path) -> None:
    """When bootfs has no keyfile, the firstboot run leaves
    system-connections/ untouched. AP-only path stays intact."""
    _run_oneshot(fakefs)
    sysconn = fakefs / "etc" / "NetworkManager" / "system-connections"
    if sysconn.exists():
        assert not list(sysconn.iterdir()), (
            "system-connections/ should be empty when no bootfs keyfile staged"
        )


# --- r34: mgmt-WiFi keyfile drop (§5d) -----------------------------------


def _stage_mgmt_bootfs_keyfile(fakefs: Path, ssid: str, psk: str) -> Path:
    """Drop a faux /boot/firmware/openmarquee-mgmt-wifi.nmconnection
    in the fakefs so the firstboot §5d detection path fires.

    Mirrors _stage_bootfs_keyfile but writes the mgmt-side keyfile
    with interface-name=wlan-dongle (the udev-renamed USB dongle)
    and route-metric=50 (preferred for default route over the
    sign-WiFi's 600)."""
    keyfile = fakefs / "boot" / "firmware" / "openmarquee-mgmt-wifi.nmconnection"
    keyfile.parent.mkdir(parents=True, exist_ok=True)
    keyfile.write_text(
        "[connection]\n"
        "id=openmarquee-mgmt-wifi\n"
        "type=wifi\n"
        "interface-name=wlan-dongle\n"
        "autoconnect=true\n"
        "autoconnect-priority=10\n"
        "\n"
        "[wifi]\n"
        "mode=infrastructure\n"
        f"ssid={ssid}\n"
        "\n"
        "[wifi-security]\n"
        "key-mgmt=wpa-psk\n"
        f"psk={psk}\n"
        "\n"
        "[ipv4]\n"
        "method=auto\n"
        "route-metric=50\n"
    )
    return keyfile


def test_mgmt_nm_keyfile_moved_from_bootfs_to_system_connections(
    fakefs: Path,
) -> None:
    _stage_mgmt_bootfs_keyfile(fakefs, ssid="InstallerWifi", psk="mgmt-test")
    _run_oneshot(fakefs)
    dst = (
        fakefs
        / "etc"
        / "NetworkManager"
        / "system-connections"
        / "openmarquee-mgmt-wifi.nmconnection"
    )
    assert dst.exists(), "mgmt keyfile not copied to system-connections/"
    body = dst.read_text()
    assert "ssid=InstallerWifi" in body
    assert "psk=mgmt-test" in body
    assert "interface-name=wlan-dongle" in body, (
        "mgmt keyfile must pin to wlan-dongle (the udev-renamed dongle)"
    )


def test_mgmt_nm_keyfile_bootfs_copy_removed_after_move(fakefs: Path) -> None:
    """Mirrors the sign-WiFi safety: plaintext psk on bootfs is
    readable by anyone who mounts the SD card -- remove the bootfs
    copy once promoted to system-connections/."""
    keyfile = _stage_mgmt_bootfs_keyfile(fakefs, ssid="x", psk="y")
    assert keyfile.exists()
    _run_oneshot(fakefs)
    assert not keyfile.exists(), "bootfs mgmt keyfile not removed after move"


def test_mgmt_nm_keyfile_chmod_600_after_move(fakefs: Path) -> None:
    """NM silently rejects keyfiles wider than 0600 -- chmod is
    load-bearing for the mgmt path too."""
    _stage_mgmt_bootfs_keyfile(fakefs, ssid="x", psk="y")
    _run_oneshot(fakefs)
    dst = (
        fakefs
        / "etc"
        / "NetworkManager"
        / "system-connections"
        / "openmarquee-mgmt-wifi.nmconnection"
    )
    mode = stat.S_IMODE(dst.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_both_keyfiles_land_independently(fakefs: Path) -> None:
    """When BOTH sign-WiFi AND mgmt-WiFi keyfiles are staged at
    burn time, firstboot must promote both -- the §5d block can't
    short-circuit on §5c's success or vice versa. Regression guard
    against a future refactor that conflates the two paths."""
    _stage_bootfs_keyfile(fakefs, ssid="SignWifi", psk="sign-pass")
    _stage_mgmt_bootfs_keyfile(fakefs, ssid="MgmtWifi", psk="mgmt-pass")
    _run_oneshot(fakefs)
    sysconn = fakefs / "etc" / "NetworkManager" / "system-connections"
    sign_dst = sysconn / "openmarquee-wifi.nmconnection"
    mgmt_dst = sysconn / "openmarquee-mgmt-wifi.nmconnection"
    assert sign_dst.exists(), "sign-WiFi keyfile dropped when both staged"
    assert mgmt_dst.exists(), "mgmt-WiFi keyfile dropped when both staged"
    assert "ssid=SignWifi" in sign_dst.read_text()
    assert "ssid=MgmtWifi" in mgmt_dst.read_text()


def test_no_mgmt_keyfile_no_mgmt_op(fakefs: Path) -> None:
    """When mgmt keyfile is absent (operator didn't pass
    --mgmt-wifi-ssid at burn), the §5d block is a no-op even if
    the sign-WiFi keyfile IS present. No spurious mgmt file
    appears in system-connections/."""
    _stage_bootfs_keyfile(fakefs, ssid="SignOnly", psk="sign-only-pass")
    _run_oneshot(fakefs)
    sysconn = fakefs / "etc" / "NetworkManager" / "system-connections"
    mgmt_dst = sysconn / "openmarquee-mgmt-wifi.nmconnection"
    assert not mgmt_dst.exists(), (
        "mgmt keyfile created spuriously when no mgmt-bootfs-keyfile staged"
    )


def test_nm_keyfile_psk_not_logged_to_stdout(fakefs: Path) -> None:
    """Defense-in-depth: the psk shouldn't appear in the firstboot
    log even though it lives in the keyfile contents. The SSID is OK
    to log (it's broadcast over the air anyway)."""
    _stage_bootfs_keyfile(fakefs, ssid="MyHome", psk="super-secret-psk-1234")
    stdout = _run_oneshot(fakefs)
    assert "super-secret-psk-1234" not in stdout
    assert "MyHome" in stdout, "SSID should be logged for operator visibility"
