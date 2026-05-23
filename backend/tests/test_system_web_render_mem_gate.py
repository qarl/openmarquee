"""Web-render memory-pressure gate regression lock (postmortem
mitigation #3, 2026-05-23).

qarl-direct ship-blocker postmortem 2026-05-23: the Pi Zero 2 W's
~426 MB RAM gets blown by chromium-headless-shell spawned every 5
min for Newsmoji slides; sustained memory pressure manifests as
brcmfmac SDIO CMD53 errors → chronic WiFi instability. Mitigation
#3 gates the Chromium spawn on /proc/meminfo: skip the render
when MemAvailable is below floor (default 80 MB) OR SwapUsed is
above ceiling (default 30 MB).

Six load-bearing invariants the unit tests verify at runtime, plus
two that ONLY a source-level check can catch (refactor that moves
the gate inside the lock would still pass mocked unit tests):

1. The gate fires BEFORE the `async with _render_lock` acquire.
   A refactor that moved it inside the lock would still pass every
   mocked unit test (no lock contention there), but would defeat
   the cheap fast-path: a skip would now wait on any in-flight
   render. Source-order check is the only way to fence this.

2. Both env-var names are referenced. A refactor that drops one
   silently removes the ops override.

3. /proc/meminfo literal path is referenced. A refactor that
   swaps in psutil would be a new-dep decision needing QA sign-
   off, NOT a quiet substitution.

4. The skip path returns False (consistent with the existing
   failure-return contract — slide keeps its last-good asset.png).

5. Default thresholds match the postmortem (80 MB floor, 30 MB
   ceiling). A refactor that retunes either default without
   re-running the postmortem analysis is a regression.

Static parse — same shape as wifi-watchdog / inline-preview font-
load / web-slide preview locks.
"""

from __future__ import annotations

import re
from pathlib import Path

_WEB_SCREENSHOT = (
    Path(__file__).resolve().parent.parent
    / "openmarquee"
    / "web_screenshot.py"
)


def _read_source() -> str:
    """Read web_screenshot.py and strip Python `#` line comments
    and triple-quoted docstrings so narrative mentions of locked
    symbols in prose don't false-pass the assertions. Naive strip
    that matches the way the test target is actually written —
    docstrings are triple-quoted, comments are `#`-to-EOL."""
    assert _WEB_SCREENSHOT.is_file(), (
        f"web_screenshot.py not found at {_WEB_SCREENSHOT}; "
        f"relocation? Update the test path."
    )
    text = _WEB_SCREENSHOT.read_text(encoding="utf-8")
    # Strip triple-double-quoted docstrings (greedy non-greedy across
    # lines). Triple-single is uncommon in this codebase but covered
    # for safety.
    text = re.sub(r'""".*?"""', "", text, flags=re.DOTALL)
    text = re.sub(r"'''.*?'''", "", text, flags=re.DOTALL)
    # Strip `#`-to-EOL line comments. (No `#` lives inside a string
    # literal in this file.)
    text = re.sub(r"#[^\n]*", "", text)
    return text


def test_gate_fires_before_render_lock_acquire() -> None:
    """The memory-pressure gate MUST be evaluated BEFORE the
    `async with _render_lock` acquire — a skip needs to be a cheap
    fast-path that doesn't wait on an in-flight render. A refactor
    that moved the gate inside the lock would still pass every
    mocked unit test (no real lock contention there) but would
    defeat the fast-path mitigation."""
    source = _read_source()
    # Find the gate's _read_meminfo call site and the lock acquire.
    # The gate calls _read_meminfo() (without leading underscore-
    # prefix-stripping noise); the lock line is the literal
    # `async with _render_lock`.
    gate_match = re.search(r"\b_read_meminfo\s*\(\s*\)", source)
    lock_match = re.search(r"async\s+with\s+_render_lock\s*:", source)
    assert gate_match, (
        "_read_meminfo() call not found in web_screenshot.py — "
        "memory-pressure gate appears removed entirely."
    )
    assert lock_match, (
        "`async with _render_lock:` not found in web_screenshot.py "
        "— the render-serialization lock appears removed."
    )
    assert gate_match.start() < lock_match.start(), (
        f"the _read_meminfo() gate (at offset {gate_match.start()}) "
        f"must fire BEFORE the `async with _render_lock:` acquire "
        f"(at offset {lock_match.start()}). A refactor moved the "
        f"gate inside the lock — a skipped refresh would now wait "
        f"on any in-flight render, defeating the cheap fast-path."
    )


