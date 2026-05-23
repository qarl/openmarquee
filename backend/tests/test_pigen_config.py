"""Pi-gen config + package-list structural invariants (Batch B.1).

These tests don't actually build an image (pi-gen needs Docker + a
trixie chroot — way beyond a unit test). They guard the structural
contract of images/openmarquee/: the config file parses, the right
keys are pinned, the package list contains everything the openMarquee
runtime needs.

If pi-gen ever changes its config-file shape (unlikely; it's been a
sourced shell script for years), this test will fail loudly rather
than the build crashing mid-stage on a real Pi.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_IMAGE_DIR = _REPO_ROOT / "images" / "openmarquee"
_STAGE_DIR = _IMAGE_DIR / "stage-openmarquee"


def _parse_shell_config(path: Path) -> dict[str, str]:
    """Parse a pi-gen-style sourced-shell config into a key→value dict.

    Ignores comments and blank lines. Supports both `KEY='value'` and
    `KEY=value` forms. Doesn't try to evaluate shell expansion -- the
    config keys we care about are all plain literals.
    """
    out: dict[str, str] = {}
    pattern = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*)$")
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = pattern.match(stripped)
        if not m:
            continue
        key, raw = m.group(1), m.group(2).strip()
        if (raw.startswith("'") and raw.endswith("'")) or (
            raw.startswith('"') and raw.endswith('"')
        ):
            raw = raw[1:-1]
        out[key] = raw
    return out


@pytest.fixture(scope="module")
def config() -> dict[str, str]:
    return _parse_shell_config(_IMAGE_DIR / "pi-gen.config")


@pytest.fixture(scope="module")
def packages() -> list[str]:
    raw = (_STAGE_DIR / "00-install-packages" / "00-packages").read_text()
    return [
        line.strip()
        for line in raw.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


# --- pi-gen.config invariants ---


def test_config_pins_image_name(config: dict[str, str]) -> None:
    """IMG_NAME is the filename root; renaming it silently would
    break the build artifact + flash scripts (B.6)."""
    assert config["IMG_NAME"] == "openmarquee"


def test_config_pins_release_to_trixie(config: dict[str, str]) -> None:
    """Pi argon2id params (project_pi_argon2_params memo) are measured
    on trixie's argon2-cffi. Bumping to a different release without
    re-measuring would drift the latency budget."""
    assert config["RELEASE"] == "trixie"


def test_config_pins_arch_to_arm64(config: dict[str, str]) -> None:
    """Pi Zero 2 W BCM2710A1 is ARMv8. The original Pi Zero (ARMv6) is
    explicitly unsupported -- 1080p HDMI rules it out anyway."""
    assert config["TARGET_ARCH"] == "arm64"


def test_config_uses_openmarquee_username(config: dict[str, str]) -> None:
    """system/openmarquee-backend.service runs as user `openmarquee`;
    the FIRST_USER_NAME must match or the service won't have ownership
    of /opt/openmarquee/ or /var/openmarquee/."""
    assert config["FIRST_USER_NAME"] == "openmarquee"


def test_config_hostname_matches_service_default(config: dict[str, str]) -> None:
    """Pre-cloud-init hostname seed; cloud-init (B.2) replaces with
    openmarquee-<hex>. The seed must be 'openmarquee' so the AP SSID +
    Tailscale name stay in sync principle (feedback memo) holds even
    if cloud-init hasn't run yet."""
    assert config["HOSTNAME"] == "openmarquee"


def test_config_locale_is_utf8(config: dict[str, str]) -> None:
    """Backend code assumes UTF-8 throughout (filenames, text slides).
    A non-UTF-8 default locale on the device would surface as cryptic
    encoding errors in ContentStorage.save_text_slide."""
    assert config["LOCALE_DEFAULT"] == "en_US.UTF-8"


# --- package-list invariants ---


@pytest.mark.parametrize(
    "package, reason",
    [
        ("hostapd", "AP mode on ap0 (captive portal)"),
        ("dnsmasq", "DHCP + DNS intercept for captive portal"),
        ("iptables", "captive-portal redirect-to-welcome.html"),
        ("python3", "backend runtime"),
        ("python3-venv", "/opt/openmarquee/venv via install.sh (B.3)"),
        ("python3-pip", "pip install -e . in venv"),
        ("ffmpeg", "video transcode pipeline"),
        ("qrencode", "B.4 first-boot AP-password QR code"),
        ("wpasupplicant", "station-mode wlan0 WiFi join"),
        ("iw", "ap0 virtual-interface creation"),
        ("cloud-init", "B.2 first-boot config; not in Pi OS Lite default"),
        (
            "wireless-tools",
            "Phase C: wifi_prefill.py shells out to iwgetid which lives "
            "in wireless-tools (NOT iw -- different package, modern vs legacy)",
        ),
    ],
)
def test_packages_includes_runtime_essential(
    packages: list[str], package: str, reason: str
) -> None:
    assert package in packages, f"missing essential package: {package} ({reason})"


def test_packages_excludes_desktop_stack(packages: list[str]) -> None:
    """openMarquee runs framebuffer-direct via vc4-fkms-v3d.
    Pulling in X11 / LXDE / desktop-environment packages would
    bloat the image, slow boot, and steal RAM. Pi Zero 2 W has 512MB."""
    desktop_packages = {
        "lxde",
        "lxde-core",
        "xserver-xorg",
        "xinit",
        "x11-common",
        "lightdm",
        "gdm3",
        "raspberrypi-ui-mods",
    }
    overlap = set(packages) & desktop_packages
    assert not overlap, f"desktop packages must not appear: {overlap}"


def test_packages_no_duplicates(packages: list[str]) -> None:
    """Duplicates aren't fatal to apt, but they're noise. The package
    list should be a clean set."""
    assert len(packages) == len(set(packages)), (
        f"duplicate package(s) in 00-packages: {[p for p in packages if packages.count(p) > 1]}"
    )


# --- pi-gen stage structure ---


def test_export_image_marker_exists() -> None:
    """Without this empty file, pi-gen won't emit a .img at the end
    of stage-openmarquee. The build would complete but produce no
    flashable artifact."""
    assert (_STAGE_DIR / "EXPORT_IMAGE").exists()


def test_prerun_sh_is_executable() -> None:
    """pi-gen invokes prerun.sh directly (not via `bash`); the file
    must have +x bit set or the stage silently no-ops."""
    import stat

    prerun = _STAGE_DIR / "prerun.sh"
    assert prerun.exists()
    mode = prerun.stat().st_mode
    assert mode & stat.S_IXUSR, "prerun.sh must be executable"


def test_packages_substage_run_sh_is_executable() -> None:
    """Same +x requirement for substage run scripts."""
    import stat

    run = _STAGE_DIR / "00-install-packages" / "00-run.sh"
    assert run.exists()
    mode = run.stat().st_mode
    assert mode & stat.S_IXUSR, "00-run.sh must be executable"
