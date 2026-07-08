"""Recovery A1: tests for system/openmarquee-boot-gesture.sh — the
"3x power-cycle → Setup Mode" gesture.

The counter + hint paths are env-overridable, so we drive the real
script against temp files and assert the counter/hint state after each
`increment` / `clear`, exactly mirroring the boot sequence:

  * 3 rapid power cycles = increment, increment, increment (no clear
    between them, because each boot was cut short before the ~20s
    stable mark) → the hint appears on the 3rd.
  * A normal boot = increment then clear (the sign stayed up) → the
    counter zeroes, so ordinary reboots never accumulate.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _REPO_ROOT / "system" / "openmarquee-boot-gesture.sh"


def _run(mode: str, count_file: Path, hint_file: Path, *, threshold: int = 3):
    env = {
        "OPENMARQUEE_BOOT_CYCLE_COUNT_FILE": str(count_file),
        "OPENMARQUEE_BOOT_HINT_FILE": str(hint_file),
        "OPENMARQUEE_BOOT_GESTURE_THRESHOLD": str(threshold),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    }
    return subprocess.run(
        ["bash", str(_SCRIPT), mode],
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def paths(tmp_path: Path):
    return tmp_path / "boot-cycle-count", tmp_path / "boot-hint"


def test_script_exists_and_executable():
    assert _SCRIPT.exists()
    assert _SCRIPT.stat().st_mode & 0o111, "ExecStart needs +x"


def test_three_rapid_cycles_fire_the_hint(paths):
    count_file, hint_file = paths
    # Cycle 1 + 2: counter accumulates, NO hint yet.
    for expected in (1, 2):
        r = _run("increment", count_file, hint_file)
        assert r.returncode == 0, r.stderr
        assert count_file.read_text().strip() == str(expected)
        assert not hint_file.exists()
    # Cycle 3: hint fires, counter resets to 0.
    r = _run("increment", count_file, hint_file)
    assert r.returncode == 0, r.stderr
    assert hint_file.read_text().strip() == "setup"
    assert count_file.read_text().strip() == "0"


def test_clear_zeroes_counter_and_removes_hint(paths):
    count_file, hint_file = paths
    _run("increment", count_file, hint_file)  # count → 1
    assert count_file.read_text().strip() == "1"
    r = _run("clear", count_file, hint_file)
    assert r.returncode == 0, r.stderr
    assert count_file.read_text().strip() == "0"
    assert not hint_file.exists()


def test_normal_reboots_never_accumulate(paths):
    count_file, hint_file = paths
    # Each "normal" boot: increment then (20s later) clear. Repeat many
    # times — the counter must never reach the threshold, no hint.
    for _ in range(6):
        _run("increment", count_file, hint_file)
        _run("clear", count_file, hint_file)
        assert count_file.read_text().strip() == "0"
        assert not hint_file.exists()


def test_clear_after_hint_removes_it(paths):
    # The stable-boot clear also drops a hint the backend already
    # consumed, so a later backend restart can't re-read it.
    count_file, hint_file = paths
    for _ in range(3):
        _run("increment", count_file, hint_file)
    assert hint_file.read_text().strip() == "setup"
    _run("clear", count_file, hint_file)
    assert not hint_file.exists()
    assert count_file.read_text().strip() == "0"


def test_corrupt_counter_treated_as_zero(paths):
    count_file, hint_file = paths
    count_file.write_text("not-a-number\n")
    r = _run("increment", count_file, hint_file)
    assert r.returncode == 0, r.stderr
    # Garbage → 0, then +1 = 1.
    assert count_file.read_text().strip() == "1"


def test_threshold_is_configurable(paths):
    count_file, hint_file = paths
    # threshold=2 → hint on the 2nd cycle.
    _run("increment", count_file, hint_file, threshold=2)
    assert not hint_file.exists()
    _run("increment", count_file, hint_file, threshold=2)
    assert hint_file.read_text().strip() == "setup"


def test_unknown_mode_is_usage_error(paths):
    count_file, hint_file = paths
    r = _run("bogus", count_file, hint_file)
    assert r.returncode == 64  # EX_USAGE
