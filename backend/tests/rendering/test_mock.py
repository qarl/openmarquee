from pathlib import Path

import pytest
from PIL import Image

from openmarquee.rendering import Renderer
from openmarquee.rendering.mock import MockRenderer


def _solid_frame(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    """Build a row-major RGB frame filled with `color`."""
    return bytes(color) * (width * height)


def test_mock_renderer_satisfies_renderer_protocol(tmp_path: Path):
    renderer = MockRenderer(2, 2, tmp_path / "out.png")
    assert isinstance(renderer, Renderer)


def test_render_frame_stores_last_frame(tmp_path: Path):
    renderer = MockRenderer(2, 2, tmp_path / "out.png")
    assert renderer.last_frame is None
    frame = _solid_frame(2, 2, (255, 0, 0))
    renderer.render_frame(frame)
    assert renderer.last_frame == frame


def test_render_frame_writes_png_at_output_path(tmp_path: Path):
    output = tmp_path / "out.png"
    renderer = MockRenderer(4, 4, output)
    renderer.render_frame(_solid_frame(4, 4, (0, 255, 0)))

    assert output.exists()
    img = Image.open(output)
    assert img.size == (4, 4)
    assert img.mode == "RGB"
    assert img.getpixel((0, 0)) == (0, 255, 0)
    assert img.getpixel((3, 3)) == (0, 255, 0)


def test_render_frame_encodes_rgb_in_row_major_order(tmp_path: Path):
    """Two-pixel row: first pixel red, second pixel green, read back correctly."""
    output = tmp_path / "out.png"
    renderer = MockRenderer(2, 1, output)
    renderer.render_frame(b"\xff\x00\x00" + b"\x00\xff\x00")
    img = Image.open(output)
    assert img.getpixel((0, 0)) == (255, 0, 0)
    assert img.getpixel((1, 0)) == (0, 255, 0)


def test_render_frame_rejects_wrong_length(tmp_path: Path):
    renderer = MockRenderer(2, 2, tmp_path / "out.png")
    with pytest.raises(ValueError, match="does not match"):
        renderer.render_frame(b"\x00" * 5)


def test_render_frame_creates_parent_directory(tmp_path: Path):
    deep_output = tmp_path / "deeply" / "nested" / "preview.png"
    renderer = MockRenderer(1, 1, deep_output)
    renderer.render_frame(_solid_frame(1, 1, (128, 128, 128)))
    assert deep_output.exists()


def test_renderer_exposes_dimensions(tmp_path: Path):
    renderer = MockRenderer(128, 96, tmp_path / "out.png")
    assert renderer.width == 128
    assert renderer.height == 96


def test_render_frame_overwrites_previous_png(tmp_path: Path):
    output = tmp_path / "out.png"
    renderer = MockRenderer(1, 1, output)
    renderer.render_frame(b"\xff\x00\x00")
    renderer.render_frame(b"\x00\x00\xff")
    img = Image.open(output)
    assert img.getpixel((0, 0)) == (0, 0, 255)


def test_last_frame_updates_on_each_render(tmp_path: Path):
    renderer = MockRenderer(1, 1, tmp_path / "out.png")
    renderer.render_frame(b"\xff\x00\x00")
    assert renderer.last_frame == b"\xff\x00\x00"
    renderer.render_frame(b"\x00\xff\x00")
    assert renderer.last_frame == b"\x00\xff\x00"


# --- dynamic dims via get_dims callable ---


class TestDynamicDims:
    def test_get_dims_drives_width_height_each_access(self, tmp_path: Path):
        """width + height are properties — re-reading get_dims on each
        access is how settings-change-in-UI flows to the next frame
        without a renderer rebuild."""
        dims = {"value": (128, 96)}
        renderer = MockRenderer(
            output_path=tmp_path / "out.png",
            get_dims=lambda: dims["value"],
        )
        assert renderer.width == 128 and renderer.height == 96

        dims["value"] = (1920, 1080)
        assert renderer.width == 1920 and renderer.height == 1080

    def test_render_frame_honors_current_dims(self, tmp_path: Path):
        """A frame sized to the get_dims-reported shape is accepted;
        the written PNG matches that shape — so a settings-change to
        1920×1080 mid-run reflects on the next tick."""
        dims = {"value": (4, 2)}
        renderer = MockRenderer(
            output_path=tmp_path / "out.png",
            get_dims=lambda: dims["value"],
        )
        renderer.render_frame(_solid_frame(4, 2, (0, 255, 0)))
        assert Image.open(tmp_path / "out.png").size == (4, 2)

        dims["value"] = (2, 2)
        renderer.render_frame(_solid_frame(2, 2, (255, 0, 0)))
        assert Image.open(tmp_path / "out.png").size == (2, 2)

    def test_render_frame_rejects_mismatch_against_current_dims(self, tmp_path: Path):
        """A frame that was correct for OLD dims is rejected after
        get_dims flips — rather than silently truncating or crashing
        deeper in Pillow."""
        dims = {"value": (4, 2)}
        renderer = MockRenderer(
            output_path=tmp_path / "out.png",
            get_dims=lambda: dims["value"],
        )
        dims["value"] = (8, 8)
        with pytest.raises(ValueError, match="does not match"):
            renderer.render_frame(_solid_frame(4, 2, (10, 20, 30)))


# --- constructor validation ---


class TestConstructorValidation:
    def test_rejects_both_static_and_dynamic_dims(self, tmp_path: Path):
        with pytest.raises(ValueError, match="either"):
            MockRenderer(
                width=10, height=10,
                output_path=tmp_path / "out.png",
                get_dims=lambda: (20, 20),
            )

    def test_rejects_missing_width_when_no_get_dims(self, tmp_path: Path):
        with pytest.raises(ValueError, match="need both"):
            MockRenderer(width=10, output_path=tmp_path / "out.png")
        with pytest.raises(ValueError, match="need both"):
            MockRenderer(height=10, output_path=tmp_path / "out.png")

    def test_rejects_missing_output_path(self):
        with pytest.raises(TypeError, match="output_path"):
            MockRenderer(width=10, height=10)
