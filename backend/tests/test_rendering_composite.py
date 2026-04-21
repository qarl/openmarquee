"""Tests for CompositeRenderer.

CompositeRenderer inherits the byte-layout + upscale + lifecycle
machinery from HDMIRenderer (extensively covered in
`test_rendering_hdmi.py`). These tests pin only the composite-
specific surface area: tv_mode picks the right default dims and
can be overridden.
"""

from pathlib import Path

import pytest

from openmarquee.rendering import Renderer
from openmarquee.rendering.composite import CompositeRenderer
from openmarquee.rendering.hdmi import HDMIRenderer


class TestCompositeRenderer:
    def test_is_a_renderer_and_an_hdmi_renderer(self, tmp_path: Path):
        r = CompositeRenderer(width=64, height=48, output_path=tmp_path / "fb")
        assert isinstance(r, Renderer)
        assert isinstance(r, HDMIRenderer)
        assert r.tv_mode == "ntsc"

    def test_ntsc_defaults_to_720x480(self, tmp_path: Path):
        r = CompositeRenderer(
            width=128, height=96, tv_mode="ntsc", output_path=tmp_path / "fb"
        )
        assert (r.display_width, r.display_height) == (720, 480)

    def test_pal_defaults_to_720x576(self, tmp_path: Path):
        r = CompositeRenderer(
            width=128, height=96, tv_mode="pal", output_path=tmp_path / "fb"
        )
        assert (r.display_width, r.display_height) == (720, 576)

    def test_explicit_display_dims_override_tv_mode(self, tmp_path: Path):
        """Operators driving a non-standard encoder (RF modulator etc.)
        should be able to specify custom raster size, overriding the
        tv_mode convenience default."""
        r = CompositeRenderer(
            width=64, height=48,
            tv_mode="ntsc",
            display_width=640,
            display_height=480,
            output_path=tmp_path / "fb",
        )
        assert (r.display_width, r.display_height) == (640, 480)

    def test_rejects_unknown_tv_mode(self, tmp_path: Path):
        with pytest.raises(ValueError, match="tv_mode"):
            CompositeRenderer(
                width=64, height=48,
                tv_mode="secam",  # type: ignore[arg-type]
                output_path=tmp_path / "fb",
            )

    def test_renders_and_upscales_to_ntsc_raster(self, tmp_path: Path):
        """End-to-end sanity: push a 2×1 red frame, tmp-fb contains
        720×480 BGRA bytes with the sign content letterboxed. Exact
        letterbox layout is covered by HDMIRenderer tests; here we
        just verify the payload size matches the tv_mode default."""
        path = tmp_path / "fb"
        r = CompositeRenderer(
            width=2, height=1,
            tv_mode="ntsc",
            output_path=path,
        )
        r.render_frame(bytes([255, 0, 0, 255, 0, 0]))
        r.close()
        assert path.stat().st_size == 720 * 480 * 4

    def test_rgb24_pixel_format_honored(self, tmp_path: Path):
        """Pixel format pass-through to the parent: `rgb24` writes 3
        bytes/pixel even for a composite renderer."""
        path = tmp_path / "fb"
        r = CompositeRenderer(
            width=2, height=1,
            tv_mode="pal",
            pixel_format="rgb24",
            output_path=path,
        )
        r.render_frame(bytes([255, 0, 0, 255, 0, 0]))
        r.close()
        assert path.stat().st_size == 720 * 576 * 3