def test_both_env_var_names_referenced() -> None:
    """Both override env-var names must be referenced in source. A
    refactor that drops one (or renames either) silently removes
    the ops escape hatch for that threshold."""
    source = _read_source()
    for name in (
        "OPENMARQUEE_WEB_RENDER_MEM_FLOOR_MB",
        "OPENMARQUEE_WEB_RENDER_SWAP_CEILING_MB",
    ):
        assert name in source, (
            f"env var {name!r} not referenced in web_screenshot.py "
            f"— ops override surface for that threshold is gone."
        )


def test_proc_meminfo_path_referenced() -> None:
    """/proc/meminfo literal path must be referenced. A refactor
    that swaps in psutil (or any other introspection library) is a
    new-dep decision needing QA sign-off — not a silent swap. The
    test exists to surface that decision."""
    source = _read_source()
    assert "/proc/meminfo" in source, (
        "/proc/meminfo path not referenced — has psutil or another "
        "memory-introspection library been substituted? That is a "
        "new-dep decision needing QA sign-off; do not silently "
        "swap it in."
    )


def test_skip_path_returns_false() -> None:
    """The skip path must `return False`. False matches the existing
    failure-return contract (`fetch_web_screenshot` returns False on
    every failure path, keeping the slide's last-good asset.png).
    A skip that returned True would deceive the caller into thinking
    the asset was refreshed; a skip that raised would crash the
    fire-and-forget producer."""
    source = _read_source()
    # Find the gate body — the if-block that follows the threshold
    # comparison. Match from the `if mem_available_mb < floor` line
    # through the next `return` statement.
    gate_body = re.search(
        r"if\s+mem_available_mb\s*<\s*floor\s+or\s+swap_used_mb\s*>\s*ceiling\s*:(.*?)return\s+(\w+)",
        source,
        flags=re.DOTALL,
    )
    assert gate_body, (
        "could not locate the memory-pressure skip block "
        "(`if mem_available_mb < floor or swap_used_mb > ceiling: "
        "... return ...`). Refactor may have restructured the "
        "gate; re-confirm the skip path returns False."
    )
    return_value = gate_body.group(2)
    assert return_value == "False", (
        f"the skip path returns {return_value!r} — must be `False` "
        f"to match the existing failure-return contract (slide "
        f"keeps last-good asset.png). Returning True would deceive "
        f"the caller; raising would crash the producer."
    )


def test_default_thresholds_match_postmortem() -> None:
    """Defaults must match the 2026-05-23 postmortem mitigation #3
    values: 80 MB MemAvailable floor, 30 MB SwapUsed ceiling. A
    refactor that retunes either without re-running the postmortem
    analysis (and updating this test deliberately) is a regression."""
    source = _read_source()
    assert re.search(
        r"^\s*_MEM_FLOOR_MB_DEFAULT\s*=\s*80\s*$",
        source,
        flags=re.MULTILINE,
    ), (
        "_MEM_FLOOR_MB_DEFAULT must be 80 (the postmortem value) — "
        "a retune of this default without re-analysis is a "
        "regression."
    )
    assert re.search(
        r"^\s*_SWAP_CEILING_MB_DEFAULT\s*=\s*30\s*$",
        source,
        flags=re.MULTILINE,
    ), (
        "_SWAP_CEILING_MB_DEFAULT must be 30 (the postmortem "
        "value) — a retune of this default without re-analysis is "
        "a regression."
    )
