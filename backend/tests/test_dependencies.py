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

    def test_renderer_default_auto_with_legacy_led_mode_coerces_and_routes_to_rust(
        self, monkeypatch, tmp_path
    ):
        """DELETE-PIL: legacy on-disk LED output_mode values coerce to
        "hdmi" at settings load, so the default auto path lands on the
        Rust IPC sidecar -- not Mock. Pins the migration + dispatch
        contract together."""
        monkeypatch.delenv("OPENMARQUEE_RENDERER", raising=False)
        monkeypatch.setenv("OPENMARQUEE_CONTENT_ROOT", str(tmp_path))
        from openmarquee.dependencies import AutoFallbackRenderer

        # Save a legacy hub75 settings file; the migration coerces it
        # to "hdmi" on load.
        SettingsStorage(Path(os.environ["OPENMARQUEE_SETTINGS_PATH"])).save(
            SystemSettings(output_mode="hub75")
        )
        _settings_storage_singleton.cache_clear()
        factory = self._import_factory()
        renderer = factory()
        assert isinstance(renderer, AutoFallbackRenderer)

    def test_renderer_rust_sidecar_env_returns_auto_fallback_wrapper(
        self, monkeypatch, tmp_path
    ):
        """slice-2 branch + slice-2-followup (2026-05-14):
        OPENMARQUEE_RENDERER=rust-sidecar returns an AutoFallbackRenderer
        wrapping a RustRenderer instance, NOT the bare proxy. This pins
        the wrapper contract so playback always gets the fallback
        capability."""
        from openmarquee.dependencies import AutoFallbackRenderer
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
        assert isinstance(renderer, AutoFallbackRenderer)
        assert isinstance(renderer._primary, RustRenderer)
        # Dims forwarded through the wrapper from the proxy.
        assert renderer.width == 1920
        assert renderer.height == 1080
        # Subprocess not yet launched.
        assert renderer._primary.is_alive is False
        # Wrapper is not in fallback mode yet (no failure has happened).
        assert renderer.is_in_fallback is False

    def test_renderer_rust_sidecar_honors_binary_env_override(
        self, monkeypatch, tmp_path
    ):
        """OPENMARQUEE_RENDERER_BINARY routes through to the proxy's
        binary_path. The proxy doesn't validate path existence at
        construction time (it errors at open()); that's the lifespan's
        job."""
        from openmarquee.dependencies import AutoFallbackRenderer

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
        assert isinstance(renderer, AutoFallbackRenderer)
        # Drill through to the wrapped proxy to pin the env-var contract.
        assert renderer._primary._binary_path == str(custom_path)

    def test_renderer_rust_sidecar_uses_content_root_from_env(
        self, monkeypatch, tmp_path
    ):
        """The proxy receives the content_root the rest of the backend
        resolved via _resolve_content_root (env override first, then
        ./openmarquee-content)."""
        from openmarquee.dependencies import AutoFallbackRenderer

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
        assert isinstance(renderer, AutoFallbackRenderer)
        assert renderer._primary._content_root == str(my_cr)

    def test_renderer_rust_sidecar_unknown_value_falls_through_to_auto(
        self, monkeypatch, tmp_path
    ):
        """Sanity check: a typo in OPENMARQUEE_RENDERER (e.g. "rust-side"
        without the "-car") falls through to the settings-based auto
        path. DELETE-PIL: settings.output_mode is always "hdmi" post-
        migration, so the auto path routes to the Rust IPC sidecar
        (production default)."""
        from openmarquee.dependencies import AutoFallbackRenderer

        monkeypatch.setenv("OPENMARQUEE_RENDERER", "rust-sidec")  # typo
        monkeypatch.setenv("OPENMARQUEE_CONTENT_ROOT", str(tmp_path))
        SettingsStorage(Path(os.environ["OPENMARQUEE_SETTINGS_PATH"])).save(
            SystemSettings(output_mode="hdmi")
        )
        _settings_storage_singleton.cache_clear()
        factory = self._import_factory()
        renderer = factory()
        assert isinstance(renderer, AutoFallbackRenderer)


# ============================================================
# AutoFallbackRenderer (2026-05-14 dispatch — slice-2 followup).
# ============================================================


