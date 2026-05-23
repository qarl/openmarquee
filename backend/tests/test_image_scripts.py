"""Dry-run coverage for scripts/build-image.sh + scripts/flash-sd.sh
(Phase B.6).

Both scripts can't actually run in pytest -- build-image.sh wants
Docker + sudo + a network fetch, flash-sd.sh wants an SD card to
write to. We test --dry-run mode + arg validation + safety guards.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_BUILD = _REPO_ROOT / "scripts" / "build-image.sh"
_FLASH = _REPO_ROOT / "scripts" / "flash-sd.sh"


# --- build-image.sh ---------------------------------------------------------


def test_build_image_exists_and_executable() -> None:
    import stat

    assert _BUILD.exists()
    assert _BUILD.stat().st_mode & stat.S_IXUSR


def test_build_image_help() -> None:
    """--help prints usage including the --dry-run and --ssh-key flags."""
    r = subprocess.run(["bash", str(_BUILD), "--help"], check=True, capture_output=True, text=True)
    assert "--dry-run" in r.stdout
    assert "--ssh-key" in r.stdout


def test_build_image_unknown_arg_rejected() -> None:
    r = subprocess.run(
        ["bash", str(_BUILD), "--no-such-flag"], check=False, capture_output=True, text=True
    )
    assert r.returncode == 2


def test_build_image_dry_run_clones_pigen() -> None:
    r = subprocess.run(
        ["bash", str(_BUILD), "--dry-run"], check=True, capture_output=True, text=True
    )
    # The clone command should appear in dry-run output (or a fetch if
    # the workdir already exists from a previous local run -- accept
    # either).
    assert ("git clone" in r.stdout) or ("git -C" in r.stdout)
    assert "pi-gen" in r.stdout


def test_build_image_dry_run_stages_config_and_stage() -> None:
    r = subprocess.run(
        ["bash", str(_BUILD), "--dry-run"], check=True, capture_output=True, text=True
    )
    assert "Stage pi-gen.config" in r.stdout
    assert "Stage stage-openmarquee" in r.stdout


def test_build_image_dry_run_skips_desktop_stages() -> None:
    r = subprocess.run(
        ["bash", str(_BUILD), "--dry-run"], check=True, capture_output=True, text=True
    )
    # stage3, stage4, stage5 all get SKIP + SKIP_IMAGES markers.
    for stage in ("stage3", "stage4", "stage5"):
        assert f"{stage}/SKIP" in r.stdout


def test_build_image_dry_run_invokes_pigen_build_via_docker() -> None:
    r = subprocess.run(
        ["bash", str(_BUILD), "--dry-run"], check=True, capture_output=True, text=True
    )
    assert "sudo ./build-docker.sh" in r.stdout


def test_build_image_dry_run_outputs_to_tmp_by_default() -> None:
    r = subprocess.run(
        ["bash", str(_BUILD), "--dry-run"], check=True, capture_output=True, text=True
    )
    assert "/tmp/openmarquee-pi-image-" in r.stdout
    assert ".img.xz" in r.stdout


def test_build_image_rejects_missing_ssh_key() -> None:
    r = subprocess.run(
        ["bash", str(_BUILD), "--dry-run", "--ssh-key", "/nonexistent"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0
    assert "not found" in r.stderr or "not found" in r.stdout


# --- flash-sd.sh ------------------------------------------------------------


def test_flash_exists_and_executable() -> None:
    import stat

    assert _FLASH.exists()
    assert _FLASH.stat().st_mode & stat.S_IXUSR


def test_flash_help() -> None:
    r = subprocess.run(["bash", str(_FLASH), "--help"], check=True, capture_output=True, text=True)
    assert "DANGEROUS" in r.stdout
    assert "<image>" in r.stdout


def test_flash_requires_two_positional_args() -> None:
    r = subprocess.run(["bash", str(_FLASH)], check=False, capture_output=True, text=True)
    assert r.returncode == 2
    assert "usage" in r.stderr.lower() or "usage" in r.stdout.lower()


def test_flash_rejects_missing_image(tmp_path: Path) -> None:
    """Guard 1: nonexistent image file."""
    r = subprocess.run(
        ["bash", str(_FLASH), "--dry-run", "--yes", str(tmp_path / "nope.img"), "/dev/null"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0
    assert "image not found" in r.stderr


def test_flash_rejects_missing_device(tmp_path: Path) -> None:
    """Guard 2: nonexistent device path."""
    img = tmp_path / "fake.img"
    img.write_bytes(b"x" * 64)
    r = subprocess.run(
        ["bash", str(_FLASH), "--dry-run", "--yes", str(img), "/dev/this-disk-cannot-exist-xyz"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0


def test_flash_refuses_system_disk(tmp_path: Path) -> None:
    """Guard 3: refuse /dev/disk0 on Mac / root disk on Linux. Even
    in dry-run -- the operator must NEVER even practice on the system
    disk."""
    img = tmp_path / "fake.img"
    img.write_bytes(b"x" * 64)
    import platform

    if platform.system() == "Darwin":
        sys_disk = "/dev/disk0"
    else:
        # On Linux, we can't reliably predict the root disk name in CI;
        # skip on non-Mac platforms.
        pytest.skip("system-disk-refusal test is macOS-specific")
    r = subprocess.run(
        ["bash", str(_FLASH), "--dry-run", "--yes", str(img), sys_disk],
        check=False,
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0
    assert "REFUSED" in r.stderr or "system" in r.stderr


def test_flash_rejects_unknown_flag(tmp_path: Path) -> None:
    img = tmp_path / "fake.img"
    img.write_bytes(b"x" * 64)
    r = subprocess.run(
        ["bash", str(_FLASH), "--no-such-flag", str(img), "/dev/null"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 2


def test_flash_refuses_loopback_devices(tmp_path: Path) -> None:
    """B.6-followup Guard 2a: /dev/loop* is a file-backed mount;
    dd'ing to it would corrupt the underlying file in ways that are
    non-obvious to recover from. Refuse by NAME before the device-
    exists check so the operator sees a clear "loopback refused"
    error, not a generic "not found"."""
    img = tmp_path / "fake.img"
    img.write_bytes(b"x" * 64)
    r = subprocess.run(
        ["bash", str(_FLASH), "--dry-run", "--yes", str(img), "/dev/loop42"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0
    assert "loopback" in r.stderr.lower()


def test_flash_refuses_device_mapper_devices(tmp_path: Path) -> None:
    """B.6-followup Guard 2a: /dev/dm-* + /dev/mapper/* are device-
    mapper targets (LUKS-encrypted volumes, LVM logical volumes).
    dd'ing to one would silently corrupt the mapped backing -- LUKS
    headers gone, LVM metadata gone."""
    img = tmp_path / "fake.img"
    img.write_bytes(b"x" * 64)
    for dev in ("/dev/dm-0", "/dev/mapper/cryptroot"):
        r = subprocess.run(
            ["bash", str(_FLASH), "--dry-run", "--yes", str(img), dev],
            check=False,
            capture_output=True,
            text=True,
        )
        assert r.returncode != 0, f"should refuse {dev}"
        assert "device-mapper" in r.stderr.lower(), (
            f"missing device-mapper message for {dev}: {r.stderr!r}"
        )


def test_flash_partition_to_parent_disk_regex_handles_nvme_and_mmcblk() -> None:
    """Task #382: Guard 3's partition-to-parent-disk regex must handle
    the NVMe (`nvme0n1p2`) and mmcblk (`mmcblk0p2`) naming conventions,
    not just SATA-style (`sda2`). The prior regex `s/[0-9]+$//` stripped
    only digits, so a partition path like `/dev/nvme0n1p2` derived a
    bogus parent `/dev/nvme0n1p` -- not matching the operator's `$DEVICE`
    of `/dev/nvme0n1` -- so the system-disk refusal silently missed on
    NVMe hosts. Guard 3b's mounted-fs check caught the actual exploit
    surface, but Guard 3 itself was lying. Fix: `s/p?[0-9]+$//`."""
    cases = [
        ("/dev/sda2", "/dev/sda"),
        ("/dev/sda10", "/dev/sda"),
        ("/dev/nvme0n1p2", "/dev/nvme0n1"),
        ("/dev/nvme1n2p15", "/dev/nvme1n2"),
        ("/dev/mmcblk0p2", "/dev/mmcblk0"),
        ("/dev/mmcblk1p1", "/dev/mmcblk1"),
        # Passthrough: a bare device path with no partition suffix
        # stays unchanged. findmnt always returns a partition path for
        # `/` on a partitioned root, so this isn't load-bearing in
        # practice -- but verifies the regex doesn't overreach.
        ("/dev/sda", "/dev/sda"),
    ]
    for partition, expected_parent in cases:
        result = subprocess.run(
            ["sed", "-E", "s/p?[0-9]+$//"],
            input=partition,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert result == expected_parent, (
            f"regex misderived parent for {partition!r}: "
            f"got {result!r}, expected {expected_parent!r}"
        )


def test_flash_supports_yes_to_skip_prompt(tmp_path: Path) -> None:
    """--yes skips the typed-confirmation prompt. Useful in CI but the
    spirit of the safety guards still holds (size + system-disk checks
    fire regardless)."""
    r = subprocess.run(["bash", str(_FLASH), "--help"], check=True, capture_output=True, text=True)
    assert "--yes" in r.stdout or "SKIP_CONFIRM" in r.stdout
