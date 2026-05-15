"""Dry-run coverage for scripts/install.sh (Phase B.3).

The real install.sh mutates /opt, /var, /etc, systemd, iptables — far
out of pytest's reach. The script supports --dry-run + --root <prefix>
so we can invoke it from pytest, capture stdout, and assert the
right SET of high-level actions appears in the right order, against
a tmpdir prefix.

These tests can't catch a runtime error on the real Pi (e.g. a
package not installed, a permission denied), but they DO catch
silent regressions in the action list — someone editing install.sh
to drop the systemctl enable step, for instance, fails the relevant
test here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_INSTALL_SH = _REPO_ROOT / "scripts" / "install.sh"


def _run_dry(tmp_path: Path) -> str:
    """Invoke install.sh in dry-run mode against a tmpdir prefix."""
    result = subprocess.run(
        [
            "bash",
            str(_INSTALL_SH),
            "--dry-run",
            "--root",
            str(tmp_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


@pytest.fixture(scope="module")
def dry_output(tmp_path_factory: pytest.TempPathFactory) -> str:
    return _run_dry(tmp_path_factory.mktemp("install-test"))


# --- Top-level shape ---


def test_install_sh_exists_and_is_executable() -> None:
    """install.sh must exist + be executable. cloud-init runs it as
    `bash /opt/openmarquee/scripts/install.sh`, so technically the
    +x isn't strictly required — but a non-executable script is a
    surprise the next maintainer doesn't need."""
    import stat

    assert _INSTALL_SH.exists()
    mode = _INSTALL_SH.stat().st_mode
    assert mode & stat.S_IXUSR, "install.sh must be executable"


