"""Unit + integration tests for HDMIRenderer.

Real /dev/fb0 verification needs a Pi — gated manually behind a
hardware marker. The tests here write to tmp files that behave
byte-for-byte like a framebuffer (seek-0 + write + stay that size),
so the pixel-format conversion and letterbox logic are fully
covered on a Mac.
"""

from pathlib import Path

import pytest

from openmarquee.rendering import Renderer
from openmarquee.rendering.hdmi import HDMIRenderer


# --- helpers ---


def _solid_rgb(w: int, h: int, color: tuple[int, int, int]) -> bytes:
    """Build a solid-color RGB888 frame `w*h*3` bytes long."""
    return bytes(color) * (w * h)


# --- construction + argument validation ---


class TestConstruction:
    def test_satisfies_renderer_protocol(self, tmp_path: Path):
        r = HDMIRenderer(width=4, height=3, output_path=tmp_path / "fb")
        assert isinstance(r, Renderer)
        assert r.width == 4
        assert r.height == 3

    def test_defaults_display_dims_to_sign_dims(self, tmp_path: Path):
        r = HDMIRenderer(width=64, height=32, output_path=tmp_path / "fb")
        assert r.display_width == 64
        assert r.display_height == 32

    def test_accepts_explicit_display_dims(self, tmp_path: Path):
        r = HDMIRenderer(
            width=64,
            height=32,
            display_width=1920,
            display_height=1080,
            output_path=tmp_path / "fb",
        )
        assert (r.display_width, r.display_height) == (1920, 1080)

    def test_rejects_unknown_pixel_format(self, tmp_path: Path):
        with pytest.raises(ValueError, match="pixel_format"):
            HDMIRenderer(
                width=4, height=3, output_path=tmp_path / "fb", pixel_format="yuv420"
            )

    def test_rejects_non_positive_dims(self, tmp_path: Path):
        with pytest.raises(ValueError):
            HDMIRenderer(width=0, height=1, output_path=tmp_path / "fb")
        with pytest.raises(ValueError):
            HDMIRenderer(
                width=2,
                height=2,
                display_width=0,
                display_height=10,
                output_path=tmp_path / "fb",
            )


# --- render_frame input validation ---


class TestRenderFrameValidation:
    def test_rejects_wrong_length_frame(self, tmp_path: Path):
        r = HDMIRenderer(width=4, height=3, output_path=tmp_path / "fb")
        with pytest.raises(ValueError, match="frame length"):
            r.render_frame(b"\x00" * 10)  # needs 4*3*3 = 36 bytes
        r.close()


# --- byte-layout correctness (rgb24) ---


class TestRgb24Output:
    def test_passthrough_when_display_dims_match_sign(self, tmp_path: Path):
        """With pixel_format=rgb24 and matching dims, the fb contents
        should equal the input frame byte-for-byte — simplest contract
        worth locking down."""
        path = tmp_path / "fb"
        r = HDMIRenderer(
            width=2,
            height=2,
            output_path=path,
            pixel_format="rgb24",
        )
        frame = bytes([
            10, 20, 30,   40, 50, 60,
            70, 80, 90,  100, 110, 120,
        ])
        r.render_frame(frame)
        r.close()
        assert path.read_bytes() == frame

    def test_seek_zero_overwrites_previous_frame(self, tmp_path: Path):
        """Consecutive render_frame calls must overwrite in place, not
        append — the Pi's fb device has fixed size, and behaviour on
        the tmp-file substitute must match that contract."""
        path = tmp_path / "fb"
        r = HDMIRenderer(
            width=2, height=1, output_path=path, pixel_format="rgb24"
        )
        frame_a = _solid_rgb(2, 1, (255, 0, 0))
        frame_b = _solid_rgb(2, 1, (0, 255, 0))
        r.render_frame(frame_a)
        r.render_frame(frame_b)
        r.close()
        # File is still exactly one frame long, containing frame_b only.
        assert path.read_bytes() == frame_b


# --- byte-layout correctness (bgra32, the Pi HDMI default) ---


class TestBgra32Output:
    def test_channels_swap_and_alpha_is_opaque(self, tmp_path: Path):
        """RGB888 `10, 20, 30` must land as BGRA `30, 20, 10, 255` on
        the framebuffer — this is the one conversion the real Pi HDMI
        output depends on and the highest-value thing to pin."""
        path = tmp_path / "fb"
        r = HDMIRenderer(
            width=1, height=1, output_path=path, pixel_format="bgra32"
        )
        r.render_frame(bytes([10, 20, 30]))
        r.close()
        assert path.read_bytes() == bytes([30, 20, 10, 255])

    def test_multi_pixel_bgra_layout(self, tmp_path: Path):
        path = tmp_path / "fb"
        r = HDMIRenderer(
            width=2, height=1, output_path=path, pixel_format="bgra32"
        )
        # Pixel 0 = (10, 20, 30), pixel 1 = (200, 100, 50)
        r.render_frame(bytes([10, 20, 30, 200, 100, 50]))
        r.close()
        assert path.read_bytes() == bytes([
            30, 20, 10, 255,      # pixel 0 as BGRA
            50, 100, 200, 255,    # pixel 1 as BGRA
        ])


