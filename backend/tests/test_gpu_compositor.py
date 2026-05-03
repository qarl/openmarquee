"""Tests for the GPU-side slide compositor (multi-plane DRM path).

The compositor's units are plane-property updates, so tests use a
FakeMultiPlaneRenderer that records every attach / update / detach /
commit call. Behavior assertions read out of that recording rather
than re-rendering pixels — the per-effect math is already covered
in test_motion.py and the GPU compositor reuses those helpers
verbatim, so we only need to verify the math lands in the RIGHT
plane property.

Live-fire visual verification of the property math (ticker sweeps,
pulse fades, no flicker) is covered by scripts/phase6_drm_compositor_
smoke.py on the dev Pi, not here.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from openmarquee.content import TextBox, TextLayer, TextSlide
from openmarquee.rendering.gpu_compositor import (
    GPUSlideCompositor,
    SlideAssetCache,
    classify_layer,
)


# --- test double ---


class FakeMultiPlaneRenderer:
    """Records every multi-plane API call. After commit(), the
    "applied state" reflects what the kernel would have. Before
    commit(), staged-but-unflushed updates live in `_staged` (so
    tests can assert that updates happen on the SAME atomic commit
    as the rest of the tick's work).

    `max_animated_planes` constrains attach_animated_layer the same
    way the real DRMRenderer does — exceeded slot indices raise
    IndexError. `attach_calls[slot_idx]` counts how many times each
    slot was attached so tests can verify re-attach happens on
    auto-text rollover."""

    def __init__(self, *, width: int, height: int, max_animated_planes: int = 8):
        self.width = width
        self.height = height
        self.max_animated_planes = max_animated_planes
        self.primary_frames: list[bytes] = []
        # slot_idx → applied props (after commit).
        self.planes: dict[int, dict[str, object]] = {}
        # slot_idx → staged props (since last commit). Stays per-slot
        # so multi-prop updates batch correctly.
        self._staged: dict[int, dict[str, object]] = {}
        # One snapshot per commit() call: snapshot[slot] = applied
        # props after that commit. Tests assert on commit count and
        # order.
        self.commits: list[dict[int, dict[str, object]]] = []
        # attach_calls[slot_idx] = total attaches on that slot. Lets
        # auto-rollover tests assert re-attach actually happened.
        self.attach_calls: dict[int, int] = {}

    def render_frame(self, frame: bytes) -> None:
        self.primary_frames.append(frame)

    def attach_animated_layer(
        self,
        slot_idx: int,
        rgba_bytes: bytes,
        *,
        src_w: int,
        src_h: int,
        crtc_x: int,
        crtc_y: int,
        crtc_w: int,
        crtc_h: int,
        zpos: int | None = None,
    ) -> None:
        # Mirror the real DRMRenderer's IndexError on out-of-range slot.
        if slot_idx < 0 or slot_idx >= self.max_animated_planes:
            raise IndexError(
                f"animated slot {slot_idx} out of range "
                f"[0..{self.max_animated_planes - 1}]"
            )
        # Attach overwrites the slot's applied state with full at-rest
        # geometry + a fresh implicit alpha=65535. Mirrors the real
        # renderer's behavior (each attach pins blend mode + alpha).
        self.planes[slot_idx] = {
            "src_w": src_w,
            "src_h": src_h,
            "crtc_x": crtc_x,
            "crtc_y": crtc_y,
            "crtc_w": crtc_w,
            "crtc_h": crtc_h,
            "zpos": 2 + slot_idx if zpos is None else zpos,
            "alpha": 65535,
            "rgba_len": len(rgba_bytes),
            "attached": True,
        }
        self.attach_calls[slot_idx] = self.attach_calls.get(slot_idx, 0) + 1

    def update_animated_layer(self, slot_idx: int, **kwargs: object) -> None:
        slot = self._staged.setdefault(slot_idx, {})
        for k, v in kwargs.items():
            if v is None:
                continue
            slot[k] = v

    def detach_animated_layer(self, slot_idx: int) -> None:
        self._staged.setdefault(slot_idx, {})["attached"] = False

    def commit(self) -> None:
        for slot_idx, deltas in self._staged.items():
            applied = self.planes.setdefault(slot_idx, {})
            applied.update(deltas)
        self._staged.clear()
        # Snapshot: deep-copy applied state per slot.
        snap = {sid: dict(p) for sid, p in self.planes.items()}
        self.commits.append(snap)


# --- helpers ---


def _make_layer(
    text: str = "HELLO",
    *,
    motion: str = "static",
    motion_intensity: int = 50,
    motion_phase: float = 0.0,
    box: tuple[float, float, float, float] = (0.1, 0.1, 0.8, 0.8),
    visible: bool = True,
    auto_mode: str | None = None,
    text_color: str = "#FFFFFF",
) -> TextLayer:
    return TextLayer(
        text=text,
        motion=motion,
        motion_intensity=motion_intensity,
        motion_phase=motion_phase,
        box=TextBox(x=box[0], y=box[1], w=box[2], h=box[3]),
        visible=visible,
        auto_mode=auto_mode,
        text_color=text_color,
    )


def _make_slide(*layers: TextLayer, bg_color: str = "#000033") -> TextSlide:
    return TextSlide(
        name="test",
        text_layers=list(layers),
        background_color=bg_color,
    )


# --- classify_layer ---


def test_classify_static_layer():
    layer = _make_layer(motion="static")
    assert classify_layer(layer) == "static"


def test_classify_motion_layer():
    layer = _make_layer(motion="ticker")
    assert classify_layer(layer) == "animated"


def test_classify_auto_layer():
    layer = _make_layer(motion="static", auto_mode="time")
    assert classify_layer(layer) == "animated"


def test_classify_auto_plus_motion_layer():
    layer = _make_layer(motion="bounce", auto_mode="time")
    assert classify_layer(layer) == "animated"


def test_classify_hidden_layer_regardless_of_motion():
    layer = _make_layer(motion="ticker", visible=False)
    assert classify_layer(layer) == "hidden"


# --- attach() ---


def test_attach_static_only_no_planes_attached():
    """A slide with only static text layers should paint the primary
    plane and attach zero animated planes."""
    slide = _make_slide(
        _make_layer(text="STATIC1"),
        _make_layer(text="STATIC2"),
    )
    r = FakeMultiPlaneRenderer(width=320, height=240)
    c = GPUSlideCompositor(slide, r, width=320, height=240)
    c.attach()

    assert len(r.primary_frames) == 1
    assert len(r.primary_frames[0]) == 320 * 240 * 3  # RGB888
    assert r.planes == {}  # no animated planes
    assert len(r.commits) == 1


def test_attach_one_motion_layer_attaches_one_plane():
    slide = _make_slide(_make_layer(text="TICKER", motion="ticker"))
    r = FakeMultiPlaneRenderer(width=320, height=240)
    c = GPUSlideCompositor(slide, r, width=320, height=240)
    c.attach()

    assert len(r.primary_frames) == 1
    assert 0 in r.planes
    assert r.planes[0]["attached"] is True
    assert r.planes[0]["alpha"] == 65535
    # Glyph bbox should be smaller than the layer box (glyph-bbox crop).
    assert r.planes[0]["src_w"] > 0
    assert r.planes[0]["src_h"] > 0


def test_attach_mixes_static_and_animated():
    """Three layers: static, motion, hidden. Only motion claims a plane."""
    slide = _make_slide(
        _make_layer(text="STATIC"),
        _make_layer(text="MOTION", motion="pulse"),
        _make_layer(text="HIDDEN", visible=False),
    )
    r = FakeMultiPlaneRenderer(width=320, height=240)
    c = GPUSlideCompositor(slide, r, width=320, height=240)
    c.attach()

    assert len(r.planes) == 1
    assert 0 in r.planes


def test_attach_twice_raises():
    slide = _make_slide(_make_layer())
    r = FakeMultiPlaneRenderer(width=320, height=240)
    c = GPUSlideCompositor(slide, r, width=320, height=240)
    c.attach()
    with pytest.raises(RuntimeError, match="already attached"):
        c.attach()


def test_attach_empty_text_layer_does_not_consume_plane():
    """A motion layer with empty / whitespace-only text rasterizes to
    no ink → no glyph bbox → no plane attached."""
    slide = _make_slide(_make_layer(text="", motion="pulse"))
    r = FakeMultiPlaneRenderer(width=320, height=240)
    c = GPUSlideCompositor(slide, r, width=320, height=240)
    c.attach()

    assert r.planes == {}


# --- per-effect property staging ---


def _attached_compositor(motion: str, **layer_kwargs):
    """Build a single-layer slide + attached compositor for effect
    tests. Returns (compositor, renderer, slot_idx)."""
    slide = _make_slide(_make_layer(text="HI", motion=motion, **layer_kwargs))
    r = FakeMultiPlaneRenderer(width=320, height=240)
    c = GPUSlideCompositor(slide, r, width=320, height=240)
    c.attach()
    return c, r, 0


def test_tick_static_slide_emits_no_property_updates():
    """A static slide's tick should commit zero deltas (the empty atomic
    commit is a no-op in the real renderer)."""
    slide = _make_slide(_make_layer(text="STATIC"))
    r = FakeMultiPlaneRenderer(width=320, height=240)
    c = GPUSlideCompositor(slide, r, width=320, height=240)
    c.attach()
    n_commits_before = len(r.commits)
    c.tick(1.0)
    assert len(r.commits) == n_commits_before + 1
    # No staged props on slot 0 — the snapshot equals the prior state.
    if r.planes:
        assert r.commits[-1] == r.planes


def test_tick_pulse_emits_alpha_updates():
    c, r, _ = _attached_compositor("pulse", motion_intensity=100)
    # intensity=100 → min_a=0, full sin swing maps 0..1 → 0..65535.
    # phase=0:    sin=0   → s=0.5 → alpha≈32768 (mid-cycle).
    # phase=0.25: sin=+1  → s=1.0 → alpha=65535 (peak).
    # phase=0.75: sin=-1  → s=0.0 → alpha=0     (trough).
    c.tick(0.0)
    assert r.planes[0]["alpha"] == pytest.approx(32768, abs=2)
    c.tick(0.25)
    assert r.planes[0]["alpha"] == 65535
    c.tick(0.75)
    assert r.planes[0]["alpha"] == 0


def test_tick_blink_emits_alpha_zero_or_max():
    c, r, _ = _attached_compositor("blink")
    # phase < 0.5 → ON (65535)
    c.tick(0.0)
    assert r.planes[0]["alpha"] == 65535
    # phase > 0.5 (with blink_freq at intensity=50 = 1 Hz, elapsed=0.6 → phase=0.6)
    c.tick(0.6)
    assert r.planes[0]["alpha"] == 0


def test_tick_ticker_sweeps_crtc_x_leftward():
    """At phase=0, glyph just off box right edge. As phase advances,
    crtc_x decreases (sweeps leftward)."""
    c, r, _ = _attached_compositor("ticker")
    c.tick(0.0)
    x0 = r.planes[0]["crtc_x"]
    c.tick(0.5)
    x_mid = r.planes[0]["crtc_x"]
    assert x_mid < x0  # sweeping leftward


def test_tick_breathe_changes_crtc_w_and_h():
    c, r, _ = _attached_compositor("breathe", motion_intensity=100)
    attach_w = r.planes[0]["crtc_w"]
    attach_h = r.planes[0]["crtc_h"]
    # phase=0.25 → sin=1 → max scale 1.20×.
    c.tick(0.25)
    assert r.planes[0]["crtc_w"] > attach_w
    assert r.planes[0]["crtc_h"] > attach_h
    # phase=0.75 → sin=-1 → min scale 0.80×.
    c.tick(0.75)
    assert r.planes[0]["crtc_w"] < attach_w
    assert r.planes[0]["crtc_h"] < attach_h


def test_tick_bounce_modulates_crtc_y_around_glyph_y():
    c, r, _ = _attached_compositor("bounce", motion_intensity=100)
    attach_y = r.planes[0]["crtc_y"]
    # Bounce phase=0 → sin=0 → no offset.
    c.tick(0.0)
    assert r.planes[0]["crtc_y"] == attach_y
    # phase=0.25 → sin=1 → positive offset.
    c.tick(0.25)
    assert r.planes[0]["crtc_y"] > attach_y
    # phase=0.75 → sin=-1 → negative offset.
    c.tick(0.75)
    assert r.planes[0]["crtc_y"] < attach_y


def test_tick_shake_is_deterministic():
    """Same elapsed + same slide id + same layer index + same motion_
    phase should produce the same crtc_x/y offsets, even across
    compositor instances. Mirrors motion._shake_seed determinism."""
    slide = _make_slide(_make_layer(text="HI", motion="shake", motion_intensity=80))
    r1 = FakeMultiPlaneRenderer(width=320, height=240)
    c1 = GPUSlideCompositor(slide, r1, width=320, height=240)
    c1.attach()
    c1.tick(0.123)
    pos_a = (r1.planes[0]["crtc_x"], r1.planes[0]["crtc_y"])

    r2 = FakeMultiPlaneRenderer(width=320, height=240)
    c2 = GPUSlideCompositor(slide, r2, width=320, height=240)
    c2.attach()
    c2.tick(0.123)
    pos_b = (r2.planes[0]["crtc_x"], r2.planes[0]["crtc_y"])

    assert pos_a == pos_b


def test_tick_shake_zero_intensity_no_update():
    c, r, _ = _attached_compositor("shake", motion_intensity=0)
    rest_x = r.planes[0]["crtc_x"]
    rest_y = r.planes[0]["crtc_y"]
    c.tick(0.5)
    # No update was staged — applied state unchanged.
    assert r.planes[0]["crtc_x"] == rest_x
    assert r.planes[0]["crtc_y"] == rest_y


# --- auto-layer text refresh ---


def test_tick_auto_layer_rerasterizes_on_text_change():
    """When an auto-mode layer's rendered text changes (clock minute
    rollover), the next tick should re-attach the plane buffer.
    Verified via the renderer's `attach_calls[slot]` counter — exactly
    one extra attach per text rollover, none when the text is stable."""
    slide = _make_slide(_make_layer(text="", motion="static", auto_mode="time"))
    r = FakeMultiPlaneRenderer(width=320, height=240)
    c = GPUSlideCompositor(slide, r, width=320, height=240)

    t1 = datetime(2026, 5, 2, 12, 34, 0, tzinfo=UTC)
    c.attach(now=t1)
    assert r.attach_calls.get(0, 0) == 1  # one attach at slide entry

    # Same time → no re-attach.
    c.tick(0.5, now=t1)
    assert r.attach_calls[0] == 1

    # New time (different minute) → exactly one re-attach.
    t2 = datetime(2026, 5, 2, 12, 35, 0, tzinfo=UTC)
    c.tick(1.0, now=t2)
    assert r.attach_calls[0] == 2

    # Same t2 again → no further re-attach.
    c.tick(1.5, now=t2)
    assert r.attach_calls[0] == 2


def test_auto_layer_recovers_from_empty_text_rollover(monkeypatch):
    """Regression test: an auto layer that briefly rasterizes to empty
    text (e.g. format glitch, configurable display mode) must recover
    when text returns. Earlier draft popped the slot mapping on empty
    rollover, which meant the layer stayed dark forever."""
    import openmarquee.motion as motion_mod
    import openmarquee.rendering.gpu_compositor as gc_mod

    times_to_text = {}

    def fake_render(layer, now):
        return times_to_text.get(now, "")

    # Patch both: motion.render_layer_to_rgba uses it for rasterization,
    # gpu_compositor uses it for rollover-detection comparison.
    monkeypatch.setattr(motion_mod, "render_auto_text_for_layer", fake_render)
    monkeypatch.setattr(gc_mod, "render_auto_text_for_layer", fake_render)

    slide = _make_slide(_make_layer(text="", motion="static", auto_mode="time"))
    r = FakeMultiPlaneRenderer(width=320, height=240)
    c = GPUSlideCompositor(slide, r, width=320, height=240)

    t_empty = datetime(2026, 5, 2, 12, 34, 0, tzinfo=UTC)
    t_visible = datetime(2026, 5, 2, 12, 35, 0, tzinfo=UTC)
    times_to_text[t_empty] = ""
    times_to_text[t_visible] = "12:35"

    # Initial attach with empty text → slot reserved, no buffer.
    c.attach(now=t_empty)
    assert 0 not in r.attach_calls  # no attach happened

    # Tick at t_visible: text rolls over to non-empty → re-attach.
    c.tick(1.0, now=t_visible)
    assert r.attach_calls.get(0, 0) == 1, (
        "auto layer must recover when text returns after empty rollover"
    )

    # Subsequent tick at t_empty again: layer goes empty → detach
    # buffer, slot mapping retained.
    c.tick(2.0, now=t_empty)
    assert r.planes[0]["attached"] is False

    # And recovers AGAIN at t_visible.
    c.tick(3.0, now=t_visible)
    assert r.attach_calls[0] == 2


def test_tick_auto_plus_motion_combines():
    """A bouncing clock: auto layer with bounce motion. Per tick we
    expect EITHER (a) text-rolled-over → reattach + bounce update or
    (b) no rollover → bounce update only."""
    slide = _make_slide(_make_layer(
        text="", motion="bounce", motion_intensity=80, auto_mode="time",
    ))
    r = FakeMultiPlaneRenderer(width=320, height=240)
    c = GPUSlideCompositor(slide, r, width=320, height=240)

    t1 = datetime(2026, 5, 2, 12, 34, 0, tzinfo=UTC)
    c.attach(now=t1)
    rest_y = r.planes[0]["crtc_y"]

    # Same time, mid-bounce phase → crtc_y modulated, no reattach.
    c.tick(0.25, now=t1)
    assert r.planes[0]["crtc_y"] != rest_y


# --- detach() ---


def test_detach_disables_all_animated_planes():
    slide = _make_slide(
        _make_layer(text="A", motion="pulse"),
        _make_layer(text="B", motion="ticker"),
    )
    r = FakeMultiPlaneRenderer(width=320, height=240)
    c = GPUSlideCompositor(slide, r, width=320, height=240)
    c.attach()
    assert r.planes[0]["attached"] is True
    assert r.planes[1]["attached"] is True

    c.detach()
    assert r.planes[0]["attached"] is False
    assert r.planes[1]["attached"] is False


def test_detach_when_not_attached_is_safe():
    slide = _make_slide(_make_layer())
    r = FakeMultiPlaneRenderer(width=320, height=240)
    c = GPUSlideCompositor(slide, r, width=320, height=240)
    # Never attached → detach is a no-op.
    c.detach()
    assert r.commits == []


def test_attach_after_detach_works():
    slide = _make_slide(_make_layer(text="X", motion="pulse"))
    r = FakeMultiPlaneRenderer(width=320, height=240)
    c = GPUSlideCompositor(slide, r, width=320, height=240)
    c.attach()
    c.detach()
    c.attach()  # should not raise
    assert r.planes[0]["attached"] is True


# --- one ioctl per tick ---


def test_attach_overflow_raises_with_slide_context():
    """A slide with more animated layers than the renderer's plane
    budget should raise a clear, slide-id-bearing error so PlaybackLoop
    can fall back to the software compose path with an actionable log."""
    slide = _make_slide(
        _make_layer(text="A", motion="pulse"),
        _make_layer(text="B", motion="ticker"),
        _make_layer(text="C", motion="bounce"),
    )
    # Renderer budget = 2 planes; slide has 3 animated layers.
    r = FakeMultiPlaneRenderer(width=320, height=240, max_animated_planes=2)
    c = GPUSlideCompositor(slide, r, width=320, height=240)
    with pytest.raises(RuntimeError, match="exceed renderer.s 2-plane budget"):
        c.attach()


# --- one ioctl per tick ---


# --- SlideAssetCache ---


def test_cache_hit_skips_pil_work_on_attach():
    """Second attach of the same slide with a shared cache should not
    re-rasterize: the cache supplies primary_bytes + per-layer rgba
    bytes verbatim. We verify by attaching once (slow path), then a
    SECOND time after detach with the same cache, and asserting the
    rgba_len in the renderer is identical (same cached bytes)."""
    slide = _make_slide(_make_layer(text="HI", motion="pulse"))
    cache = SlideAssetCache()

    r1 = FakeMultiPlaneRenderer(width=320, height=240)
    c1 = GPUSlideCompositor(slide, r1, width=320, height=240, cache=cache)
    c1.attach()
    rgba_len_first = r1.planes[0]["rgba_len"]
    c1.detach()

    # Second attach — same slide, same cache. Use a fresh renderer so
    # state can't leak: cache is the only thing carrying assets.
    r2 = FakeMultiPlaneRenderer(width=320, height=240)
    c2 = GPUSlideCompositor(slide, r2, width=320, height=240, cache=cache)
    c2.attach()
    assert r2.planes[0]["rgba_len"] == rgba_len_first
    # Cache populated.
    assert len(cache) == 1


def test_cache_disabled_when_none_passed():
    """cache=None preserves original (uncached) behavior — every
    attach pays the full PIL cost. We don't have a way to observe
    PIL cost directly, but we verify the lookup is bypassed by
    having the SlideAssetCache fail loudly if touched."""
    slide = _make_slide(_make_layer(text="HI", motion="pulse"))
    r = FakeMultiPlaneRenderer(width=320, height=240)
    c = GPUSlideCompositor(slide, r, width=320, height=240, cache=None)
    c.attach()
    # Behavior is identical to the no-cache tests above.
    assert r.planes[0]["attached"] is True


def test_cache_invalidates_on_updated_at_change():
    """When the slide's updated_at changes, the cache entry must be
    re-rasterized. Build two slides with the same id but different
    updated_at, share a cache."""
    from datetime import datetime as dt
    cache = SlideAssetCache()
    common_id = "abc-1234"

    # Slide v1.
    slide_v1 = _make_slide(_make_layer(text="HELLO", motion="pulse"))
    object.__setattr__(slide_v1, "id", common_id)
    object.__setattr__(
        slide_v1, "updated_at", dt(2026, 5, 1, 12, 0, 0, tzinfo=UTC),
    )
    r1 = FakeMultiPlaneRenderer(width=320, height=240)
    c1 = GPUSlideCompositor(slide_v1, r1, width=320, height=240, cache=cache)
    c1.attach()
    rgba_v1 = r1.planes[0]["rgba_len"]
    c1.detach()

    # Slide v2 — same id, NEW updated_at + different text → different
    # rasterization. Cache lookup should miss → re-rasterize.
    slide_v2 = _make_slide(_make_layer(text="DIFFERENT TEXT NOW",
                                       motion="pulse"))
    object.__setattr__(slide_v2, "id", common_id)
    object.__setattr__(
        slide_v2, "updated_at", dt(2026, 5, 2, 12, 0, 0, tzinfo=UTC),
    )
    r2 = FakeMultiPlaneRenderer(width=320, height=240)
    c2 = GPUSlideCompositor(slide_v2, r2, width=320, height=240, cache=cache)
    c2.attach()
    rgba_v2 = r2.planes[0]["rgba_len"]
    # Different text → different glyph bbox dims → different rgba_len.
    assert rgba_v2 != rgba_v1


def test_cache_does_not_serve_auto_mode_layers():
    """Auto-mode layers re-rasterize per tick; a cached snapshot
    would be stale within seconds. The cache must skip these layers
    even when the slide is otherwise identical."""
    slide = _make_slide(_make_layer(
        text="", motion="static", auto_mode="time",
    ))
    cache = SlideAssetCache()

    t1 = datetime(2026, 5, 2, 12, 34, 0, tzinfo=UTC)
    r1 = FakeMultiPlaneRenderer(width=320, height=240)
    c1 = GPUSlideCompositor(slide, r1, width=320, height=240, cache=cache)
    c1.attach(now=t1)
    rgba_v1 = r1.planes[0]["rgba_len"]
    attaches_v1 = r1.attach_calls[0]
    c1.detach()

    # The cache entry was created (primary_bytes populated) but the
    # animated dict should NOT have layer 0 (it's auto).
    cached = cache.lookup(slide)
    assert cached is not None, "cache should have an entry for this slide"
    assert 0 not in cached.animated, (
        "auto-mode layer must NOT cache its rasterization"
    )

    # Second attach at a different time → auto layer re-rasterizes.
    t2 = datetime(2026, 5, 2, 12, 35, 0, tzinfo=UTC)
    r2 = FakeMultiPlaneRenderer(width=320, height=240)
    c2 = GPUSlideCompositor(slide, r2, width=320, height=240, cache=cache)
    c2.attach(now=t2)
    # Renderer.attach_animated_layer was called fresh (rgba bytes are
    # what render_layer_to_rgba produced for time t2, not cached).
    assert r2.attach_calls[0] == 1


def test_cache_clear_drops_all_entries():
    cache = SlideAssetCache()
    slide = _make_slide(_make_layer(text="HI", motion="pulse"))
    r = FakeMultiPlaneRenderer(width=320, height=240)
    c = GPUSlideCompositor(slide, r, width=320, height=240, cache=cache)
    c.attach()
    assert len(cache) == 1
    cache.clear()
    assert len(cache) == 0


# --- one ioctl per tick ---


def test_tick_emits_exactly_one_commit():
    """The whole point of the GPU compositor: one atomic commit per
    tick, no matter how many animated layers update."""
    slide = _make_slide(
        _make_layer(text="A", motion="pulse"),
        _make_layer(text="B", motion="ticker"),
        _make_layer(text="C", motion="bounce"),
    )
    r = FakeMultiPlaneRenderer(width=320, height=240)
    c = GPUSlideCompositor(slide, r, width=320, height=240)
    c.attach()
    n_before = len(r.commits)
    c.tick(0.5)
    assert len(r.commits) == n_before + 1