def test_install_sh_supports_help_flag() -> None:
    """The --help / -h flag is the conventional escape hatch and the
    only place we document the args. Missing one is a UX bug."""
    result = subprocess.run(
        ["bash", str(_INSTALL_SH), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "--dry-run" in result.stdout
    assert "--root" in result.stdout


def test_install_sh_rejects_unknown_arg() -> None:
    """Unknown args must exit non-zero so a typo doesn't silently
    install with the wrong target."""
    result = subprocess.run(
        ["bash", str(_INSTALL_SH), "--what-now"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "unknown" in result.stderr.lower()


def test_install_sh_rejects_root_without_dry_run(tmp_path: Path) -> None:
    """--root is for off-device testing only. Without --dry-run, iptables
    and systemctl would still hit the LIVE host while paths point at a
    tmpdir -- worst-of-both-worlds. Subagent review caught this gap;
    guard added in fix commit."""
    result = subprocess.run(
        ["bash", str(_INSTALL_SH), "--root", str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "requires --dry-run" in result.stderr


# --- Required steps surface in dry-run output ---


def test_dry_run_creates_state_directories(dry_output: str, tmp_path: Path) -> None:
    """Step 1 -- /var/openmarquee + /var/lib/openmarquee. Without these
    the backend's atomic_write_text falls over and the bootcmd's
    idempotency marker has no home."""
    assert "Ensure state directories" in dry_output
    assert "mkdir -p" in dry_output
    assert "/var/openmarquee" in dry_output
    assert "/var/lib/openmarquee" in dry_output


def test_dry_run_state_dir_permissions_are_tight(dry_output: str) -> None:
    """0750 on /var/openmarquee — group+other can't read
    settings.json (carries AP password + Tailscale auth key)."""
    assert "chmod 0750" in dry_output


def test_dry_run_creates_python_venv(dry_output: str) -> None:
    """Step 2 -- /opt/openmarquee/venv. python3 -m venv must appear
    AND pip install -e on backend/."""
    assert "python3 -m venv" in dry_output
    assert "pip install --upgrade" in dry_output
    assert "-e " in dry_output
    # Phase 4a 2026-05-15: without a wheels/ directory the script
    # falls back to the previous (online) pip behavior. Verify that
    # path still works -- the offline path is exercised by
    # test_dry_run_pip_install_uses_wheels_when_present.
    assert "falling back to online pip" in dry_output


def test_dry_run_pip_install_uses_wheels_when_present(
    tmp_path: Path,
) -> None:
    """Phase 4a: when ${OPT_DIR}/wheels exists and is non-empty,
    install.sh must call pip with --no-index + --find-links +
    --no-build-isolation so first-boot needs zero network."""
    wheels = tmp_path / "opt" / "openmarquee" / "wheels"
    wheels.mkdir(parents=True)
    (wheels / "fake-1.0-py3-none-any.whl").write_bytes(b"")
    out = _run_dry(tmp_path)
    assert "installing offline" in out
    assert "--no-index" in out
    assert f"--find-links={wheels}" in out
    assert "--no-build-isolation" in out


def test_dry_run_installs_three_systemd_units(dry_output: str) -> None:
    """Step 3 -- backend, ap0, tailscale. Dropping any one breaks a
    specific service: backend = no UI; ap0 = no AP interface (so
    no captive portal); tailscale = no remote management."""
    assert "openmarquee-backend.service" in dry_output
    assert "openmarquee-ap0.service" in dry_output
    assert "openmarquee-tailscale.service" in dry_output


def test_dry_run_stages_hostapd_conf(dry_output: str) -> None:
    """Step 4 -- /etc/hostapd/hostapd.conf. B.4 templates the password
    AFTER this lays the base file."""
    assert "Stage hostapd.conf" in dry_output
    assert "/etc/hostapd/hostapd.conf" in dry_output


def test_dry_run_stages_dnsmasq_conf(dry_output: str) -> None:
    """Step 5 -- dnsmasq config in /etc/dnsmasq.d/ for the
    captive-portal DNS intercept."""
    assert "Stage dnsmasq.conf" in dry_output
    assert "/etc/dnsmasq.d/openmarquee.conf" in dry_output


def test_dry_run_sets_up_iptables_redirect(dry_output: str) -> None:
    """Step 6 -- iptables PREROUTING on ap0:80 -> 10.0.0.1:80. Without
    this the captive-portal redirect never fires — phones see DNS
    pointing at the device but no HTTP responder reachable on the
    URL they typed."""
    assert "iptables -t nat -A PREROUTING -i ap0 -p tcp --dport 80" in dry_output
    assert "10.0.0.1:80" in dry_output
    # Persistence file dump for boot-time restore.
    assert "iptables-save" in dry_output
    assert "/etc/iptables/rules.v4" in dry_output


def test_dry_run_stages_firstboot_service(dry_output: str) -> None:
    """Step 7 -- unconditionally copy openmarquee-firstboot.service into
    /etc/systemd/system. Unconditional so deploy.sh-rsync'd updates to
    the unit body take effect."""
    assert "Stage openmarquee-firstboot service file" in dry_output
    assert "openmarquee-firstboot.service" in dry_output


def test_dry_run_enables_firstboot_on_first_boot(dry_output: str) -> None:
    """First-boot path: `systemctl enable --now` is synchronous for
    Type=oneshot units, so hostapd.conf + welcome.html are templated
    before install.sh's `systemctl restart backend` line runs."""
    assert "systemctl enable --now openmarquee-firstboot.service" in dry_output


def test_dry_run_invokes_firstboot_for_redeploy_templating(dry_output: str) -> None:
    """B.5: deploy.sh rsyncs welcome.html back to its placeholders on
    every redeploy. The systemd unit won't re-fire (.bootstrapped
    guards it), so install.sh runs firstboot.sh DIRECTLY for the
    redeploy re-templating path. firstboot.sh's idempotency reuses
    the existing wifi.json credentials."""
    assert "Re-running firstboot.sh for redeploy templating" in dry_output
    assert "bash" in dry_output
    assert "openmarquee-firstboot.sh" in dry_output


def test_dry_run_reloads_systemd_and_enables_units(dry_output: str) -> None:
    """Step 8 -- daemon-reload picks up new units; enable means they
    actually start on boot."""
    assert "systemctl daemon-reload" in dry_output
    assert "systemctl enable" in dry_output


def test_dry_run_restarts_backend(dry_output: str) -> None:
    """Final step -- restart the backend so a developer-mode redeploy
    actually flips to the new code. On first boot this auto-starts."""
    assert "restart openmarquee-backend.service" in dry_output


def test_dry_run_restart_is_non_blocking(dry_output: str) -> None:
    """On first boot the backend's deps (network-online, ap0, hostapd,
    dnsmasq) may take 10-30s to come up. Without --no-block, the
    restart blocks here for TimeoutStartSec; set -e + transient
    failure -> install.sh aborts before "Done.". Subagent review
    caught this; --no-block + `|| true` is the fix."""
    assert "systemctl --no-block restart openmarquee-backend.service" in dry_output


def test_dry_run_root_prefix_propagates_into_paths(tmp_path: Path) -> None:
    """Verify the --root flag actually redirects destination paths in
    dry-run output -- otherwise the test infra is asserting nothing."""
    out = _run_dry(tmp_path)
    # /var/openmarquee should be prefixed with tmp_path.
    assert f"{tmp_path}/var/openmarquee" in out
    assert f"{tmp_path}/opt/openmarquee" in out
    assert f"{tmp_path}/etc/hostapd" in out


# --- Ordering invariants ---


def test_dry_run_step_ordering(dry_output: str) -> None:
    """The actions must run in dependency order: state dirs before
    venv (pip writes to /var?), venv before systemd units (the
    service runs from the venv), systemd install before
    daemon-reload, daemon-reload before restart."""
    # Find substrings; assert ordering by index.
    state_idx = dry_output.find("Ensure state directories")
    venv_idx = dry_output.find("Ensure Python venv")
    systemd_idx = dry_output.find("Install systemd units")
    reload_idx = dry_output.find("systemctl daemon-reload")
    restart_idx = dry_output.find("restart openmarquee-backend")
    for marker, idx in [
        ("state dirs", state_idx),
        ("venv", venv_idx),
        ("systemd install", systemd_idx),
        ("daemon-reload", reload_idx),
        ("restart", restart_idx),
    ]:
        assert idx != -1, f"missing dry-run marker: {marker}"
    assert state_idx < venv_idx < systemd_idx < reload_idx < restart_idx, (
        f"step ordering broken: {state_idx=} {venv_idx=} {systemd_idx=} "
        f"{reload_idx=} {restart_idx=}"
    )


def test_dry_run_does_not_actually_mutate_filesystem(tmp_path: Path) -> None:
    """The whole point of --dry-run: no real I/O against the root
    prefix. The tmp_path stays empty."""
    _run_dry(tmp_path)
    # Walk tmp_path; expect nothing inside.
    contents = list(tmp_path.rglob("*"))
    assert contents == [], f"dry-run created files: {contents}"


# --- Phase 7 slice 3 (2026-05-13): Rust IPC sidecar binary install ---


def test_dry_run_installs_rust_sidecar_binary_to_usr_local_bin(
    dry_output: str,
) -> None:
    """Slice 3 step 3b: when a staged binary is present (dry-run always
    shows the install regardless), the script copies it to /usr/local/bin/
    and chmod +x's it. The destination path is what the systemd unit
    points OPENMARQUEE_RENDERER_BINARY at."""
    # The header line.
    assert (
        "Install Rust IPC sidecar binary" in dry_output
    ), "missing rust sidecar install step in dry-run"
    # The actions: mkdir parent, cp binary, chmod +x. Each must reference
    # the production path /usr/local/bin/openmarquee-render (the systemd
    # unit's OPENMARQUEE_RENDERER_BINARY default).
    assert (
        "/usr/local/bin/openmarquee-render" in dry_output
    ), "rust binary destination must be /usr/local/bin/openmarquee-render"
    assert (
        "chmod +x" in dry_output and "openmarquee-render" in dry_output
    ), "rust binary must be chmod +x'd"
    # The source path the staged binary is copied FROM. deploy.sh's
    # corresponding rsync step puts it here.
    assert (
        "/opt/openmarquee/bin/openmarquee-render" in dry_output
    ), "rust binary staging source must be /opt/openmarquee/bin/openmarquee-render"


def test_dry_run_rust_sidecar_install_runs_after_systemd_units_before_hostapd(
    dry_output: str,
) -> None:
    """The Rust binary install (step 3b) sits between systemd-unit install
    (step 3) and hostapd staging (step 4). Order matters: systemd unit
    references the binary path (OPENMARQUEE_RENDERER_BINARY), so the
    binary must be on disk before the units get reloaded + restarted."""
    systemd_idx = dry_output.find("Install systemd units")
    rust_idx = dry_output.find("Install Rust IPC sidecar binary")
    hostapd_idx = dry_output.find("Stage hostapd.conf")
    reload_idx = dry_output.find("systemctl daemon-reload")
    for marker, idx in [
        ("systemd", systemd_idx),
        ("rust binary", rust_idx),
        ("hostapd", hostapd_idx),
        ("daemon-reload", reload_idx),
    ]:
        assert idx != -1, f"missing dry-run marker: {marker}"
    assert systemd_idx < rust_idx < hostapd_idx < reload_idx, (
        f"slice-3 ordering broken: {systemd_idx=} {rust_idx=} "
        f"{hostapd_idx=} {reload_idx=}"
    )


def test_dry_run_rust_sidecar_step_uses_root_prefix(tmp_path: Path) -> None:
    """The Rust binary install must respect --root <prefix> so off-device
    tests don't accidentally write to the real /usr/local/bin/."""
    output = _run_dry(tmp_path)
    # The dry-run paths must include the tmp prefix.
    assert (
        f"{tmp_path}/usr/local/bin/openmarquee-render" in output
    ), "rust binary destination should respect --root prefix"
    assert (
        f"{tmp_path}/opt/openmarquee/bin/openmarquee-render" in output
    ), "rust binary source should respect --root prefix"


# --- Task #99 (2026-05-14): AP/NM coexistence fixes ---


def test_dry_run_chmod_plus_x_on_system_sh_helpers(dry_output: str) -> None:
    """Task #99 Fix 2 (2026-05-14): the deployed Pi had system/*.sh files
    at -rw-r--r-- despite Mac source being -rwxr-xr-x. Without +x,
    systemd ExecStart= fails with EACCES; ap0 never comes up; no
    captive-portal AP. Defensive chmod +x in install.sh covers any
    rsync/tar perm-strip in the delivery chain."""
    assert "Ensure +x on system/*.sh helpers" in dry_output
    # Each .sh helper must be explicitly chmod'd. Dropping any one
    # silently reintroduces the regression for that script.
    for helper in [
        "openmarquee-ap0-setup.sh",
        "openmarquee-firstboot.sh",
        "openmarquee-tailscale.sh",
    ]:
        assert (
            f"chmod +x" in dry_output and helper in dry_output
        ), f"system helper {helper} must be chmod +x'd"


def test_dry_run_unmasks_hostapd_and_dnsmasq(dry_output: str) -> None:
    """Task #99 Fix 1 (2026-05-14): Pi OS Lite trixie ships hostapd.service
    masked (prevents accidental AP-on-boot on unrelated images). Without
    `systemctl unmask hostapd`, openmarquee-ap0.service's `Before=
    hostapd.service` ordering pulls in a masked unit that refuses to
    start -- no AP, no captive portal."""
    assert "systemctl unmask hostapd.service dnsmasq.service" in dry_output


def test_dry_run_enables_hostapd_and_dnsmasq(dry_output: str) -> None:
    """Task #99 Fix 1: alongside unmask, both must be `systemctl enable`d
    so they auto-start on boot. enable on a masked unit fails; enable
    on an already-enabled unit is a no-op. The unmask MUST run first."""
    # The enable line bundles all four units; assert each appears.
    enable_block = dry_output.split("systemctl daemon-reload")[1]
    for unit in [
        "openmarquee-backend.service",
        "openmarquee-ap0.service",
        "hostapd.service",
        "dnsmasq.service",
    ]:
        assert unit in enable_block, (
            f"unit {unit} must appear after daemon-reload (in the enable block)"
        )


def test_dry_run_unmask_precedes_enable_for_hostapd(dry_output: str) -> None:
    """Order: unmask -> enable. If enable runs first against a masked
    unit, systemctl returns 1 and -- under `set -e` -- aborts install.sh
    before the rest of the enable line."""
    unmask_idx = dry_output.find("systemctl unmask hostapd.service")
    enable_idx = dry_output.find("systemctl enable openmarquee-backend.service")
    assert unmask_idx != -1, "unmask marker missing"
    assert enable_idx != -1, "enable marker missing"
    assert unmask_idx < enable_idx, (
        f"unmask must precede enable: {unmask_idx=} {enable_idx=}"
    )


def test_ap0_service_orders_before_networkmanager() -> None:
    """Task #99 Fix 3 (2026-05-14): openmarquee-ap0.service must run
    BEFORE NetworkManager.service so `iw dev wlan0 interface add ap0`
    completes before NM begins associating wlan0. This eliminates a
    brcmfmac race where NM mid-association could refuse the __ap vif
    spawn. NM still manages wlan0 normally afterwards -- the
    wifi_station nmcli applier (commit 6ecd1a2) keeps working."""
    unit = (_REPO_ROOT / "system" / "openmarquee-ap0.service").read_text()
    # Single Before= line should list hostapd + NM + NM-wait-online.
    # The ordering keywords are case-sensitive in systemd.
    assert "Before=" in unit, "openmarquee-ap0.service missing Before="
    # Pull the Before= value(s) and assert NM appears.
    before_lines = [
        line for line in unit.splitlines() if line.startswith("Before=")
    ]
    assert before_lines, "no Before= directive found"
    joined = " ".join(before_lines)
    assert "hostapd.service" in joined, "Before= must keep hostapd ordering"
    assert "NetworkManager.service" in joined, (
        "Before= must include NetworkManager.service so ap0 setup precedes NM "
        "association attempt on wlan0"
    )
