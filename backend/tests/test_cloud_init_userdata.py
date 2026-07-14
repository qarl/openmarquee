"""Structural invariants on the cloud-init user-data template (Phase B.2).

These tests don't run cloud-init -- that would require booting a VM
or chroot. They guard the user-data file's shape against silent
edits that would break first-boot:

- the operator-supplied SSH key placeholder is still present (a
  substitution-stage bug would inline a real key here; that key would
  then be in git history -- bad);
- the hostname-randomization snippet is in `bootcmd` (not `runcmd`);
- SSH gets enabled (Pi OS Lite trixie gotcha);
- install.sh is invoked;
- password auth is locked off.

Plain-text parsing (no PyYAML dep). Cloud-init YAML is line-oriented
enough that grep-style checks are sufficient and PyYAML is not in
backend/pyproject.toml.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_CLOUD_INIT_DIR = _REPO_ROOT / "images" / "openmarquee" / "cloud-init"
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_STAGE_SSH_FILES = (
    _REPO_ROOT / "images" / "openmarquee" / "stage-openmarquee" / "04-ssh-user" / "files"
)


@pytest.fixture(scope="module")
def user_data() -> str:
    return (_CLOUD_INIT_DIR / "user-data").read_text()


@pytest.fixture(scope="module")
def meta_data() -> str:
    return (_CLOUD_INIT_DIR / "meta-data").read_text()


# --- user-data shape ---


def test_user_data_starts_with_cloud_config_marker(user_data: str) -> None:
    """Cloud-init requires the first line to be `#cloud-config` --
    without it the file is treated as a shell script and ignored as
    user-data."""
    first_line = user_data.splitlines()[0]
    assert first_line == "#cloud-config"


def test_user_data_contains_ssh_authorized_keys_placeholder(user_data: str) -> None:
    """The literal placeholder must survive into the repo. If this
    fails, someone substituted a real key into source -- that key is
    now in git history and should be rotated."""
    assert "{{SSH_AUTHORIZED_KEYS}}" in user_data


def test_user_data_no_real_ssh_keys_committed(user_data: str) -> None:
    """Defense in depth: no SSH key prefix should appear outside the
    placeholder. Catches accidentally-pasted public keys."""
    forbidden_prefixes = ("ssh-rsa ", "ssh-ed25519 ", "ssh-dss ", "ecdsa-sha2-")
    for prefix in forbidden_prefixes:
        assert prefix not in user_data, (
            f"real SSH key prefix `{prefix}` found in source user-data -- "
            f"only the placeholder belongs here"
        )


def test_user_data_pins_openmarquee_user(user_data: str) -> None:
    """The user-data must declare the openmarquee user (for SSH key
    attachment). If renamed here it diverges from pi-gen's
    FIRST_USER_NAME and SSH keys end up on a phantom user."""
    assert "name: openmarquee" in user_data


def test_user_data_preserves_openmarquee_user_sudo(user_data: str) -> None:
    """The openmarquee user MUST keep passwordless sudo. cloud-init's
    user-merge replaces the user record if `sudo:` is declared here,
    so dropping the line would silently demote pi-gen's grant. Caught
    by subagent review on B.2."""
    assert "sudo: ALL=(ALL) NOPASSWD:ALL" in user_data


def test_user_data_ssh_key_placeholder_is_quoted(user_data: str) -> None:
    """Unquoted `{{...}}` is interpreted as a YAML flow-mapping and
    discards the entire user-data file (cloud-init logs the parse
    error to /var/log/cloud-init.log but boots otherwise unconfigured
    -- silent brick). The quoted form `"{{SSH_AUTHORIZED_KEYS}}"` is a
    literal string; cloud-init rejects it on the ssh validator path
    but the rest of the file still runs. Caught by subagent review."""
    # The placeholder MUST appear inside quotes.
    assert '"{{SSH_AUTHORIZED_KEYS}}"' in user_data, (
        "the {{SSH_AUTHORIZED_KEYS}} placeholder must be quoted to keep "
        "the rest of user-data parseable when unsubstituted"
    )


def test_user_data_locks_off_password_auth(user_data: str) -> None:
    """ssh_pwauth=false + disable_root=true together close the brief
    console-only window where pi-gen's literal FIRST_USER_PASS is the
    device password. Skipping this would expose a weak password over
    SSH to anyone on the AP."""
    assert "ssh_pwauth: false" in user_data
    assert "disable_root: true" in user_data


def test_user_data_preserves_hostname_against_set_hostname_module(
    user_data: str,
) -> None:
    """Without preserve_hostname=true, cloud-init's set_hostname module
    runs after bootcmd and overwrites /etc/hostname back to meta-data's
    `local-hostname: openmarquee` -- defeating the randomization.
    Caught by subagent review on B.2."""
    assert "preserve_hostname: true" in user_data


def test_user_data_hostname_randomizer_uses_od_not_xxd(user_data: str) -> None:
    """xxd is NOT in our pi-gen package list -- on trixie it lives
    in a separate `xxd` package (split from vim-common) which we
    don't pull in. od is coreutils, always present. Caught by
    subagent review on B.2 as a silent-brick risk -- xxd-not-found
    yields empty SUFFIX and the hostname becomes `openmarquee-` (with
    trailing dash, permanent because the marker file still gets
    touched on line 57)."""
    assert "SUFFIX=$(od -An -tx1" in user_data, (
        "hostname randomizer must use od (coreutils); xxd is not in 00-packages on trixie"
    )
    assert "| xxd" not in user_data, "xxd pipeline must not be re-introduced"


def test_user_data_disables_package_update_on_boot(user_data: str) -> None:
    """Auto apt-update on first boot adds 30-90s without delivering
    anything: the image was built against the same trixie snapshot
    that pi-gen used. Operator runs apt-update later via SSH."""
    assert "package_update: false" in user_data
    assert "package_upgrade: false" in user_data


def test_user_data_hostname_randomizer_uses_bootcmd(user_data: str) -> None:
    """The hostname-set snippet must be in bootcmd, NOT runcmd:
    bootcmd runs before networking comes up, so mDNS advertises the
    randomized name from the first second of life. runcmd would
    advertise `openmarquee` (the seed) briefly then flip -- a 5-10s
    window where the operator's phone might cache the wrong name."""
    boot_idx = user_data.find("bootcmd:")
    run_idx = user_data.find("runcmd:")
    rand_idx = user_data.find("openmarquee-${SUFFIX}")
    assert boot_idx != -1
    assert run_idx != -1
    assert rand_idx != -1
    # bootcmd appears before runcmd; the randomizer falls between them.
    assert boot_idx < rand_idx < run_idx


