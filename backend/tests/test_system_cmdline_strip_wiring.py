"""cmdline.txt strip wiring regression lock (postmortem mitigation
#5, 2026-05-23).

qarl-direct ship-blocker postmortem 2026-05-23 item #5: the base
Pi OS image carries `cgroup_disable=memory` in /boot/firmware/
cmdline.txt, which suppresses kernel PSI/cgroup memory accounting
+ blocks systemd-OOMD policies. Mitigation #5 adds a generic
`strip_cmdline_token "<token>" "<file>"` helper to the existing
boot-config patch library and wires the FYS-required strip into
BOTH the pi-gen build-time path (`02-run.sh`) AND the install/
redeploy-time path (`scripts/install.sh`).

The helper's behavior — single-line invariant, idempotency,
substring-prefix collision safety (awk field comparison), empty-
file refusal, would-be-empty refusal — is fenced by the shell-
based `test-boot-config.sh` next to the lib. This Python file is
the COMPLEMENT: it fences the cross-file WIRING — "is the helper
actually called from both call sites with the right token?". A
refactor that defined the helper but forgot to wire it into one
of the two call sites would fall right through `test-boot-config.
sh` (which only tests the helper itself) — only a wiring-shape
check catches it.

Same static-parse shape as the wifi-watchdog + web-render mem-
gate locks.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
_LIB = (
    _REPO / "images" / "openmarquee" / "stage-openmarquee" / "02-boot-config" / "boot-config-lib.sh"
)
_PIGEN_RUN = _REPO / "images" / "openmarquee" / "stage-openmarquee" / "02-boot-config" / "02-run.sh"
_INSTALL = _REPO / "scripts" / "install.sh"

_TARGET_TOKEN = "cgroup_disable=memory"


def _strip_shell_comments(text: str) -> str:
    """Strip shell `#` comments (preserving the shebang on line 1)
    so narrative mentions of the locked tokens in docstrings don't
    false-pass the assertions."""
    lines = []
    for i, line in enumerate(text.splitlines()):
        if i == 0 and line.startswith("#!"):
            lines.append(line)
            continue
        lines.append(re.sub(r"#.*$", "", line))
    return "\n".join(lines)


def _read_lib() -> str:
    assert _LIB.is_file(), f"boot-config-lib.sh not found at {_LIB}"
    return _strip_shell_comments(_LIB.read_text(encoding="utf-8"))


def _read_pigen() -> str:
    assert _PIGEN_RUN.is_file(), f"02-run.sh not found at {_PIGEN_RUN}"
    return _strip_shell_comments(_PIGEN_RUN.read_text(encoding="utf-8"))


def _read_install() -> str:
    assert _INSTALL.is_file(), f"install.sh not found at {_INSTALL}"
    return _strip_shell_comments(_INSTALL.read_text(encoding="utf-8"))


def test_strip_cmdline_token_function_defined() -> None:
    """The helper itself must exist in boot-config-lib.sh as a
    POSIX shell function. The shell test (`test-boot-config.sh`)
    pins its behavior; this assertion just fences "the function
    was removed entirely" — which would make the two wiring
    assertions below false-pass via sourcing-error rather than
    a clean signal."""
    source = _read_lib()
    assert re.search(
        r"^\s*strip_cmdline_token\s*\(\s*\)\s*\{",
        source,
        flags=re.MULTILINE,
    ), (
        "strip_cmdline_token function not defined in boot-config-lib.sh "
        "— a refactor removed the helper entirely. The wiring tests "
        "below would still see the call sites as 'present' but the "
        "sourced lib would fail to define them at runtime."
    )


def test_pigen_run_invokes_strip_with_target_token() -> None:
    """The pi-gen build-time runner must invoke `strip_cmdline_token`
    with the exact `cgroup_disable=memory` token. Fences against
    two failure modes: (a) the helper added but never wired into
    02-run.sh, so freshly-built images STILL carry the flag; (b) a
    refactor that renamed the token literal without updating this
    call site — the strip would fire on the wrong token and the
    PSI/OOMD-suppression flag would survive.

    Match shape: `strip_cmdline_token "cgroup_disable=memory" ...`
    — the quoted token argument is required (a bareword could be
    a variable expansion the test can't statically verify)."""
    source = _read_pigen()
    assert re.search(
        rf'\bstrip_cmdline_token\s+"{re.escape(_TARGET_TOKEN)}"',
        source,
    ), (
        f"02-run.sh does not invoke `strip_cmdline_token "
        f'"{_TARGET_TOKEN}"`. Freshly-built pi-gen images will '
        f"still carry the cgroup_disable=memory flag — PSI/cgroup "
        f"memory accounting + systemd-OOMD remain blocked."
    )


def test_install_sh_invokes_strip_with_target_token() -> None:
    """The install/redeploy-time runner must ALSO invoke
    `strip_cmdline_token "cgroup_disable=memory"`. Without this an
    operator running `scripts/install.sh` on an existing Pi
    (the recovery path when something drifts) silently doesn't
    pick up the mitigation."""
    source = _read_install()
    assert re.search(
        rf'\bstrip_cmdline_token\s+"{re.escape(_TARGET_TOKEN)}"',
        source,
    ), (
        f"install.sh does not invoke `strip_cmdline_token "
        f'"{_TARGET_TOKEN}"`. An operator-driven redeploy on a '
        f"running Pi will not pick up the mitigation — only freshly-"
        f"built images would."
    )


def test_target_token_literal_referenced_in_both_call_sites() -> None:
    """The exact token literal `"cgroup_disable=memory"` (with
    double quotes) must appear in BOTH wired call sites. Fences a
    refactor that renamed the token in one file but missed the
    other (e.g. fixing a typo without grep). A divergence here
    would silently leave one of the two paths un-mitigating."""
    pigen = _read_pigen()
    install = _read_install()
    quoted = f'"{_TARGET_TOKEN}"'
    assert quoted in pigen, (
        f"{quoted} literal not in 02-run.sh — call site exists "
        f"but with a different token shape (variable expansion, "
        f"single-quoted, etc.). Re-confirm the exact token reaches "
        f"the helper."
    )
    assert quoted in install, (
        f"{quoted} literal not in install.sh — same diagnostic "
        f"as for 02-run.sh: the call site may exist with the "
        f"wrong token shape."
    )
