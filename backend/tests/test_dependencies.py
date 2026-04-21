"""Tests for the dev-mode dependency wiring.

The important contract here: MockRenderer's width/height track
SystemSettings so the /dev/preview + /simulator.html pop-out reflect
the operator's configured display dims without a backend restart.
This test exercises the full dependencies.py path (not a hand-rolled
mock) to catch any regression in the wiring.
"""

import os
from pathlib import Path
from unittest import mock

import pytest

from openmarquee.dependencies import (
    _mock_renderer_singleton,
    _settings_storage_singleton,
)
from openmarquee.settings import SettingsStorage, SystemSettings


@pytest.fixture(autouse=True)
def _isolated_singletons(tmp_path: Path, monkeypatch):
    """Redirect the settings file + preview path into tmp and flush
    the singleton caches so each test starts fresh."""
    monkeypatch.setenv("OPENMARQUEE_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv(
        "OPENMARQUEE_DEV_PREVIEW_PATH", str(tmp_path / "preview.png")
    )
    # Clear the WIDTH/HEIGHT env overrides — if they're set (e.g. by
    # a parent shell), our dynamic path short-circuits into static
    # dims and the test misses the point.
    monkeypatch.delenv("OPENMARQUEE_DEV_WIDTH", raising=False)
    monkeypatch.delenv("OPENMARQUEE_DEV_HEIGHT", raising=False)
    _mock_renderer_singleton.cache_clear()
    _settings_storage_singleton.cache_clear()
    yield
    _mock_renderer_singleton.cache_clear()
    _settings_storage_singleton.cache_clear()


class TestMockRendererFollowsSettings:
    def test_width_height_read_from_settings_on_each_access(self, tmp_path: Path):
        settings_path = Path(os.environ["OPENMARQUEE_SETTINGS_PATH"])
        storage = SettingsStorage(settings_path)
        storage.save(SystemSettings(display_width=1920, display_height=1080))

        renderer = _mock_renderer_singleton()
        assert renderer.width == 1920 and renderer.height == 1080

        # Persist a settings change and re-read — no renderer restart.
        storage.save(SystemSettings(display_width=64, display_height=32))
        assert renderer.width == 64 and renderer.height == 32

    def test_portrait_rotation_swaps_dims(self):
        settings_path = Path(os.environ["OPENMARQUEE_SETTINGS_PATH"])
        storage = SettingsStorage(settings_path)
        storage.save(
            SystemSettings(
                display_width=1920,
                display_height=1080,
                display_rotation=90,
            )
        )
        renderer = _mock_renderer_singleton()
        # 90° rotation: landscape-native 1920×1080 becomes 1080×1920.
        assert renderer.width == 1080 and renderer.height == 1920

    def test_env_override_pins_static_dims(self, monkeypatch):
        """OPENMARQUEE_DEV_WIDTH/HEIGHT bypass the settings path —
        tests + CI use this to pin a small canvas for speed."""
        monkeypatch.setenv("OPENMARQUEE_DEV_WIDTH", "4")
        monkeypatch.setenv("OPENMARQUEE_DEV_HEIGHT", "3")
        _mock_renderer_singleton.cache_clear()
        renderer = _mock_renderer_singleton()
        assert renderer.width == 4 and renderer.height == 3

    def test_settings_change_causes_next_render_to_resize_output(
        self, tmp_path: Path
    ):
        """Full integration: render at one size, flip settings, render
        again — the second PNG on disk must be the new size."""
        from PIL import Image

        settings_path = Path(os.environ["OPENMARQUEE_SETTINGS_PATH"])
        storage = SettingsStorage(settings_path)
        storage.save(SystemSettings(display_width=4, display_height=3))

        renderer = _mock_renderer_singleton()
        renderer.render_frame(bytes([10, 20, 30]) * (4 * 3))
        assert Image.open(renderer.output_path).size == (4, 3)

        storage.save(SystemSettings(display_width=2, display_height=2))
        renderer.render_frame(bytes([40, 50, 60]) * (2 * 2))
        assert Image.open(renderer.output_path).size == (2, 2)
