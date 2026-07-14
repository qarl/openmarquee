#!/usr/bin/env python3
"""Bless 37 FYS parity goldens (Step 2b, Path B).

Runs --capture-slide / --capture-sb-mid against the post-gamma-flip
rust binary on the FYS Pi for each parity_fys_* fixture in
scripts/parity/fixtures.json, then scp's the resulting PNGs into
renderer/tests/golden/.

Path B (qarl-via-QA call 2026-05-17): bless via a one-shot script
instead of expanding scripts/render_tests.sh's FIXTURES array.
render_tests.sh stays the small curated rust-regression set; this
script is for parity baselines.

Why DRM master matters: --capture-slide opens an EGL window surface
which needs GBM which needs DRM master. The running openmarquee-
backend's sidecar holds it, so we stop the unit before captures and
restart it after (try/finally so it always recovers).

Assumes:
- /opt/openmarquee/bin/openmarquee-render is the post-flip binary
  (deployed 2026-05-17 by the gamma-default-flip commit 01d9aeb)
- the BLESS_FYS_PI_HOST target (default qarl@192.168.1.67) has sudo NOPASSWD
- renderer/tests/fixtures/<UUID>/item.json exists for every UUID
  referenced by a parity_fys_ fixture (the Step 2a snapshots)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
FIXTURES_JSON = REPO / "scripts" / "parity" / "fixtures.json"
GOLDEN_DIR = REPO / "renderer" / "tests" / "golden"
FIXTURE_DIR = REPO / "renderer" / "tests" / "fixtures"

# SSH target for the FYS parity Pi. Overridable via env so the user@host
# isn't baked to a personal identity in the code — set BLESS_FYS_PI_HOST
# (e.g. openmarquee@192.168.1.67) once that device is re-provisioned to the
# openmarquee SSH user. Default preserved so current access is uninterrupted.
PI_HOST = os.environ.get("BLESS_FYS_PI_HOST", "qarl@192.168.1.67")
PI_BIN = "/opt/openmarquee/bin/openmarquee-render"
PI_CONTENT_ROOT = "/tmp/bless-content"
PI_GOLDEN_DIR = "/tmp/bless-goldens"
FORCE_MODE = "1920x1080@60"


def ssh(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", PI_HOST, *cmd],
        check=check, capture_output=True, text=True,
    )


def main() -> int:
    spec = json.loads(FIXTURES_JSON.read_text())
    fys = [f for f in spec["fixtures"] if f["name"].startswith("parity_fys_")]
    print(f"==> {len(fys)} parity_fys_ fixtures in fixtures.json")
    if not fys:
        print("FATAL: no parity_fys_ fixtures found")
        return 1

    # Collect every UUID referenced (single uuid + from/to for transitions).
    referenced_uuids: set[str] = set()
    for fx in fys:
        if fx["kind"] == "single":
            referenced_uuids.add(fx["uuid"])
        elif fx["kind"] == "transition_mid":
            referenced_uuids.add(fx["from_uuid"])
            referenced_uuids.add(fx["to_uuid"])

    # Verify fixture content exists locally before pushing.
    for uuid in sorted(referenced_uuids):
        item = FIXTURE_DIR / uuid / "item.json"
        if not item.exists():
            print(f"FATAL: missing fixture content: {item}")
            return 1

    print(f"==> push {len(referenced_uuids)} fixture dirs to {PI_HOST}:{PI_CONTENT_ROOT}/")
    ssh(["rm", "-rf", PI_CONTENT_ROOT, PI_GOLDEN_DIR], check=False)
    ssh(["mkdir", "-p", PI_CONTENT_ROOT, PI_GOLDEN_DIR])
    # One rsync trip is faster than 19 scp -r calls. Push only the
    # referenced UUID dirs by selecting them via include/exclude.
    rsync_cmd = ["rsync", "-az", "--delete"]
    for uuid in sorted(referenced_uuids):
        rsync_cmd.extend(["--include", f"{uuid}/", "--include", f"{uuid}/**"])
    rsync_cmd.extend(["--exclude", "*", str(FIXTURE_DIR) + "/", f"{PI_HOST}:{PI_CONTENT_ROOT}/"])
    subprocess.run(rsync_cmd, check=True)

    print(f"==> stop openmarquee-backend (frees DRM master); sign will be dark for ~{len(fys) * 3}s")
    ssh(["sudo", "systemctl", "stop", "openmarquee-backend"])

    captured: list[tuple[dict, str]] = []
    failures: list[tuple[dict, str]] = []

    try:
        for i, fx in enumerate(fys, 1):
            name = fx["golden"]
            pi_png = f"{PI_GOLDEN_DIR}/{name}.png"

            if fx["kind"] == "single":
                tick = fx.get("tick", 0.0)
                cmd = [
                    PI_BIN, "--output", "hdmi",
                    "--capture-slide", fx["uuid"],
                    "--capture-slide-at-tick", str(tick),
                    "--content-root", PI_CONTENT_ROOT,
                    "--capture-path", pi_png,
                    "--force-mode", FORCE_MODE,
                ]
            elif fx["kind"] == "transition_mid":
                # tick=0 (the --capture-sb-mid default) leaves motion at
                # phase 0 within the from-slide. Per qarl's "motion-
                # through-transitions required" rule, this surfaces any
                # motion-frozen-at-transition divergence in the parity
                # table; we don't pre-fix here.
                cmd = [
                    PI_BIN, "--output", "hdmi",
                    "--capture-sb-mid",
                    "--fade-from", fx["from_uuid"],
                    "--fade-to", fx["to_uuid"],
                    "--transition", fx["transition"],
                    "--capture-sb-t", str(fx.get("transition_t", 0.5)),
                    "--content-root", PI_CONTENT_ROOT,
                    "--capture-path", pi_png,
                    "--force-mode", FORCE_MODE,
                ]
            else:
                print(f"  [{i:02d}/{len(fys)}] {name}  SKIP (kind={fx['kind']!r})")
                continue

            print(f"  [{i:02d}/{len(fys)}] {name} ({fx['kind']})")
            result = ssh(cmd, check=False)
            if result.returncode != 0:
                tail = (result.stderr or result.stdout)[-400:]
                print(f"      FAIL exit={result.returncode}: {tail}")
                failures.append((fx, tail))
                continue
            captured.append((fx, pi_png))

        if not captured:
            print("FATAL: no successful captures")
            return 2

        print(f"==> rsync {len(captured)} PNGs back to {GOLDEN_DIR}/")
        subprocess.run(
            ["rsync", "-az", f"{PI_HOST}:{PI_GOLDEN_DIR}/", str(GOLDEN_DIR) + "/"],
            check=True,
        )
    finally:
        print("==> restart openmarquee-backend")
        ssh(["sudo", "systemctl", "start", "openmarquee-backend"], check=False)
        print(f"==> cleanup Pi /tmp dirs")
        ssh(["rm", "-rf", PI_CONTENT_ROOT, PI_GOLDEN_DIR], check=False)

    print(f"==> DONE: {len(captured)} blessed, {len(failures)} failed")
    if failures:
        print("FAILURES:")
        for fx, msg in failures:
            print(f"  {fx['name']:50s}  {msg.strip().splitlines()[-1]}")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
