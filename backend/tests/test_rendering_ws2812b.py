"""Unit + integration tests for WS2812BRenderer.

Real LED strip verification needs a Pi + chain + PSU — gated behind
a hardware marker. The byte-layout + pixel-map logic is fully
covered here against a tmp file (the same abstraction the encoder
uses; Phase-10 bring-up just swaps the file-fd sink for the
`rpi_ws281x` DMA buffer).
"""

from pathlib import Path

import pytest

from openmarquee.rendering import Renderer
from openmarquee.rendering.ws2812b import WS2812BRenderer


def _rgb_frame(width: int, height: int, pixels: dict[tuple[int, int], tuple[int, int, int]], fill=(0, 0, 0)) -> bytes:
    """Build an RGB888 frame: `fill` everywhere except `pixels` overrides."""
    buf = bytearray(width * height * 3)
    for y in range(height):
        for x in range(width):
            color = pixels.get((x, y), fill)
            i = (y * width + x) * 3
            buf[i:i + 3] = bytes(color)
    return bytes(buf)


# --- construction + argument validation ---


class TestConstruction:
    def test_satisfies_renderer_protocol(self, tmp_path: Path):
        r = WS2812BRenderer(width=4, height=3, output_path=tmp_path / "chain.bin")
        assert isinstance(r, Renderer)
        assert r.width == 4 and r.height == 3
        assert r.led_count == 12  # row_major default

    def test_rejects_non_positive_dims(self, tmp_path: Path):
        with pytest.raises(ValueError):
            WS2812BRenderer(width=0, height=4, output_path=tmp_path / "x.bin")
        with pytest.raises(ValueError):
            WS2812BRenderer(width=4, height=-1, output_path=tmp_path / "x.bin")

    def test_rejects_unknown_strategy_name(self, tmp_path: Path):
        with pytest.raises(ValueError, match="pixel_map strategy"):
            WS2812BRenderer(
                width=2, height=2,
                pixel_map="diagonal",
                output_path=tmp_path / "x.bin",
            )

    def test_rejects_custom_map_with_out_of_bounds_coord(self, tmp_path: Path):
        with pytest.raises(ValueError, match="outside"):
            WS2812BRenderer(
                width=2, height=2,
                pixel_map=[(0, 0), (3, 0)],  # x=3 out of a 2-wide frame
                output_path=tmp_path / "x.bin",
            )

    def test_rejects_empty_custom_map(self, tmp_path: Path):
        with pytest.raises(ValueError, match="non-empty"):
            WS2812BRenderer(
                width=2, height=2, pixel_map=[],
                output_path=tmp_path / "x.bin",
            )

    def test_rejects_malformed_custom_map_entries(self, tmp_path: Path):
        with pytest.raises(TypeError):
            WS2812BRenderer(
                width=2, height=2,
                pixel_map=[(0, 0), (1,)],  # not a 2-tuple
                output_path=tmp_path / "x.bin",
            )


# --- render_frame input validation ---


class TestRenderFrameValidation:
    def test_rejects_wrong_length_frame(self, tmp_path: Path):
        r = WS2812BRenderer(width=2, height=2, output_path=tmp_path / "c.bin")
        with pytest.raises(ValueError, match="frame length"):
            r.render_frame(b"\x00" * 5)  # needs 2*2*3 = 12 bytes
        r.close()


# --- GRB channel order (the one thing a WS2812B driver *has* to get right) ---


class TestGRBChannelOrder:
    def test_rgb_10_20_30_serialises_as_grb_20_10_30(self, tmp_path: Path):
        """RGB888 `(R=10, G=20, B=30)` must land on the wire as
        `(G=20, R=10, B=30)`. Highest-value invariant in the module."""
        path = tmp_path / "c.bin"
        r = WS2812BRenderer(width=1, height=1, output_path=path)
        r.render_frame(bytes([10, 20, 30]))
        r.close()
        assert path.read_bytes() == bytes([20, 10, 30])

    def test_multi_pixel_chain_is_grb_concatenation(self, tmp_path: Path):
        path = tmp_path / "c.bin"
        r = WS2812BRenderer(width=2, height=1, output_path=path)
        # Pixel 0 = (10, 20, 30); pixel 1 = (200, 100, 50).
        r.render_frame(bytes([10, 20, 30, 200, 100, 50]))
        r.close()
        assert path.read_bytes() == bytes([
            20, 10, 30,     # LED 0: G R B
            100, 200, 50,   # LED 1: G R B
        ])


# --- pixel-map strategies ---


class TestRowMajorStrategy:
    def test_default_strategy_is_row_major(self, tmp_path: Path):
        """A red dot at (1, 0) on a 2×2 must be the second LED, and only
        the second LED — confirms the y*width+x layout."""
        path = tmp_path / "c.bin"
        r = WS2812BRenderer(width=2, height=2, output_path=path)
        frame = _rgb_frame(2, 2, {(1, 0): (255, 0, 0)})
        r.render_frame(frame)
        r.close()
        # 4 LEDs, each 3 bytes. Only LED index 1 (= (1, 0)) is red.
        expected = bytearray(4 * 3)
        expected[3:6] = bytes([0, 255, 0])  # GRB for pure red
        assert path.read_bytes() == bytes(expected)


