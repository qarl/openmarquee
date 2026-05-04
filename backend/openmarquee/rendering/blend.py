"""Per-layer blend modes for compositing TextLayers onto a background.

The TextLayer schema (content/__init__.py) has carried a `blend` field
since v3.1 (qarl 2026-05-01) with four modes: normal, multiply, screen,
overlay. This module is the render-side wave that gives them effect.

PIL's Image.alpha_composite is source-over only -- equivalent to
TextLayer.blend = "normal". The three non-normal modes need explicit
per-channel math:

  multiply: result = base * top (in [0,1] space). Darker output;
            useful for tinting a bg with a colored overlay or for
            "photo-multiply" looks where the layer's bright areas
            disappear and dark areas tint through.

  screen:   result = 1 - (1-base) * (1-top). Lighter output; the
            inverse of multiply. Useful for additive glow effects
            over a dark bg, or compositing translucent highlights.

  overlay:  result = multiply for dark base regions, screen for light.
            Adds contrast in the bg from the top layer. Common for
            embossed-text-on-photo looks where the layer brightens
            highlights and deepens shadows.

All modes operate per RGB channel. The top layer's alpha channel
modulates the per-pixel mix between blend output and the base, so
a partially-transparent multiply layer shows through more of the
base where it's translucent.

Implementation: numpy vectorized, ~10 ms at 1080p on Pi Zero 2 W.
Per-slide cost paid at slide entry (compose_slide_rgba / gpu_
compositor primary-plane paint); zero per-frame impact for static
blend-mode layers. Animated blend-mode layers drop the slide to the
software (compose_motion_frame) path -- HVS overlay planes can only
do alpha-blend (vc4 PREMULTI), not Photoshop modes.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

# Modes the TextLayer schema currently accepts. Keep in sync with the
# Literal[...] in TextLayer.blend; adding a new mode is (a) extend the
# schema, (b) extend this dispatch + tests.
_BLEND_MODES: frozenset[str] = frozenset({
    "normal",
    "multiply",
    "screen",
    "overlay",
})


def composite_with_blend(
    base: Image.Image,
    top: Image.Image,
    mode: str = "normal",
) -> Image.Image:
    """Composite `top` onto `base` using the given blend mode.

    Returns a new RGBA Image.Image. Inputs must be the same size and
    in RGBA mode -- caller is responsible for that conversion. For
    mode == "normal" this is equivalent to base.alpha_composite(top)
    plus a copy. Other modes apply per-channel Photoshop blend math
    in [0,1] space, then mix with base via the standard source-over
    alpha formula using top's alpha.

    Unknown modes degrade gracefully to "normal" -- callers can
    propagate any future schema mode without breaking renders.
    """
    if mode == "normal" or mode not in _BLEND_MODES:
        out = base.copy()
        out.alpha_composite(top)
        return out

    if base.mode != "RGBA" or top.mode != "RGBA":
        raise ValueError(
            f"composite_with_blend requires RGBA inputs; "
            f"got base={base.mode!r}, top={top.mode!r}"
        )
    if base.size != top.size:
        raise ValueError(
            f"composite_with_blend requires same size; "
            f"got base={base.size}, top={top.size}"
        )

    base_arr = np.asarray(base, dtype=np.float32) / 255.0
    top_arr = np.asarray(top, dtype=np.float32) / 255.0

    base_rgb = base_arr[..., :3]
    top_rgb = top_arr[..., :3]
    base_a = base_arr[..., 3:4]
    top_a = top_arr[..., 3:4]

    if mode == "multiply":
        blend_rgb = base_rgb * top_rgb
    elif mode == "screen":
        blend_rgb = 1.0 - (1.0 - base_rgb) * (1.0 - top_rgb)
    elif mode == "overlay":
        # Photoshop overlay: (base < 0.5) ? 2*base*top : 1 - 2*(1-base)*(1-top).
        dark_blend = 2.0 * base_rgb * top_rgb
        light_blend = 1.0 - 2.0 * (1.0 - base_rgb) * (1.0 - top_rgb)
        blend_rgb = np.where(base_rgb < 0.5, dark_blend, light_blend)
    else:  # pragma: no cover -- guarded above
        raise AssertionError(f"unreachable: unhandled blend mode {mode!r}")

    # Standard source-over alpha + blend-mode-RGB result. The blend's
    # contribution is gated by top's alpha (transparent regions of
    # top still show through to the base unmodified); the blend
    # output gets mixed onto the base proportionally to top_a, then
    # the base contributes its own alpha-weighted RGB underneath.
    result_a = top_a + base_a * (1.0 - top_a)
    # Avoid divide-by-zero where both inputs are fully transparent.
    # When result_a == 0 the numerator is also 0 (both top_a and base_a
    # are 0), so the divided result_rgb becomes 0/1 = 0 -- the right
    # invariant for "fully transparent pixel; RGB undefined, store 0."
    safe_a = np.where(result_a > 0.0, result_a, 1.0)
    result_rgb = (
        blend_rgb * top_a + base_rgb * base_a * (1.0 - top_a)
    ) / safe_a
    result_rgb = np.clip(result_rgb, 0.0, 1.0)

    out_arr = np.empty_like(base_arr)
    out_arr[..., :3] = result_rgb
    out_arr[..., 3:4] = result_a
    out_u8 = np.clip(out_arr * 255.0 + 0.5, 0.0, 255.0).astype(np.uint8)
    return Image.fromarray(out_u8, mode="RGBA")
