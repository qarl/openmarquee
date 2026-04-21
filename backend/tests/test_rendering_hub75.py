"""Unit tests for HUB75Renderer — the dry-land parts.

Real panel output needs a Pi + Adafruit Bonnet + HUB75 panel + PSU;
`_write_to_panel` is a stub that raises NotImplementedError so the
missing library doesn't masquerade as a silent-success bug. The
Renderer protocol contract, all config validation, the gamma/
brightness LUT, frame prep, and lifecycle are all exercised today.
"""

from pathlib import Path

import pytest

from openmarquee.rendering import Renderer
from openmarquee.rendering.hub75 import HUB75Renderer, _build_gamma_lut


# --- construction + argument validation ---


class TestConstructionValidation:
    def test_satisfies_renderer_protocol(self):
        r = HUB75Renderer(width=64, height=32)
        assert isinstance(r, Renderer)
        assert r.width == 64 and r.height == 32

    def test_default_config_is_sensible(self):
        r = HUB75Renderer(width=64, height=32)
        assert r.chain_length == 1
        assert r.parallel_chains == 1
        assert r.brightness == 100
        assert r.gamma == 2.2
        assert r.pwm_bits == 11
        assert r.gpio_slowdown == 1
        assert r.pixel_mapper is None

    @pytest.mark.parametrize("w,h", [(0, 32), (64, 0), (-1, 32), (64, -1)])
    def test_rejects_non_positive_dims(self, w, h):
        with pytest.raises(ValueError):
            HUB75Renderer(width=w, height=h)

    def test_rejects_chain_length_below_one(self):
        with pytest.raises(ValueError, match="chain_length"):
            HUB75Renderer(width=64, height=32, chain_length=0)

    @pytest.mark.parametrize("n", [0, 4, -1])
    def test_parallel_chains_limited_to_1_through_3(self, n):
        with pytest.raises(ValueError, match="parallel_chains"):
            HUB75Renderer(width=64, height=32, parallel_chains=n)

    @pytest.mark.parametrize("b", [-1, 101, 200])
    def test_rejects_out_of_range_brightness(self, b):
        with pytest.raises(ValueError, match="brightness"):
            HUB75Renderer(width=64, height=32, brightness=b)

    @pytest.mark.parametrize("g", [0.0, 0.4, 4.1, 10.0])
    def test_rejects_out_of_range_gamma(self, g):
        with pytest.raises(ValueError, match="gamma"):
            HUB75Renderer(width=64, height=32, gamma=g)

    @pytest.mark.parametrize("bits", [0, 12, -1])
    def test_rejects_out_of_range_pwm_bits(self, bits):
        with pytest.raises(ValueError, match="pwm_bits"):
            HUB75Renderer(width=64, height=32, pwm_bits=bits)

    @pytest.mark.parametrize("s", [0, 5, -1])
    def test_rejects_out_of_range_gpio_slowdown(self, s):
        with pytest.raises(ValueError, match="gpio_slowdown"):
            HUB75Renderer(width=64, height=32, gpio_slowdown=s)

    def test_rejects_empty_pixel_mapper_string(self):
        with pytest.raises(ValueError, match="pixel_mapper"):
            HUB75Renderer(width=64, height=32, pixel_mapper="  ")

    def test_unknown_pixel_mapper_passes_through_with_warning(self, caplog):
        """The hzeller allowlist is superset of ours; a typo-vs-exotic
        is ambiguous. Log, don't reject, so a valid-but-rare mapper
        still works on hardware day."""
        import logging
        with caplog.at_level(logging.WARNING):
            r = HUB75Renderer(
                width=64, height=32, pixel_mapper="ExoticButValid",
            )
        assert r.pixel_mapper == "ExoticButValid"
        assert any("pixel_mapper" in rec.message for rec in caplog.records)

    def test_known_pixel_mapper_does_not_warn(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            HUB75Renderer(
                width=64, height=32, pixel_mapper="U-mapper",
            )
        assert not any("pixel_mapper" in rec.message for rec in caplog.records)


# --- derived physical dimensions ---


class TestPhysicalDimensions:
    def test_single_panel_passthrough(self):
        r = HUB75Renderer(width=64, height=32)
        assert (r.physical_width, r.physical_height) == (64, 32)

    def test_chain_length_multiplies_width(self):
        r = HUB75Renderer(width=64, height=32, chain_length=3)
        assert (r.physical_width, r.physical_height) == (192, 32)

    def test_parallel_chains_multiplies_height(self):
        r = HUB75Renderer(
            width=64, height=32, chain_length=2, parallel_chains=2
        )
        assert (r.physical_width, r.physical_height) == (128, 64)


# --- gamma / brightness LUT correctness ---


class TestGammaLUT:
    def test_lut_has_256_entries(self):
        lut = _build_gamma_lut(gamma=2.2, brightness=100)
        assert len(lut) == 256

    def test_lut_zero_maps_to_zero(self):
        assert _build_gamma_lut(gamma=2.2, brightness=100)[0] == 0

    def test_lut_max_at_full_brightness_reaches_255(self):
        assert _build_gamma_lut(gamma=2.2, brightness=100)[255] == 255

    def test_lut_is_monotonic_non_decreasing(self):
        lut = _build_gamma_lut(gamma=2.2, brightness=100)
        for i in range(1, 256):
            assert lut[i] >= lut[i - 1]

    def test_brightness_50_caps_max_around_half(self):
        """brightness=50 with gamma=1 (linear) should take 255 → ~128."""
        lut = _build_gamma_lut(gamma=1.0, brightness=50)
        assert 125 <= lut[255] <= 130
        assert lut[0] == 0

    def test_brightness_0_zeros_everything(self):
        lut = _build_gamma_lut(gamma=2.2, brightness=0)
        assert all(v == 0 for v in lut)

    def test_gamma_2_2_mid_point_approximation(self):
        """(128/255)^2.2 * 255 ≈ 55 — standard gamma curve midpoint."""
        lut = _build_gamma_lut(gamma=2.2, brightness=100)
        assert 50 <= lut[128] <= 60


# --- frame prep ---


class TestPrepareFrame:
    def test_applies_lut_to_every_byte(self):
        r = HUB75Renderer(
            width=1, height=1, gamma=1.0, brightness=50,
        )
        prepared = r._prepare_frame(bytes([0, 128, 255]))
        # Gamma 1 + brightness 50 = linear scale to ~half.
        assert prepared[0] == 0
        assert 60 <= prepared[1] <= 68   # 128 * 0.5
        assert 125 <= prepared[2] <= 130  # 255 * 0.5

    def test_prepared_length_matches_input_length(self):
        r = HUB75Renderer(width=4, height=2)
        prepared = r._prepare_frame(b"\x80" * 24)
        assert len(prepared) == 24

    def test_brightness_100_gamma_1_is_identity(self):
        r = HUB75Renderer(width=1, height=1, gamma=1.0, brightness=100)
        frame = bytes([0, 50, 128, 200, 255] + [0] * (3 - 3))[:3]
        prepared = r._prepare_frame(frame)
        # Linear + full brightness = approx passthrough (rounding tolerance).
        for src, out in zip(frame, prepared):
            assert abs(int(out) - int(src)) <= 1


# --- render_frame input validation ---


class TestRenderFrameValidation:
    def test_rejects_wrong_length_frame(self, tmp_path: Path):
        r = HUB75Renderer(
            width=2, height=2, output_path=tmp_path / "panel.bin"
        )
        with pytest.raises(ValueError, match="frame length"):
            r.render_frame(b"\x00" * 10)  # needs 12 bytes
        r.close()


# --- dry-land file sink ---


class TestOutputPathSink:
    def test_writes_prepared_bytes_when_output_path_set(self, tmp_path: Path):
        """With `output_path` set, render_frame writes the LUT-processed
        bytes to the file — dev + CI can read them back and assert."""
        path = tmp_path / "panel.bin"
        r = HUB75Renderer(
            width=1, height=1,
            gamma=1.0, brightness=50,  # linear + half = easy to verify
            output_path=path,
        )
        r.render_frame(bytes([0, 128, 200]))
        r.close()
        got = path.read_bytes()
        assert len(got) == 3
        assert got[0] == 0
        assert 60 <= got[1] <= 68
        assert 99 <= got[2] <= 101

    def test_consecutive_renders_overwrite_in_place(self, tmp_path: Path):
        path = tmp_path / "panel.bin"
        r = HUB75Renderer(
            width=1, height=1,
            gamma=1.0, brightness=100,
            output_path=path,
        )
        r.render_frame(bytes([10, 20, 30]))
        r.render_frame(bytes([40, 50, 60]))
        r.close()
        # File is still exactly one frame long, holding the second write.
        assert len(path.read_bytes()) == 3
        assert path.read_bytes() == bytes([40, 50, 60])


# --- hardware stub ---


class TestHardwareStub:
    def test_render_frame_without_output_path_raises_not_implemented(self):
        """Without a dry-land sink, calling render_frame hits the
        hardware path — which has to raise a helpful
        NotImplementedError, not a silent no-op or a crash that
        looks like a bug."""
        r = HUB75Renderer(width=2, height=2)
        with pytest.raises(NotImplementedError) as excinfo:
            r.render_frame(b"\x00" * 12)
        assert "rpi-rgb-led-matrix" in str(excinfo.value)
        assert "Phase-8" in str(excinfo.value)

    def test_stub_error_mentions_dry_land_workaround(self):
        """Operators reading the traceback on a dev machine should be
        told they can pass `output_path=` to exercise the prep path."""
        r = HUB75Renderer(width=1, height=1)
        with pytest.raises(NotImplementedError) as excinfo:
            r.render_frame(b"\x00\x00\x00")
        assert "output_path" in str(excinfo.value)


# --- lifecycle ---


class TestLifecycle:
    def test_context_manager_with_output_path(self, tmp_path: Path):
        path = tmp_path / "panel.bin"
        with HUB75Renderer(width=1, height=1, output_path=path) as r:
            r.render_frame(b"\x80\x80\x80")
            assert r._fd is not None
        assert r._fd is None

    def test_context_manager_without_output_path_is_noop_on_close(self):
        """No sink opened means nothing to close — the stub must still
        accept the `with` syntax cleanly."""
        with HUB75Renderer(width=1, height=1) as r:
            assert r._fd is None
        assert r._fd is None

    def test_close_is_idempotent(self, tmp_path: Path):
        r = HUB75Renderer(
            width=1, height=1, output_path=tmp_path / "panel.bin"
        )
        r.render_frame(b"\x00\x00\x00")
        r.close()
        r.close()  # must not raise
