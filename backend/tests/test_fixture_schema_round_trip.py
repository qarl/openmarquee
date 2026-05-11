"""Batch 18.4 / sweep #9 N1: Python-side schema round-trip for renderer
golden-master fixtures.

17.4 added a Rust-side cargo test that asserts the p2g_overlay_route
fixture deserializes via the renderer's TextSlide mirror. The Python
side has the same exposure: if the spec moves v3 -> v4 with a new
field, the renderer's fixtures load on the lenient Rust deserializer
but the production Python backend's `TextSlide.model_validate` may
reject or silently drop the field. Either failure shape is bad --
either the fixture stops parsing on a real device, or the golden PNG
no longer reflects what a production save round-trip would produce.

This test walks every `renderer/tests/fixtures/<UUID>/item.json`
and asserts the item parses cleanly via the corresponding Pydantic
model (TextSlide for type=text_slide; ImageSlide for type=image;
VideoSlide for type=video). When a future spec bump lands without
adding the field to the model, this test fails loud.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openmarquee.content import ImageSlide, TextSlide, VideoSlide

# Repo-relative path to the renderer fixtures dir.
# backend/tests/<this> -> backend/ -> code/ -> renderer/tests/fixtures
_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "renderer" / "tests" / "fixtures"


def _fixture_item_paths() -> list[Path]:
    """All renderer fixture item.json files. Each lives at
    `renderer/tests/fixtures/<UUID>/item.json`."""
    if not _FIXTURES_DIR.is_dir():
        return []
    return sorted(_FIXTURES_DIR.glob("*/item.json"))


@pytest.mark.parametrize(
    "item_path",
    _fixture_item_paths(),
    ids=lambda p: p.parent.name,
)
def test_renderer_fixture_deserializes_via_pydantic(item_path: Path):
    """For each renderer fixture, the embedded item must round-trip
    through the production Python Pydantic model that corresponds to
    its `type`. Catches the silent-drop class of bug from the other
    direction (renderer is fine; backend wouldn't load it after a
    spec change)."""
    envelope = json.loads(item_path.read_text())
    assert "schema_version" in envelope, f"envelope missing schema_version: {item_path}"
    item = envelope["item"]
    kind = item.get("type")
    if kind == "text_slide":
        TextSlide.model_validate(item)
    elif kind == "image":
        ImageSlide.model_validate(item)
    elif kind == "video":
        VideoSlide.model_validate(item)
    else:
        pytest.fail(f"unknown item.type {kind!r} in {item_path}")


def test_at_least_one_fixture_present():
    """Sanity guard: if the parametrize sweep returns an empty list
    (e.g., the fixtures dir moved or got renamed), the per-fixture
    tests above won't run AT ALL, producing a green CI with zero
    actual coverage. This test fails loud in that case."""
    paths = _fixture_item_paths()
    assert paths, f"no renderer fixtures found under {_FIXTURES_DIR}"