class _FakeRustRenderer:
    """Minimal stand-in for RustRenderer used in AutoFallbackRenderer
    unit tests. Behaves enough like the real proxy for the wrapper to
    exercise its forwarding + swap-on-error paths without spinning up
    a real subprocess. The wrapper only depends on:
      - width / height attrs
      - open() / close() / render_frame()
      - the IPC ops (begin_slide, advance, ...)
      - raising `RustRendererSubprocessError` from any op to trigger fallback.
    Configure the raise behavior per-test via `raise_on_op`.
    """

    def __init__(self, width: int = 1920, height: int = 1080):
        self.width = width
        self.height = height
        self.open_called = 0
        self.close_called = 0
        self.calls: list[tuple[str, tuple, dict]] = []
        # op_name -> exception to raise once at that op's next call.
        self.raise_on_op: dict[str, Exception] = {}

    def _maybe_raise(self, op_name: str):
        exc = self.raise_on_op.pop(op_name, None)
        if exc is not None:
            raise exc

    def open(self):
        self.open_called += 1
        self.calls.append(("open", (), {}))
        self._maybe_raise("open")
        return ("mock_open_ok", self.width, self.height)

    def close(self):
        self.close_called += 1
        self.calls.append(("close", (), {}))
        # Don't raise from close — wrapper should swallow teardown
        # errors anyway, but we don't want to obscure assertions.

    def render_frame(self, frame: bytes, **kwargs) -> None:
        # HW-decode (2026-05-20): render_frame gained keyword-only
        # pixel_format / frame_w / frame_h; record them so the
        # forwarding tests can assert AutoFallbackRenderer threads
        # them through verbatim.
        self.calls.append(("render_frame", (len(frame),), kwargs))
        self._maybe_raise("render_frame")

    def begin_slide(self, *args, **kwargs):
        self.calls.append(("begin_slide", args, kwargs))
        self._maybe_raise("begin_slide")

    def advance(self, *args, **kwargs):
        self.calls.append(("advance", args, kwargs))
        self._maybe_raise("advance")
        return ("advance_ok", args, kwargs)

    def begin_transition(self, *args, **kwargs):
        self.calls.append(("begin_transition", args, kwargs))
        self._maybe_raise("begin_transition")

    def capture(self, *args, **kwargs):
        self.calls.append(("capture", args, kwargs))
        self._maybe_raise("capture")

    def reconfigure(self, *args, **kwargs):
        self.calls.append(("reconfigure", args, kwargs))
        self._maybe_raise("reconfigure")


