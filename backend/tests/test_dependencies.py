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


class TestResolveSelfAddress:
    """The push layer asks _resolve_self_address() for what goes in the
    notify payload. Peers use that to pull content back, so a value
    they can't resolve breaks sync silently."""

    def test_env_override_wins(self, monkeypatch):
        from openmarquee.dependencies import _resolve_self_address

        monkeypatch.setenv("OPENMARQUEE_SELF_ADDRESS", "force.ts.net:1234")
        assert _resolve_self_address() == "force.ts.net:1234"

    def test_settings_hostname_used_when_no_env(self, monkeypatch, tmp_path: Path):
        from openmarquee.dependencies import _resolve_self_address

        monkeypatch.delenv("OPENMARQUEE_SELF_ADDRESS", raising=False)
        storage = SettingsStorage(Path(os.environ["OPENMARQUEE_SETTINGS_PATH"]))
        storage.save(SystemSettings(tailscale_hostname="lobby"))
        _settings_storage_singleton.cache_clear()
        assert _resolve_self_address() == "lobby"

    def test_gethostname_fallback_rejects_bare_short_name(
        self, monkeypatch, tmp_path: Path
    ):
        # Stock Pi returns "raspberrypi"; Tailscale peers can't resolve
        # that. Better to return None so notify_peers skips with a
        # warning than to send pushes with an unreachable sender_address.
        from openmarquee.dependencies import _resolve_self_address

        monkeypatch.delenv("OPENMARQUEE_SELF_ADDRESS", raising=False)
        SettingsStorage(Path(os.environ["OPENMARQUEE_SETTINGS_PATH"])).save(
            SystemSettings()
        )
        _settings_storage_singleton.cache_clear()
        with mock.patch("openmarquee.dependencies.socket.gethostname", return_value="raspberrypi"):
            assert _resolve_self_address() is None
        with mock.patch(
            "openmarquee.dependencies.socket.gethostname",
            return_value="mymachine.local",
        ):
            assert _resolve_self_address() == "mymachine.local"