def test_user_data_runcmd_enables_ssh(user_data: str) -> None:
    """Pi OS Lite trixie does NOT enable SSH automatically even with
    cloud-init present (reference_dev_pi memo). Must be explicitly
    enabled + started. Order matters -- enable then start."""
    enable_idx = user_data.find("systemctl enable ssh")
    start_idx = user_data.find("systemctl start ssh")
    assert enable_idx != -1
    assert start_idx != -1
    assert enable_idx < start_idx


def test_user_data_runcmd_invokes_install_sh(user_data: str) -> None:
    """The handoff to B.3's install.sh. If this line is missing the
    device boots into a half-configured state -- backend not running,
    AP not up, captive-portal absent. The package was laid down by
    B.1 but nothing wired it up."""
    assert "/opt/openmarquee/scripts/install.sh" in user_data


def test_user_data_hostname_randomizer_is_idempotent(user_data: str) -> None:
    """The /var/lib/openmarquee/.hostname-randomized marker guards
    against the bootcmd re-running on subsequent boots and shuffling
    the hostname. cloud-init normally only runs bootcmd once, but
    defense in depth -- a user reset via cloud-init clean would
    otherwise re-randomize and break Tailscale + flock peer state
    pinned to the original hostname."""
    assert "/var/lib/openmarquee/.hostname-randomized" in user_data


# --- meta-data shape ---


def test_meta_data_has_instance_id(meta_data: str) -> None:
    """cloud-init refuses to read user-data if meta-data lacks an
    instance-id. Any non-empty string works; we use a fixed value
    so cloud-init doesn't re-run bootcmd on subsequent boots."""
    assert "instance-id:" in meta_data
    # Extract the value, strip whitespace, ensure non-empty.
    for line in meta_data.splitlines():
        if line.startswith("instance-id:"):
            value = line.split(":", 1)[1].strip()
            assert value, "instance-id must have a non-empty value"
            return
    pytest.fail("instance-id line not found")


