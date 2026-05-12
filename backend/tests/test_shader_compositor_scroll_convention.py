"""Pin the Python shader_compositor's scroll-direction convention
(task #388, 2026-05-12).

Rust renderer's transition_sp_quad_vbo binds NDC-y-up UVs and
required a sign flip (qarl-bug 2026-05-12 commit 3d6f43e). The Python
shader compositor uses a DIFFERENT UV convention: its vertex shader
Y-flips at vertex stage, so v_uv.y=0 is at screen TOP and v_uv.y=1
at screen BOTTOM (matches PIL image convention).

Under that y-down convention, the existing `step(seam, v_uv.y)` math
in _FRAGMENT_SCROLL correctly places B at the screen bottom -- scroll
up, the intended direction.

This test pins the contract: a future contributor flipping ONE of
(vertex shader, scroll math) without the OTHER would silently
invert the direction. By asserting both shapes together this test
fails loudly on either drift.
"""

from __future__ import annotations

from pathlib import Path

_SHADER_FILE = (
    Path(__file__).resolve().parent.parent
    / "openmarquee" / "rendering" / "shader_compositor.py"
)
_SRC = _SHADER_FILE.read_text()


def test_vertex_shader_y_flips_uv():
    """The vertex shader's Y-flip is what makes v_uv.y=0 land at the
    screen TOP. Without it the Y axis would be NDC-y-up (bottom=0)
    and the scroll math would need the Rust-side fix."""
    # Pin the exact expression that produces the flip. `0.5 - a_pos.y
    # * 0.5` maps a_pos.y=-1 (screen bottom in NDC) to v_uv.y=1, and
    # a_pos.y=+1 (top) to v_uv.y=0.
    assert "0.5 - a_pos.y * 0.5" in _SRC, (
        "vertex shader Y-flip removed; the scroll convention would "
        "invert. See _FRAGMENT_SCROLL comment for the contract."
    )


def test_scroll_shader_step_direction():
    """Under y-down v_uv (top=0, bottom=1), `step(seam, v_uv.y)`
    returns 1 when v_uv.y >= seam = 1-t. That's the BOTTOM region
    of the screen -- which is where B (new content) correctly
    appears for scroll-up.

    If a contributor changes this to `step(v_uv.y, t)` (the Rust-side
    fix shape) WITHOUT also flipping the vertex shader, the direction
    inverts.
    """
    assert "step(seam, v_uv.y)" in _SRC, (
        "_FRAGMENT_SCROLL step direction changed; only safe to flip "
        "if the vertex shader's Y-flip is also dropped (see Rust path "
        "commit 3d6f43e for the symmetric fix)."
    )


def test_scroll_sampling_offsets_match_convention():
    """A samples at v_uv.y + t; B at v_uv.y - seam. Under y-down:
    - At t=0.5, v_uv.y=0 (top): A samples y=0.5 (A's middle), B
      samples y=-0.5 (off-texture, would clamp; onTo=0 so masked out
      anyway).
    - At t=0.5, v_uv.y=1 (bottom): A samples y=1.5 (off-texture,
      masked out; onTo=1 selects B), B samples y=0.5 (B's middle).
    Net: A's top half visible at screen-top (A has translated DOWN
    in image-y terms = UP visually because y-down); B's top half
    visible at screen-bottom (B has translated UP in image-y =
    appearing from below).
    """
    assert "vec2(v_uv.x, v_uv.y + t)" in _SRC, "A sampling offset"
    assert "vec2(v_uv.x, v_uv.y - seam)" in _SRC, "B sampling offset"


def test_convention_pin_comment_present():
    """Inline comment near _FRAGMENT_SCROLL explicitly documents the
    convention + warns against blind symmetric-port from Rust.
    Without this, a future contributor sees the Rust fix and might
    'fix' the Python side identically, inverting it."""
    assert "Convention pin" in _SRC or "convention pin" in _SRC.lower(), (
        "convention pin documentation removed; future contributors "
        "may symmetric-port the Rust scroll fix and invert direction."
    )
    # And the Rust commit reference so a code archaeologist can
    # trace the relationship.
    assert "3d6f43e" in _SRC, "Rust fix commit reference removed"