class TestRealRendererFactory:
    """Factory dispatch matrix for `_real_renderer_singleton`.

    Phase 7 slice 2 (2026-05-13) adds an `OPENMARQUEE_RENDERER=rust-sidecar`
    branch to the existing `mock` / `drm` / `auto` matrix. Tests pin the
    selection logic so:
      - the default (`auto`) continues to resolve to the same class it
        did before this slice (no accidental priority shift toward rust-
        sidecar);
      - `rust-sidecar` is opt-in only.
    """

    def _import_factory(self):
        from openmarquee.dependencies import _real_renderer_singleton

        _real_renderer_singleton.cache_clear()
        return _real_renderer_singleton

    def test_renderer_mock_env_returns_mock(self, monkeypatch):
        """Pre-existing behavior: OPENMARQUEE_RENDERER=mock -> MockRenderer."""
        from openmarquee.rendering.mock import MockRenderer

        monkeypatch.setenv("OPENMARQUEE_RENDERER", "mock")
        factory = self._import_factory()
        renderer = factory()
        assert isinstance(renderer, MockRenderer)

    def test_renderer_default_auto_resolves_unchanged_with_non_hdmi_settings(
        self, monkeypatch
    ):
        """Pre-existing behavior: with the default env (auto) AND non-HDMI
        output_mode, the factory returns MockRenderer. This is the test
        that pins "auto behavior is unchanged by the slice-2 patch"."""
        monkeypatch.delenv("OPENMARQUEE_RENDERER", raising=False)
        # SystemSettings default output_mode is "mock" (set by the
        # _isolated_singletons fixture's tmp_path settings save). Use
        # the dev MockRenderer path.
        from openmarquee.rendering.mock import MockRenderer

        SettingsStorage(Path(os.environ["OPENMARQUEE_SETTINGS_PATH"])).save(
            SystemSettings(output_mode="hub75")
        )
        _settings_storage_singleton.cache_clear()
        factory = self._import_factory()
        renderer = factory()
        assert isinstance(renderer, MockRenderer)

    def test_renderer_rust_sidecar_env_returns_rust_renderer(
        self, monkeypatch, tmp_path
    ):
        """New slice-2 branch: OPENMARQUEE_RENDERER=rust-sidecar returns a
        RustRenderer instance WITHOUT launching the subprocess (construction-
        only at factory time)."""
        from openmarquee.rendering.rust_renderer import RustRenderer

        monkeypatch.setenv("OPENMARQUEE_RENDERER", "rust-sidecar")
        # Provide writable content_root so _resolve_content_root doesn't
        # surprise us.
        monkeypatch.setenv("OPENMARQUEE_CONTENT_ROOT", str(tmp_path))
        SettingsStorage(Path(os.environ["OPENMARQUEE_SETTINGS_PATH"])).save(
            SystemSettings(display_width=1920, display_height=1080)
        )
        _settings_storage_singleton.cache_clear()
        factory = self._import_factory()
        renderer = factory()
        assert isinstance(renderer, RustRenderer)
        # Dims from settings (negotiated dims would update at open() time;
        # the factory doesn't open).
        assert renderer.width == 1920
        assert renderer.height == 1080
        # Subprocess not yet launched.
        assert renderer.is_alive is False

    def test_renderer_rust_sidecar_honors_binary_env_override(
        self, monkeypatch, tmp_path
    ):
        """OPENMARQUEE_RENDERER_BINARY routes through to the proxy's
        binary_path. The proxy doesn't validate path existence at
        construction time (it errors at open()); that's the lifespan's
        job."""
        from openmarquee.rendering.rust_renderer import RustRenderer

        custom_path = tmp_path / "my-custom-render-binary"
        monkeypatch.setenv("OPENMARQUEE_RENDERER", "rust-sidecar")
        monkeypatch.setenv("OPENMARQUEE_RENDERER_BINARY", str(custom_path))
        monkeypatch.setenv("OPENMARQUEE_CONTENT_ROOT", str(tmp_path))
        SettingsStorage(Path(os.environ["OPENMARQUEE_SETTINGS_PATH"])).save(
            SystemSettings()
        )
        _settings_storage_singleton.cache_clear()
        factory = self._import_factory()
        renderer = factory()
        assert isinstance(renderer, RustRenderer)
        # Access the private attr — this test pins the env-var contract.
        assert renderer._binary_path == str(custom_path)

    def test_renderer_rust_sidecar_uses_content_root_from_env(
        self, monkeypatch, tmp_path
    ):
        """The proxy receives the content_root the rest of the backend
        resolved via _resolve_content_root (env override first, then
        ./openmarquee-content)."""
        from openmarquee.rendering.rust_renderer import RustRenderer

        my_cr = tmp_path / "my-content-root"
        my_cr.mkdir()
        monkeypatch.setenv("OPENMARQUEE_RENDERER", "rust-sidecar")
        monkeypatch.setenv("OPENMARQUEE_CONTENT_ROOT", str(my_cr))
        SettingsStorage(Path(os.environ["OPENMARQUEE_SETTINGS_PATH"])).save(
            SystemSettings()
        )
        _settings_storage_singleton.cache_clear()
        factory = self._import_factory()
        renderer = factory()
        assert isinstance(renderer, RustRenderer)
        assert renderer._content_root == str(my_cr)

    def test_renderer_rust_sidecar_unknown_value_falls_through_to_auto(
        self, monkeypatch
    ):
        """Sanity check: a typo in OPENMARQUEE_RENDERER (e.g. "rust-side"
        without the "-car") does NOT silently route to the sidecar.
        Unknown values fall through to the default auto/drm path."""
        from openmarquee.rendering.mock import MockRenderer

        monkeypatch.setenv("OPENMARQUEE_RENDERER", "rust-sidec")  # typo
        SettingsStorage(Path(os.environ["OPENMARQUEE_SETTINGS_PATH"])).save(
            SystemSettings(output_mode="hub75")
        )
        _settings_storage_singleton.cache_clear()
        factory = self._import_factory()
        renderer = factory()
        # Typo'd value isn't "rust-sidecar" so we fall through to the
        # want_drm branch; with output_mode=mock that returns Mock.
        assert isinstance(renderer, MockRenderer)
