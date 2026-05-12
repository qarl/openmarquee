"""Contract tests for scripts/parity/fixtures.json.

The parity harness's runtime errors (missing item.json / golden /
field) only surface when someone runs `scripts/parity_tests.sh`, which
needs Playwright + chromium installed. These tests pin the static
contract -- "every fixture references files that exist on disk +
declares the fields its `kind` requires" -- so a fixture-list edit
that breaks the harness fails CI immediately.

See `qa/cross-renderer-parity-design.md` for the harness design.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
FIXTURES_JSON = REPO / "scripts" / "parity" / "fixtures.json"
FIXTURE_DIR = REPO / "renderer" / "tests" / "fixtures"
GOLDEN_DIR = REPO / "renderer" / "tests" / "golden"

REQUIRED_TOP_KEYS = {"schema_version", "defaults", "fixtures"}
REQUIRED_DEFAULTS = {"ssim_min", "max_delta_max"}
REQUIRED_FIXTURE_KEYS = {"name", "kind", "golden", "purpose"}
SINGLE_KEYS = {"uuid", "tick"}
TRANSITION_FADE_MID_KEYS = {"from_uuid", "to_uuid", "transition_t"}


@pytest.fixture(scope="module")
def spec() -> dict:
    assert FIXTURES_JSON.exists(), f"missing {FIXTURES_JSON}"
    return json.loads(FIXTURES_JSON.read_text())


def test_top_level_shape(spec):
    missing = REQUIRED_TOP_KEYS - set(spec)
    assert not missing, f"fixtures.json missing top-level keys: {missing}"
    assert spec["schema_version"] == 1
    assert isinstance(spec["fixtures"], list)
    assert len(spec["fixtures"]) >= 1


def test_defaults_present(spec):
    missing = REQUIRED_DEFAULTS - set(spec["defaults"])
    assert not missing, f"fixtures.json defaults missing: {missing}"
    assert 0.0 < spec["defaults"]["ssim_min"] <= 1.0
    assert 0 < spec["defaults"]["max_delta_max"] <= 255


def test_each_fixture_has_required_fields(spec):
    for fx in spec["fixtures"]:
        missing = REQUIRED_FIXTURE_KEYS - set(fx)
        assert not missing, (
            f"fixture {fx.get('name')!r} missing required keys: {missing}"
        )


def test_kind_specific_fields(spec):
    for fx in spec["fixtures"]:
        if fx["kind"] == "single":
            missing = SINGLE_KEYS - set(fx)
            assert not missing, (
                f"single fixture {fx['name']!r} missing: {missing}"
            )
        elif fx["kind"] == "transition_fade_mid":
            missing = TRANSITION_FADE_MID_KEYS - set(fx)
            assert not missing, (
                f"transition fixture {fx['name']!r} missing: {missing}"
            )
            t = fx["transition_t"]
            assert 0.0 <= t <= 1.0, (
                f"{fx['name']}: transition_t {t} not in [0,1]"
            )
        else:
            pytest.fail(f"{fx['name']}: unknown kind {fx['kind']!r}")


def test_fixture_uuids_reference_existing_item_json(spec):
    """Every fixture-referenced UUID must have an item.json under
    renderer/tests/fixtures/<uuid>/. Catches orphan references when
    someone deletes a fixture but forgets to update the parity list."""
    for fx in spec["fixtures"]:
        uuids = []
        if fx["kind"] == "single":
            uuids.append(fx["uuid"])
        elif fx["kind"] == "transition_fade_mid":
            uuids.extend([fx["from_uuid"], fx["to_uuid"]])
        for u in uuids:
            item_path = FIXTURE_DIR / u / "item.json"
            assert item_path.exists(), (
                f"{fx['name']!r} references {u} but {item_path} missing"
            )


def test_fixture_golden_references_existing_png(spec):
    """Every fixture's `golden` must resolve to a checked-in PNG.
    This is the parity baseline -- without it the harness has nothing
    to diff against."""
    for fx in spec["fixtures"]:
        png = GOLDEN_DIR / f"{fx['golden']}.png"
        assert png.exists(), (
            f"{fx['name']!r} references golden {fx['golden']!r} "
            f"but {png} missing"
        )


def test_fixture_names_unique(spec):
    names = [fx["name"] for fx in spec["fixtures"]]
    assert len(names) == len(set(names)), (
        f"duplicate fixture names: {names}"
    )
