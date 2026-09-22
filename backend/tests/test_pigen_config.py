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
            "openssh-server",
            "base-level sshd for tether-independent recovery (first-light "
            "hardening 2026-09-19); Pi OS Lite ships it but we pin it so the "
            "04-ssh-user enable + baked key never rest on a base-image assumption",
        ),
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


def test_all_substage_run_scripts_are_git_executable() -> None:
    """EVERY pi-gen substage *-run.sh must be tracked in git as mode
    100755. pi-gen (build.sh) SILENTLY skips run scripts without the
    exec bit, so a 100644 script no-ops its whole substage on a clean
    checkout — e.g. 02-run.sh bakes cma=256M / gpu_mem=128, so a
    non-exec 02-run.sh ships an image with the wrong memory split + no
    splash, with NO build error.

    Regression guard for 2026-07-09, where 01/02/03-run.sh were 100644
    and shipped skipped on a clean build. Checks the GIT-tracked mode
    (`git ls-files -s`), NOT the working-tree st_mode — the whole bug
    was that stale local perms (755 on disk) hid the committed 644, so
    an st_mode check would have passed locally while CI/clean-checkout
    silently skipped the substage. The existing st_mode tests above only
    covered prerun.sh + 00-run.sh, which is why the 01/02/03 gap slipped.
    """
    import subprocess

    result = subprocess.run(
        ["git", "ls-files", "-s", "--", str(_STAGE_DIR)],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip("git unavailable or not a repo checkout")

    offenders = []
    for line in result.stdout.splitlines():
        # "<mode> <sha> <stage>\t<path>"
        meta, _, path = line.partition("\t")
        mode = meta.split()[0]
        if path.endswith("-run.sh") and mode != "100755":
            offenders.append(f"{path}={mode}")
    assert not offenders, (
        "pi-gen substage *-run.sh must be committed 100755 (executable) or "
        f"pi-gen silently skips the whole substage: {offenders}"
    )


# --- USB-gadget networking (dwc2 + g_ether), 2026-09-16 ---
#
# The dwc2 gadget lets the Pi be reached as <sign-name>.local over a USB
# cable (the wired recovery path the Pi Zero 2 W lacks). Two halves, both
# regression-guarded here at the static level (a loop-mount of the built
# image is out of scope for a unit test — that lives in the manual
# build-completeness check documented in images/openmarquee/README.md):
#   1. boot-config: dtoverlay=dwc2 (config.txt) + modules-load=dwc2,g_ether
#      right after rootwait (cmdline.txt) — the byte-level patch behavior
#      is unit-tested by 02-boot-config/test-boot-config.sh.
#   2. usb0 bring-up: the 05-usb-gadget substage bakes a link-local NM
#      profile bound to interface-name=usb0.
# The tests below assert the two halves are actually WIRED (a patch fn
# that exists but is never CALLED ships nothing — same failure shape as
# the exec-bit regression above).

_BOOT_CONFIG = _STAGE_DIR / "02-boot-config"


def test_boot_config_lib_defines_dwc2_functions() -> None:
    """boot-config-lib.sh must define the dwc2 config.txt patch and the
    modules-load cmdline.txt patch, with the exact kernel-side literals."""
    lib = (_BOOT_CONFIG / "boot-config-lib.sh").read_text()
    assert "patch_config_txt_dwc2()" in lib, "patch_config_txt_dwc2 not defined"
    assert "patch_cmdline_txt_modules()" in lib, "patch_cmdline_txt_modules not defined"
    assert "dtoverlay=dwc2" in lib, "dwc2 overlay literal missing from lib"
    assert "modules-load=dwc2,g_ether" in lib, "g_ether module literal missing from lib"
    # First-light fix 2026-09-19: the gadget MUST be dr_mode=peripheral, not
    # a bare dwc2 (defaults to otg) and not the stock [cm4] dr_mode=host. The
    # old presence-only patch shipped no [all] peripheral line at all.
    assert "dr_mode=peripheral" in lib, (
        "patch_config_txt_dwc2 must enforce dr_mode=peripheral (first-light fix) — "
        "a bare dwc2 defaults to otg and the Zero 2 W won't enumerate the gadget"
    )


def test_boot_config_runner_invokes_dwc2() -> None:
    """02-run.sh (the pi-gen substage runner) must CALL both dwc2 patch
    functions — defining them isn't enough, an uncalled patch bakes
    nothing into the image."""
    runner = (_BOOT_CONFIG / "02-run.sh").read_text()
    assert "patch_config_txt_dwc2" in runner, (
        "02-run.sh must call patch_config_txt_dwc2 or the built image ships no dtoverlay=dwc2"
    )
    assert "patch_cmdline_txt_modules" in runner, (
        "02-run.sh must call patch_cmdline_txt_modules or the built image "
        "ships no modules-load=dwc2,g_ether"
    )


def test_usb_gadget_substage_run_sh_exists() -> None:
    """The 05-usb-gadget substage must exist (its run.sh is separately
    checked for git mode 100755 by the substage-exec test above)."""
    run = _STAGE_DIR / "05-usb-gadget" / "05-run.sh"
    assert run.exists(), "05-usb-gadget/05-run.sh missing"
    assert "system-connections" in run.read_text(), (
        "05-run.sh must install the usb0 NM profile under system-connections"
    )


def test_usb0_nmconnection_is_linklocal_and_bound() -> None:
    """The baked usb0 profile must bind ONLY to interface-name=usb0 (so it
    coexists with wlan0) and use link-local addressing (no DHCP server
    needed; mDNS resolves <sign-name>.local over the cable)."""
    import configparser

    conn = (
        _STAGE_DIR
        / "05-usb-gadget"
        / "files"
        / "etc"
        / "NetworkManager"
        / "system-connections"
        / "usb0.nmconnection"
    )
    assert conn.exists(), f"missing baked NM profile at {conn}"
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.read_string(conn.read_text())
    assert parser["connection"]["interface-name"] == "usb0", (
        "usb0 profile must bind to interface-name=usb0 so it can't attach "
        "to wlan0 / the AP interface"
    )
    assert parser["connection"]["type"] == "ethernet"
    assert parser["ipv4"]["method"] == "link-local", (
        "usb0 must use link-local IPv4 (169.254/16) — the minimal "
        "zero-config address the tethered host self-assigns to match"
    )


# --- cloud-init enablement + NoCloud seed (first-light fix, 2026-09-19) ---
#
# First burned card: cloud-init was apt-installed but never RAN — empty
# /var/log/cloud-init.log, stock `raspberrypi` hostname, no bundle extract,
# black screen. Root cause was two gaps, both now owned by the 06-cloud-init
# substage. As with the dwc2 tests above, these are static wiring guards (a
# built-image loop-mount is out of scope for a unit test; admin's live
# serial-console run is the behavioral gate). A patch that exists but is
# never wired ships nothing.

_CLOUD_INIT = _STAGE_DIR / "06-cloud-init"


def test_cloud_init_substage_enables_units() -> None:
    """06-run.sh must ENABLE the cloud-init systemd units (the gap that
    caused first-light: installed-but-never-enabled → nothing in
    multi-user.target.wants) and clear any disable marker. The unit set was
    renamed across cloud-init versions (trixie ships 24.x), so both the
    classic and renamed names must be covered by the tolerant enable loop."""
    run = (_CLOUD_INIT / "06-run.sh").read_text()
    assert "systemctl enable" in run, "06-run.sh must enable cloud-init units"
    for unit in (
        "cloud-init-local.service",
        "cloud-init.service",
        "cloud-init-network.service",  # 24.x rename
        "cloud-config.service",
        "cloud-final.service",
    ):
        assert unit in run, f"06-run.sh must handle the cloud-init unit {unit}"
    assert "rm -f /etc/cloud/cloud-init.disabled" in run, (
        "06-run.sh must remove any /etc/cloud/cloud-init.disabled marker"
    )
    assert "cloud-init.target" in run, (
        "06-run.sh must also enable cloud-init.target (the belt that pulls the "
        "stage services into multi-user boot)"
    )
    # Fail-loud if the package somehow isn't present, rather than baking a
    # silently-non-provisioning image again.
    assert "no cloud-init units found" in run, (
        "06-run.sh must fail the build loudly if no cloud-init units exist"
    )


def test_cloud_init_nocloud_cfg_seeds_from_boot_partition() -> None:
    """The cloud.cfg.d drop-in must force the NoCloud datasource and seed it
    from the FAT boot partition (bootfs = /boot/firmware on trixie) — Debian
    cloud-init does NOT auto-seed from there like Ubuntu's Pi images, so the
    staged user-data/meta-data on the boot partition would never be read.
    The trailing slash is load-bearing (cloud-init appends user-data etc.)."""
    cfg = _CLOUD_INIT / "files" / "etc" / "cloud" / "cloud.cfg.d" / "99_openmarquee.cfg"
    assert cfg.exists(), f"missing NoCloud drop-in at {cfg}"
    text = cfg.read_text()
    assert re.search(r"^datasource_list:\s*\[\s*NoCloud\s*\]", text, re.M), (
        "cfg must force datasource_list: [ NoCloud ]"
    )
    assert re.search(r"^\s*seedfrom:\s*file:///boot/firmware/\s*$", text, re.M), (
        "cfg must seed NoCloud from file:///boot/firmware/ (trailing slash required)"
    )


def test_cloud_init_runner_installs_the_cfg() -> None:
    """06-run.sh must actually INSTALL the drop-in into the rootfs — a cfg
    file that ships in the substage but is never copied does nothing."""
    run = (_CLOUD_INIT / "06-run.sh").read_text()
    assert "/etc/cloud/cloud.cfg.d/99_openmarquee.cfg" in run, (
        "06-run.sh must install 99_openmarquee.cfg into /etc/cloud/cloud.cfg.d"
    )


# --- base-level ssh (first-light hardening 2026-09-19) ---
#
# A burned card must be reachable over ssh (home wifi or USB tether) EVEN IF
# cloud-init never runs — it was the ONLY thing enabling ssh before. Three
# pieces, all regression-guarded: (1) openssh-server pinned (package list
# above), (2) 04-ssh-user enables ssh.service at build, (3) build-image.sh
# --ssh-key bakes the operator key into the rootfs + 04-ssh-user installs it.

_SSH_USER = _STAGE_DIR / "04-ssh-user"


def test_ssh_service_enabled_at_base() -> None:
    """04-run.sh must enable ssh.service in the chroot, so ssh works without
    cloud-init (previously the only thing that enabled it)."""
    run = (_SSH_USER / "04-run.sh").read_text()
    assert "systemctl enable ssh.service" in run, (
        "04-run.sh must enable ssh.service at base (tether-independent recovery)"
    )


def test_ssh_operator_key_baked_into_rootfs() -> None:
    """04-run.sh must install the staged operator key into the rootfs
    authorized_keys when present, and build-image.sh --ssh-key must stage it.
    Without BOTH, base-enabled ssh has no key to authenticate and recovery is
    dead if cloud-init hiccups."""
    run = (_SSH_USER / "04-run.sh").read_text()
    assert "operator-authorized-keys" in run, (
        "04-run.sh must consume the staged operator-authorized-keys"
    )
    assert "/home/openmarquee/.ssh/authorized_keys" in run, (
        "04-run.sh must install the key to /home/openmarquee/.ssh/authorized_keys"
    )
    build = (_REPO_ROOT / "scripts" / "build-image.sh").read_text()
    assert "operator-authorized-keys" in build, (
        "build-image.sh --ssh-key must stage operator-authorized-keys for 04-ssh-user"
    )