class TestAutoFallbackRenderer:
    """Unit tests for the dependencies.AutoFallbackRenderer wrapper.

    The wrapper closes Phase 7 slice 2's robustness story: 1796584
    landed bounded auto-reconnect inside the proxy; this wrapper
    catches reconnect-exhaustion exceptions and swaps the dead
    proxy for a MockRenderer for the rest of the session.
    """

    @pytest.fixture
    def mock_factory_factory(self):
        """Returns a factory that builds a MockRenderer wired into the
        test's tmp directory + a sentinel callsite counter so tests can
        assert lazy construction."""
        def _build(tmp_path: Path):
            calls = [0]

            def _factory():
                calls[0] += 1
                return _mock_renderer_singleton()

            return _factory, calls

        return _build

    def test_satisfies_renderer_protocol(self, tmp_path):
        from openmarquee.dependencies import AutoFallbackRenderer
        from openmarquee.rendering import Renderer

        fake = _FakeRustRenderer(width=1920, height=1080)
        wrapper = AutoFallbackRenderer(fake, _mock_renderer_singleton)
        assert isinstance(wrapper, Renderer)
        assert wrapper.width == 1920
        assert wrapper.height == 1080

    def test_happy_path_forwards_to_primary(self, tmp_path):
        from openmarquee.dependencies import AutoFallbackRenderer

        fake = _FakeRustRenderer(width=128, height=96)
        wrapper = AutoFallbackRenderer(fake, _mock_renderer_singleton)
        frame = b"\xff\x00\x00" * 128 * 96  # 1 frame of red, RGB888
        wrapper.render_frame(frame)
        # HW-decode (2026-05-20): AutoFallbackRenderer forwards the
        # render_frame pixel_format / frame_w / frame_h kwargs
        # verbatim — for a plain (rgb888) call those are the defaults.
        _rgb_kwargs = {
            "pixel_format": "rgb888",
            "frame_w": None,
            "frame_h": None,
        }
        assert ("render_frame", (len(frame),), _rgb_kwargs) in fake.calls
        assert wrapper.is_in_fallback is False

    def test_render_frame_subprocess_error_triggers_fallback(self, tmp_path):
        """The headline behavior: when the primary raises
        RustRendererSubprocessError (e.g. reconnect exhausted), the
        wrapper swaps to MockRenderer and replays the same frame
        against it. is_in_fallback becomes True permanently."""
        from openmarquee.dependencies import AutoFallbackRenderer
        from openmarquee.rendering.mock import MockRenderer
        from openmarquee.rendering.rust_renderer import (
            RustRendererSubprocessError,
        )

        SettingsStorage(Path(os.environ["OPENMARQUEE_SETTINGS_PATH"])).save(
            SystemSettings(display_width=128, display_height=96)
        )
        _settings_storage_singleton.cache_clear()

        fake = _FakeRustRenderer(width=128, height=96)
        fake.raise_on_op["render_frame"] = RustRendererSubprocessError(
            "reconnect exhausted (max=3 in 60s) -- trail: [...]"
        )
        wrapper = AutoFallbackRenderer(fake, _mock_renderer_singleton)
        assert wrapper.is_in_fallback is False
        frame = b"\x00\xff\x00" * 128 * 96
        # Call should succeed: wrapper catches the exhaustion, swaps,
        # and replays the frame against MockRenderer.
        wrapper.render_frame(frame)
        assert wrapper.is_in_fallback is True
        # Primary was closed during the swap.
        assert fake.close_called == 1
        # Subsequent render_frame calls route to Mock without touching
        # the (now-released) primary.
        wrapper.render_frame(frame)
        # No NEW calls to the fake's render_frame after the first.
        # (The forwarded kwargs are the rgb888 defaults — see
        # test_happy_path_forwards_to_primary.)
        _rgb_kwargs = {
            "pixel_format": "rgb888",
            "frame_w": None,
            "frame_h": None,
        }
        assert (
            fake.calls.count(("render_frame", (len(frame),), _rgb_kwargs)) == 1
        )
        # The MockRenderer wrote the PNG at the dev preview path.
        preview = Path(os.environ["OPENMARQUEE_DEV_PREVIEW_PATH"])
        assert preview.exists()

    def test_fallback_is_one_way_permanent(self, tmp_path):
        """Once the wrapper falls back, it never goes back to the
        primary even if the primary 'recovers' (e.g. someone restarts
        the binary). Restart-of-process is the recovery story."""
        from openmarquee.dependencies import AutoFallbackRenderer
        from openmarquee.rendering.rust_renderer import (
            RustRendererSubprocessError,
        )

        SettingsStorage(Path(os.environ["OPENMARQUEE_SETTINGS_PATH"])).save(
            SystemSettings(display_width=128, display_height=96)
        )
        _settings_storage_singleton.cache_clear()
        fake = _FakeRustRenderer(width=128, height=96)
        fake.raise_on_op["render_frame"] = RustRendererSubprocessError("boom")
        wrapper = AutoFallbackRenderer(fake, _mock_renderer_singleton)
        wrapper.render_frame(b"\x00" * 128 * 96 * 3)
        assert wrapper.is_in_fallback
        # Wrapper released the reference to the dead primary.
        assert wrapper._primary is None

    def test_ipc_op_subprocess_error_swaps_and_raises_autofallback_error(
        self, tmp_path
    ):
        """When an IPC op (advance / begin_slide / etc.) raises
        SubprocessError on the primary, the wrapper swaps to Mock and
        re-raises as AutoFallbackInMockError so the caller knows to
        switch to render_frame()."""
        from openmarquee.dependencies import (
            AutoFallbackInMockError,
            AutoFallbackRenderer,
        )
        from openmarquee.rendering.rust_renderer import (
            RustRendererSubprocessError,
        )

        SettingsStorage(Path(os.environ["OPENMARQUEE_SETTINGS_PATH"])).save(
            SystemSettings(display_width=128, display_height=96)
        )
        _settings_storage_singleton.cache_clear()
        fake = _FakeRustRenderer()
        fake.raise_on_op["advance"] = RustRendererSubprocessError("boom")
        wrapper = AutoFallbackRenderer(fake, _mock_renderer_singleton)
        with pytest.raises(AutoFallbackInMockError, match="advance"):
            wrapper.advance(t_ms=100)
        assert wrapper.is_in_fallback is True
        # Subsequent IPC ops also raise (Mock can't satisfy IPC).
        with pytest.raises(AutoFallbackInMockError, match="begin_slide"):
            wrapper.begin_slide("slide_id", t0_ms=0, duration_ms=5000)
        # But render_frame routes cleanly to Mock.
        wrapper.render_frame(b"\x00" * 128 * 96 * 3)

    def test_open_subprocess_error_swaps_and_reraises(self, tmp_path):
        """If even open() can't launch, the wrapper swaps to Mock
        immediately but re-raises so the lifespan sees the original
        SubprocessError. Future render_frames work against Mock."""
        from openmarquee.dependencies import AutoFallbackRenderer
        from openmarquee.rendering.rust_renderer import (
            RustRendererSubprocessError,
        )

        SettingsStorage(Path(os.environ["OPENMARQUEE_SETTINGS_PATH"])).save(
            SystemSettings(display_width=128, display_height=96)
        )
        _settings_storage_singleton.cache_clear()
        fake = _FakeRustRenderer()
        fake.raise_on_op["open"] = RustRendererSubprocessError(
            "rust binary not found"
        )
        wrapper = AutoFallbackRenderer(fake, _mock_renderer_singleton)
        with pytest.raises(RustRendererSubprocessError, match="not found"):
            wrapper.open()
        assert wrapper.is_in_fallback is True
        # Mock takes over; render_frame works.
        wrapper.render_frame(b"\xff" * 128 * 96 * 3)

    def test_close_tears_down_primary_when_alive(self, tmp_path):
        """close() before any fallback: tears down the primary, no
        Mock construction (lazy)."""
        from openmarquee.dependencies import AutoFallbackRenderer

        fake = _FakeRustRenderer()
        wrapper = AutoFallbackRenderer(fake, _mock_renderer_singleton)
        wrapper.close()
        assert fake.close_called == 1
        # Second close is a no-op.
        wrapper.close()
        assert fake.close_called == 1

    def test_close_after_fallback_does_not_double_teardown(self, tmp_path):
        """After fallback, the primary was already closed during the
        swap. close() on the wrapper must NOT call close() on the
        (released) primary again, and must not raise."""
        from openmarquee.dependencies import AutoFallbackRenderer
        from openmarquee.rendering.rust_renderer import (
            RustRendererSubprocessError,
        )

        SettingsStorage(Path(os.environ["OPENMARQUEE_SETTINGS_PATH"])).save(
            SystemSettings(display_width=128, display_height=96)
        )
        _settings_storage_singleton.cache_clear()
        fake = _FakeRustRenderer()
        fake.raise_on_op["render_frame"] = RustRendererSubprocessError("boom")
        wrapper = AutoFallbackRenderer(fake, _mock_renderer_singleton)
        wrapper.render_frame(b"\x00" * 128 * 96 * 3)
        assert fake.close_called == 1
        wrapper.close()
        # close_called still 1 — wrapper didn't try to close the
        # already-torn-down primary again.
        assert fake.close_called == 1

    def test_context_manager_lifecycle(self, tmp_path):
        from openmarquee.dependencies import AutoFallbackRenderer

        fake = _FakeRustRenderer()
        wrapper = AutoFallbackRenderer(fake, _mock_renderer_singleton)
        with wrapper:
            assert fake.open_called == 1
        assert fake.close_called == 1

    def test_lazy_mock_construction(self, tmp_path):
        """The MockRenderer factory is only called on the FIRST fallback.
        Construction is otherwise lazy — happy-path operations don't
        pay the Mock-construction cost."""
        from openmarquee.dependencies import AutoFallbackRenderer
        from openmarquee.rendering.rust_renderer import (
            RustRendererSubprocessError,
        )

        SettingsStorage(Path(os.environ["OPENMARQUEE_SETTINGS_PATH"])).save(
            SystemSettings(display_width=128, display_height=96)
        )
        _settings_storage_singleton.cache_clear()
        calls = [0]

        def _factory():
            calls[0] += 1
            return _mock_renderer_singleton()

        fake = _FakeRustRenderer(width=128, height=96)
        wrapper = AutoFallbackRenderer(fake, _factory)
        # Happy path: no factory call.
        wrapper.render_frame(b"\x00" * 128 * 96 * 3)
        assert calls[0] == 0
        # Force a swap.
        fake.raise_on_op["render_frame"] = RustRendererSubprocessError("boom")
        wrapper.render_frame(b"\x00" * 128 * 96 * 3)
        assert calls[0] == 1
        # Subsequent renders don't reconstruct Mock.
        wrapper.render_frame(b"\x00" * 128 * 96 * 3)
        assert calls[0] == 1

    def test_non_subprocess_errors_propagate_unwrapped(self, tmp_path):
        """The wrapper only catches RustRendererSubprocessError. OTHER
        exceptions (e.g. RustRendererOpError for a TBD-image-slide, or
        a Python TypeError) propagate normally so callers see them."""
        from openmarquee.dependencies import AutoFallbackRenderer
        from openmarquee.rendering.rust_renderer import RustRendererOpError

        fake = _FakeRustRenderer()
        fake.raise_on_op["advance"] = RustRendererOpError(
            "paint_slide: image_slide requires content_root"
        )
        wrapper = AutoFallbackRenderer(fake, _mock_renderer_singleton)
        # Op-level error should propagate unchanged; no fallback swap.
        with pytest.raises(RustRendererOpError, match="image_slide"):
            wrapper.advance(t_ms=100)
        assert wrapper.is_in_fallback is False

    def test_respawned_error_does_not_trigger_fallback(self, tmp_path):
        """RustRendererRespawnedError is a SUBCLASS of SubprocessError
        but indicates a SUCCESSFUL reconnect (proxy is alive). The
        wrapper must NOT swap to Mock — it should re-raise so the
        caller knows to replay session state on the healthy proxy.

        Regression test for the subagent-flagged bug where the wrapper
        threw away a respawned-but-healthy primary."""
        from openmarquee.dependencies import AutoFallbackRenderer
        from openmarquee.rendering.rust_renderer import (
            RustRendererRespawnedError,
        )

        SettingsStorage(Path(os.environ["OPENMARQUEE_SETTINGS_PATH"])).save(
            SystemSettings(display_width=128, display_height=96)
        )
        _settings_storage_singleton.cache_clear()
        fake = _FakeRustRenderer(width=128, height=96)
        fake.raise_on_op["render_frame"] = RustRendererRespawnedError(
            "subprocess died during op 'render_frame'; proxy reconnected"
        )
        wrapper = AutoFallbackRenderer(fake, _mock_renderer_singleton)
        with pytest.raises(RustRendererRespawnedError):
            wrapper.render_frame(b"\x00" * 128 * 96 * 3)
        # Critical: wrapper did NOT swap to Mock — proxy stays.
        assert wrapper.is_in_fallback is False
        assert wrapper._primary is fake
        assert fake.close_called == 0  # primary not torn down

    def test_respawned_error_in_ipc_op_does_not_trigger_fallback(self, tmp_path):
        """Same regression as test_respawned_error_does_not_trigger_fallback
        but exercised through an IPC op (advance) instead of
        render_frame."""
        from openmarquee.dependencies import AutoFallbackRenderer
        from openmarquee.rendering.rust_renderer import (
            RustRendererRespawnedError,
        )

        fake = _FakeRustRenderer()
        fake.raise_on_op["advance"] = RustRendererRespawnedError(
            "subprocess died during op 'advance'; proxy reconnected"
        )
        wrapper = AutoFallbackRenderer(fake, _mock_renderer_singleton)
        with pytest.raises(RustRendererRespawnedError):
            wrapper.advance(t_ms=100)
        assert wrapper.is_in_fallback is False
        assert wrapper._primary is fake

    def test_unsupported_slide_error_does_not_trigger_fallback(self, tmp_path):
        """Slice 4: RustRendererUnsupportedSlideError signals a SLIDE-kind
        limitation (today: VideoSlide), not a process-layer failure. The
        wrapper must NOT swap to Mock; the playback loop catches the
        propagated exception and skips the slide.

        Wired BEFORE the SubprocessError clause in `_forward_ipc_op` --
        UnsupportedSlideError isn't a SubprocessError subclass so it'd
        propagate either way, but the explicit clause pins the policy
        (and emits the log line operators look for)."""
        from openmarquee.dependencies import AutoFallbackRenderer
        from openmarquee.rendering.rust_renderer import (
            RustRendererUnsupportedSlideError,
        )

        fake = _FakeRustRenderer()
        wire_msg = "Capture: VideoSlide capture not implemented (image + text only)"
        fake.raise_on_op["begin_slide"] = RustRendererUnsupportedSlideError(wire_msg)
        wrapper = AutoFallbackRenderer(fake, _mock_renderer_singleton)
        with pytest.raises(RustRendererUnsupportedSlideError) as exc_info:
            wrapper.begin_slide("00000000-0000-0000-0000-000000000001", 0, 5000)
        # Exception message preserved verbatim so playback's skip-log
        # carries the same wire-format string operators see in tests.
        assert exc_info.value.message == wire_msg
        # Critical: wrapper did NOT swap to Mock -- proxy stays.
        assert wrapper.is_in_fallback is False
        assert wrapper._primary is fake
        assert fake.close_called == 0

    def test_unsupported_slide_error_emits_skip_log(self, tmp_path, caplog):
        """Operator-visible log line at INFO is the AutoFallbackRenderer-
        level "this slide was skipped" signal. Slice 4 wires playback to
        catch the exception and advance; the log is the breadcrumb that
        ties the two halves of the story together."""
        import logging as _logging

        from openmarquee.dependencies import AutoFallbackRenderer
        from openmarquee.rendering.rust_renderer import (
            RustRendererUnsupportedSlideError,
        )

        fake = _FakeRustRenderer()
        fake.raise_on_op["advance"] = RustRendererUnsupportedSlideError(
            "Capture: VideoSlide capture not implemented (image + text only)"
        )
        wrapper = AutoFallbackRenderer(fake, _mock_renderer_singleton)
        with caplog.at_level(_logging.INFO, logger="openmarquee.dependencies"):
            with pytest.raises(RustRendererUnsupportedSlideError):
                wrapper.advance(t_ms=100)
        # Log line names the op + reproduces the wire-format message.
        matching = [
            r for r in caplog.records
            if "skipped" in r.getMessage() and "advance" in r.getMessage()
        ]
        assert matching, f"expected skip log; got {[r.getMessage() for r in caplog.records]}"

    def test_unsupported_slide_error_is_caught_before_subprocess_error(
        self, tmp_path
    ):
        """Subagent-flagged invariant from the slice-4 dispatch: the
        except chain MUST list UnsupportedSlideError BEFORE
        SubprocessError. Today the chain is technically safe even with
        SubprocessError first (UnsupportedSlideError isn't a
        SubprocessError subclass), but if the class hierarchy ever
        shifts, this test catches the regression."""
        import openmarquee.dependencies as deps_module
        import inspect

        src = inspect.getsource(deps_module.AutoFallbackRenderer._forward_ipc_op)
        unsupported_at = src.find("RustRendererUnsupportedSlideError")
        subprocess_at = src.find("RustRendererSubprocessError as e")
        assert unsupported_at > 0, "UnsupportedSlideError clause missing"
        assert subprocess_at > 0, "SubprocessError clause missing"
        assert unsupported_at < subprocess_at, (
            "UnsupportedSlideError must be caught BEFORE SubprocessError "
            "(see slice-4 dispatch subagent-review item #1)"
        )