class TestSerpentineStrategy:
    def test_row_1_is_reversed(self, tmp_path: Path):
        """Serpentine: row 0 L→R, row 1 R→L. A red dot at the LEFT of
        row 1 maps to the LAST LED in the chain, not the first of row 1.
        """
        path = tmp_path / "c.bin"
        r = WS2812BRenderer(
            width=3, height=2,
            pixel_map="serpentine",
            output_path=path,
        )
        # Pixel (0, 1) = top-left of row 1 = far end of the serpentine.
        frame = _rgb_frame(3, 2, {(0, 1): (255, 0, 0)})
        r.render_frame(frame)
        r.close()

        out = path.read_bytes()
        # 6 LEDs, 18 bytes total. Expect the last LED (index 5) red.
        assert len(out) == 18
        # LED 0..4 are black, LED 5 is GRB red.
        for i in range(5):
            assert out[i * 3:i * 3 + 3] == b"\x00\x00\x00"
        assert out[15:18] == bytes([0, 255, 0])


class TestCustomPixelMap:
    def test_custom_map_controls_chain_length_and_order(self, tmp_path: Path):
        """A 3-LED chain wired (1,1) → (0,0) → (1,0) maps those exact
        pixels onto the chain in that order. Physical LED positions
        that aren't in the map simply don't exist on the strip."""
        path = tmp_path / "c.bin"
        r = WS2812BRenderer(
            width=2, height=2,
            pixel_map=[(1, 1), (0, 0), (1, 0)],
            output_path=path,
        )
        assert r.led_count == 3
        frame = _rgb_frame(2, 2, {
            (0, 0): (10, 0, 0),   # red-ish
            (1, 0): (0, 20, 0),   # green-ish
            (0, 1): (0, 0, 30),   # blue-ish (NOT in the map → dropped)
            (1, 1): (40, 50, 60),
        })
        r.render_frame(frame)
        r.close()

        out = path.read_bytes()
        # Chain order: (1,1), (0,0), (1,0). Each LED serialised GRB.
        assert out == bytes([
            50, 40, 60,   # (1, 1) GRB = (50, 40, 60)
            0, 10, 0,     # (0, 0)
            20, 0, 0,     # (1, 0)
        ])


# --- seek-0 overwrite semantics (matches the fixed-size DMA buffer on Pi) ---


class TestOverwriteSemantics:
    def test_consecutive_frames_overwrite_in_place(self, tmp_path: Path):
        path = tmp_path / "c.bin"
        r = WS2812BRenderer(width=1, height=1, output_path=path)
        r.render_frame(bytes([10, 20, 30]))   # GRB = (20, 10, 30)
        r.render_frame(bytes([99, 88, 77]))   # GRB = (88, 99, 77)
        r.close()
        # File still exactly one LED long, containing only the second write.
        assert path.read_bytes() == bytes([88, 99, 77])


# --- lifecycle ---


class TestLifecycle:
    def test_context_manager_opens_and_closes(self, tmp_path: Path):
        path = tmp_path / "c.bin"
        with WS2812BRenderer(width=1, height=1, output_path=path) as r:
            assert r._fd is not None
            r.render_frame(b"\x01\x02\x03")
        assert r._fd is None
        assert path.read_bytes() == bytes([2, 1, 3])

    def test_render_without_context_manager_auto_opens(self, tmp_path: Path):
        r = WS2812BRenderer(width=1, height=1, output_path=tmp_path / "c.bin")
        r.render_frame(b"\x10\x20\x30")
        assert r._fd is not None
        r.close()
        assert r._fd is None

    def test_close_is_idempotent(self, tmp_path: Path):
        r = WS2812BRenderer(width=1, height=1, output_path=tmp_path / "c.bin")
        r.close()
        r.close()  # must not raise


# --- integration: PlaybackLoop drives WS2812BRenderer ---


@pytest.mark.asyncio
async def test_playback_loop_drives_ws2812b_renderer(tmp_path: Path):
    """Push a TextSlide with a solid-blue PNG through the loop via a
    WS2812B renderer writing to a tmp file. All 4 LEDs should end up
    encoded as GRB `(0, 0, 255)` in chain order."""
    import asyncio
    import io

    from PIL import Image

    from openmarquee.content import TextSlide
    from openmarquee.playback import PlaybackLoop

    img = Image.new("RGB", (2, 2), (0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png = buf.getvalue()

    slide = TextSlide(name="blue", text="x", duration_ms=100)
    chain_path = tmp_path / "chain.bin"

    renderer = WS2812BRenderer(width=2, height=2, output_path=chain_path)
    loop = PlaybackLoop(
        renderer=renderer,
        fetch_items=lambda: [slide],
        read_asset=lambda _id: png,
        empty_playlist_poll_seconds=0.01,
    )
    try:
        await loop.start()
        await asyncio.sleep(0.08)
        await loop.stop()
    finally:
        renderer.close()

    # 4 LEDs × GRB of blue (R=0, G=0, B=255) = (0, 0, 255) per LED.
    assert chain_path.read_bytes() == bytes([0, 0, 255]) * 4