# --- display-size upscale + letterbox ---


class TestDisplayUpscale:
    def test_simple_integer_upscale_nearest(self, tmp_path: Path):
        """A 2×2 solid-red sign upscaled to a 4×4 display should be a
        4×4 solid-red field (no blending, no black bars). Validates
        the NEAREST path."""
        path = tmp_path / "fb"
        r = HDMIRenderer(
            width=2,
            height=2,
            display_width=4,
            display_height=4,
            output_path=path,
            pixel_format="rgb24",
        )
        r.render_frame(_solid_rgb(2, 2, (255, 0, 0)))
        r.close()
        out = path.read_bytes()
        assert len(out) == 4 * 4 * 3
        assert out == _solid_rgb(4, 4, (255, 0, 0))

    def test_aspect_mismatch_letterboxes_with_black(self, tmp_path: Path):
        """Sign is 2×1 (wide), display is 2×2 (square). The image fills
        the top half-ish (fit by width); the remainder is black bars."""
        path = tmp_path / "fb"
        r = HDMIRenderer(
            width=2,
            height=1,
            display_width=2,
            display_height=2,
            output_path=path,
            pixel_format="rgb24",
        )
        r.render_frame(_solid_rgb(2, 1, (255, 0, 0)))
        r.close()
        out = path.read_bytes()
        # Expect: one row of red, one row of black (the sign fits at
        # width so height = 1 row; the other row is letterbox).
        red_row = bytes((255, 0, 0)) * 2
        black_row = bytes((0, 0, 0)) * 2
        # Row ordering depends on where the paste centers — with a
        # 1-pixel remainder the centered offset is 0, so the red row
        # is at index 0 and the black row at index 1.
        assert out == red_row + black_row

    def test_sign_bigger_than_display_downscales(self, tmp_path: Path):
        """Unusual but allowed: sign 4×4 on a 2×2 display. Shouldn't
        crash — should render a 2×2 downsampled image."""
        path = tmp_path / "fb"
        r = HDMIRenderer(
            width=4,
            height=4,
            display_width=2,
            display_height=2,
            output_path=path,
            pixel_format="rgb24",
        )
        r.render_frame(_solid_rgb(4, 4, (0, 255, 0)))
        r.close()
        assert path.read_bytes() == _solid_rgb(2, 2, (0, 255, 0))


# --- lifecycle ---


class TestLifecycle:
    def test_context_manager_opens_and_closes(self, tmp_path: Path):
        path = tmp_path / "fb"
        with HDMIRenderer(
            width=1, height=1, output_path=path, pixel_format="rgb24"
        ) as r:
            assert r._fd is not None
            r.render_frame(b"\x10\x20\x30")
        # Exiting the block closes the fd.
        assert r._fd is None
        assert path.read_bytes() == b"\x10\x20\x30"

    def test_render_frame_without_context_manager_auto_opens(self, tmp_path: Path):
        """Operators who skip `with` still get a valid render — the fd
        is opened lazily on first frame. They're responsible for close()
        (leak isn't catastrophic on a long-running daemon — the kernel
        reaps on exit — but we document it)."""
        r = HDMIRenderer(
            width=1, height=1, output_path=tmp_path / "fb", pixel_format="rgb24"
        )
        r.render_frame(b"\xaa\xbb\xcc")
        assert r._fd is not None
        r.close()
        assert r._fd is None

    def test_close_is_idempotent(self, tmp_path: Path):
        r = HDMIRenderer(
            width=1, height=1, output_path=tmp_path / "fb", pixel_format="rgb24"
        )
        r.close()
        r.close()  # second call must not raise


# --- integration: PlaybackLoop drives HDMIRenderer ---


@pytest.mark.asyncio
async def test_playback_loop_drives_hdmi_renderer(tmp_path: Path):
    """End-to-end-ish: build a PlaybackLoop pointing at an HDMIRenderer
    writing to a tmp file. Push a TextSlide with a bright-color PNG
    through and assert the tmp file ends up with the expected BGRA
    bytes. Proves HDMIRenderer satisfies the renderer protocol the
    loop expects."""
    import asyncio
    import io

    from PIL import Image

    from openmarquee.content import TextSlide
    from openmarquee.playback import PlaybackLoop

    # Tiny 2×2 green PNG so the assertion below is readable.
    img = Image.new("RGB", (2, 2), (0, 255, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png = buf.getvalue()

    slide = TextSlide(name="g", text="x", duration_ms=100)
    fb_path = tmp_path / "fb0"

    renderer = HDMIRenderer(
        width=2, height=2, output_path=fb_path, pixel_format="bgra32"
    )
    loop = PlaybackLoop(
        renderer=renderer,
        fetch_items=lambda: [slide],
        read_asset=lambda _id: png,
        empty_playlist_poll_seconds=0.01,
    )
    try:
        await loop.start()
        await asyncio.sleep(0.08)  # less than the slide's 100ms duration
        await loop.stop()
    finally:
        renderer.close()

    # Each pixel: green RGB (0, 255, 0) → BGRA (0, 255, 0, 255).
    expected = bytes([0, 255, 0, 255]) * 4
    assert fb_path.read_bytes() == expected