def test_meta_data_seed_hostname_matches_pi_gen(meta_data: str) -> None:
    """Seed hostname (pre-bootcmd-randomization) is `openmarquee` --
    same as pi-gen's HOSTNAME pin. Keeps the AP SSID + Tailscale name
    + log hostname in sync principle (feedback_sign_name_sync memo)
    even during the bootcmd window before randomization completes."""
    assert "local-hostname: openmarquee" in meta_data


# --- staged user-data (written by stage_sd_card.sh; the variant that
#     actually ships on a code-staged card — the base image is a
#     "no app code" base that fail-loud-refuses a standalone flash) ---


@pytest.fixture(scope="module")
def staged_user_data() -> str:
    """Extract the user-data heredoc stage_sd_card.sh writes onto the card."""
    text = (_SCRIPTS_DIR / "stage_sd_card.sh").read_text()
    marker = "cat > \"$BOOTFS/user-data\" <<'EOF'\n"
    start = text.index(marker) + len(marker)
    end = text.index("\nEOF\n", start)
    return text[start:end]


def test_staged_user_data_openmarquee_is_login_capable(staged_user_data: str) -> None:
    """The shipped card's openmarquee must be a LOGIN user (it does double
    duty as the SSH identity), NOT the old system/nologin service account.
    Regression guard for the 2026-07-13 SSH-user arc: a nologin openmarquee
    with no key made SSH-in impossible on shipped cards."""
    assert "name: openmarquee" in staged_user_data
    assert "shell: /bin/bash" in staged_user_data
    assert "/usr/sbin/nologin" not in staged_user_data
    assert "system: true" not in staged_user_data


def test_staged_user_data_carries_quoted_ssh_key_placeholder(staged_user_data: str) -> None:
    """The shipped user-data must carry the (quoted) SSH key placeholder,
    which stage_sd_card.sh's --ssh-key handling substitutes. Without it the
    card boots but is SSH-unreachable (key-only auth)."""
    assert '"{{SSH_AUTHORIZED_KEYS}}"' in staged_user_data


def test_staged_user_data_full_sudo_and_locked_ssh(staged_user_data: str) -> None:
    assert "sudo: ALL=(ALL) NOPASSWD:ALL" in staged_user_data
    assert "ssh_pwauth: false" in staged_user_data
    assert "disable_root: true" in staged_user_data


def test_staged_user_data_no_real_ssh_keys_committed(staged_user_data: str) -> None:
    """No real pubkey may be committed into the staged heredoc — only the
    placeholder (a committed key would be in git history)."""
    for prefix in ("ssh-rsa ", "ssh-ed25519 ", "ssh-dss ", "ecdsa-sha2-"):
        assert prefix not in staged_user_data, (
            f"real SSH key prefix `{prefix}` in stage_sd_card.sh — only the placeholder belongs"
        )


# --- image-baked SSH/sudo hardening (stage-openmarquee/04-ssh-user) ---


def test_baked_sshd_config_hardening() -> None:
    """The image-baked sshd drop-in enforces key-only, no-root login
    independent of cloud-init."""
    conf = (_STAGE_SSH_FILES / "etc" / "ssh" / "sshd_config.d" / "openmarquee.conf").read_text()
    assert "PasswordAuthentication no" in conf
    assert "PubkeyAuthentication yes" in conf
    assert "PermitRootLogin no" in conf


def test_baked_sudoers_is_full_nopasswd() -> None:
    sudoers = (_STAGE_SSH_FILES / "etc" / "sudoers.d" / "openmarquee").read_text()
    assert "openmarquee ALL=(ALL) NOPASSWD:ALL" in sudoers


def test_baked_sudoers_matches_canonical_no_drift() -> None:
    """The image-baked sudoers (stage substage) and the install.sh-applied
    canonical (system/openmarquee-sudoers) must stay byte-identical — both
    write /etc/sudoers.d/openmarquee, so drift would mean two devices with
    different grants depending on the delivery path (image bake vs redeploy)."""
    baked = (_STAGE_SSH_FILES / "etc" / "sudoers.d" / "openmarquee").read_bytes()
    canonical = (_REPO_ROOT / "system" / "openmarquee-sudoers").read_bytes()
    assert baked == canonical, (
        "images/.../04-ssh-user/files/etc/sudoers.d/openmarquee has drifted from "
        "system/openmarquee-sudoers — copy the canonical file over it"
    )
