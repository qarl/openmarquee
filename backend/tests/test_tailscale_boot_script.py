"""Drives system/openmarquee-tailscale.sh — the boot unit that decides
whether a sign stays on the tailnet.

2026-07-16 (F2): this script used to `tailscale logout` whenever
settings.json said `tailscale_enabled` wasn't 1. That is a very sharp knife
pointed at the ONE lane we use to reach a sign nobody can physically touch,
and it fired on the say-so of a field that was wrong in three ways:

  * nothing wrote `tailscale_enabled` at all until /api/system/tailscale/up
    started persisting it, so an operator who clicked Enable (and never
    ticked the Settings box) was logged out on the first reboot;
  * the Settings checkbox is disabled until the station radio is on, and
    that radio was itself wrong on an NM-provisioned sign — so there was no
    path to True through the UI;
  * a stale browser tab autosaving an unticked box flipped it back to False
    under a working node.

These tests EXECUTE the real script against a stubbed `tailscale` binary
rather than grepping it. A source-substring assertion could not witness the
hazard: it would pass on a script that spelled the call differently, and it
proves nothing about what the script actually does with a given
settings.json. Live in the backend suite (not scripts/tests/, which the
pre-push hook documents as run MANUALLY and which CI never invokes) so this
is an actual gate.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "system" / "openmarquee-tailscale.sh"


@pytest.fixture
def run_boot_script(tmp_path):
    """Run the real script with a fake `tailscale` on PATH; return the
    argv lines the stub recorded."""
    calls = tmp_path / "calls.log"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "tailscale"
    stub.write_text(f'#!/usr/bin/env bash\necho "$@" >> "{calls}"\nexit 0\n')
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def _run(settings: dict) -> tuple[subprocess.CompletedProcess, list[str]]:
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps(settings))
        env = dict(os.environ)
        env["OPENMARQUEE_SETTINGS_PATH"] = str(settings_path)
        env["PATH"] = f"{bindir}:{env['PATH']}"
        proc = subprocess.run(
            ["bash", str(SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        recorded = calls.read_text().splitlines() if calls.exists() else []
        return proc, recorded

    return _run


def test_disabled_never_logs_the_node_off_the_tailnet(run_boot_script):
    """THE FIX. `tailscale_enabled: false` must not sever the support lane.
    Fails on the pre-fix script, which ran `tailscale logout` here."""
    proc, calls = run_boot_script({"tailscale_enabled": False})

    assert proc.returncode == 0, proc.stderr
    # Pin that we actually REACHED the disabled branch: without this the
    # test would pass just as well on a script that bailed at the
    # settings.json-not-found guard and never got here.
    assert "disabled in settings" in proc.stdout, proc.stdout
    assert not any("logout" in c for c in calls), (
        f"the boot script logged the sign off the tailnet on the say-so of a "
        f"settings field; recorded calls: {calls}"
    )


def test_disabled_does_not_bring_the_node_up_either(run_boot_script):
    """CONTROL: removing the logout must not turn the flag into a no-op that
    silently ENABLES tailscale — the script should still do nothing. Without
    this, a test asserting 'no logout' would pass on a script that ignored
    the field entirely."""
    proc, calls = run_boot_script({"tailscale_enabled": False})

    assert proc.returncode == 0
    assert calls == [], f"a disabled sign must be left alone, not acted on: {calls}"


def test_enabled_still_brings_the_node_up(run_boot_script):
    """CONTROL: the script must still do its actual job."""
    proc, calls = run_boot_script({"tailscale_enabled": True, "tailscale_hostname": "jasonssign1"})

    assert proc.returncode == 0, proc.stderr
    assert any(c.startswith("up") for c in calls), f"expected a `tailscale up`: {calls}"
    assert not any("logout" in c for c in calls)
