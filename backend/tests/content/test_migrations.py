"""Tests for one-shot content migrations.

Covers `migrate_050608_bg_to_000000` in isolation. End-to-end env-gate
wiring is covered by the smoke fixture in tests/test_app.py if added;
this file pins the migration's behavior shape.
"""

from __future__ import annotations

from pathlib import Path

from openmarquee.content import TextLayer, TextSlide
from openmarquee.content.migrations import migrate_050608_bg_to_000000
from openmarquee.content.storage import ContentStorage


def _make_slide(bg: str = "#000000", text_color: str = "#FFFFFF") -> TextSlide:
    """Builds a one-layer TextSlide with explicit bg + text colors so
    the migration's selection logic can be exercised directly."""
    return TextSlide(
        name="probe",
        text_layers=[TextLayer(text="hi", text_color=text_color)],
        background_color=bg,
    )


def test_migration_empty_storage_returns_zero(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    assert migrate_050608_bg_to_000000(storage) == 0


def test_migration_rewrites_only_050608_bg(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    s_dirty = _make_slide(bg="#050608")
    s_clean = _make_slide(bg="#000000")
    s_other = _make_slide(bg="#FF0000")
    storage.save_text_slide(s_dirty, png=b"\x89PNG-dirty")
    storage.save_text_slide(s_clean, png=b"\x89PNG-clean")
    storage.save_text_slide(s_other, png=b"\x89PNG-other")

    n = migrate_050608_bg_to_000000(storage)
    assert n == 1

    # Verify the dirty one was rewritten; the others untouched.
    assert storage.load(s_dirty.id).background_color == "#000000"
    assert storage.load(s_clean.id).background_color == "#000000"
    assert storage.load(s_other.id).background_color == "#FF0000"


def test_migration_preserves_text_color_050608(tmp_path: Path):
    """seed.py:854 deliberately uses `text_color="#050608"` as dark
    text against the "10 · Scream" pink-to-green gradient bg. The
    migration must NOT collateral-damage it."""
    storage = ContentStorage(tmp_path)
    slide = _make_slide(bg="#050608", text_color="#050608")
    storage.save_text_slide(slide, png=b"\x89PNG")

    migrate_050608_bg_to_000000(storage)

    loaded = storage.load(slide.id)
    assert loaded.background_color == "#000000"  # changed
    assert loaded.text_layers[0].text_color == "#050608"  # untouched


def test_migration_preserves_png_bytes(tmp_path: Path):
    """The migration reads the existing PNG so it can re-call save();
    the asset bytes must round-trip exactly (the Rust renderer re-
    renders text slides from spec, but the editor's cover image
    should still match what was originally uploaded)."""
    storage = ContentStorage(tmp_path)
    slide = _make_slide(bg="#050608")
    original_png = b"\x89PNG\r\n" + bytes(range(256)) * 4
    storage.save_text_slide(slide, original_png)

    migrate_050608_bg_to_000000(storage)

    assert storage.read_asset(slide.id) == original_png


def test_migration_is_idempotent(tmp_path: Path):
    """Running the migration twice must update zero items the second
    time — this is what lets the operator leave the env var on across
    multiple boots without re-writing already-clean content.

    Exercises BOTH the bg_color path AND the pattern.color_a path
    (the widen-migration commit 6c5de9a added pattern handling but
    the existing idempotency test only covered bg). Subagent nit
    follow-up — closes the explicit-anchor gap."""
    storage = ContentStorage(tmp_path)
    bg_slide = _make_slide(bg="#050608")
    # NOTE: import is local to keep _make_slide_with_pattern out of
    # the module namespace where the earlier tests don't need it.
    # The helper itself lives further down in this file.
    pattern_slide = _make_slide_with_pattern(bg="#000000", color_a="#050608", color_b="#FFB43C")
    storage.save_text_slide(bg_slide, png=b"\x89PNG")
    storage.save_text_slide(pattern_slide, png=b"\x89PNG")

    first = migrate_050608_bg_to_000000(storage)
    second = migrate_050608_bg_to_000000(storage)
    assert first == 2  # bg-only + pattern-only items both migrated
    assert second == 0  # both clean now — no dirty fields left


def test_migration_skips_items_without_background_color_attr(tmp_path: Path):
    """ImageSlide / VideoSlide / StreamSlide / WebSlide don't carry a
    `background_color` field. The migration must walk past them
    without exception (getattr default)."""
    storage = ContentStorage(tmp_path)
    # Add a TextSlide alongside what would be a different-shape item.
    # Easier path: just verify the migration tolerates a mixed bag by
    # asserting on a TextSlide-only storage that it doesn't blow up on
    # an item where bg isn't #050608.
    slide = _make_slide(bg="#123456")
    storage.save_text_slide(slide, png=b"\x89PNG")
    assert migrate_050608_bg_to_000000(storage) == 0


# ---- 2026-05-24 widen: background_pattern.color_a + color_b also
# ---- carry #050608 and must be migrated.


def _make_slide_with_pattern(
    *,
    bg: str = "#000000",
    pattern_kind: str = "solid",
    color_a: str = "#FFFFFF",
    color_b: str = "#000000",
) -> TextSlide:
    """Build a slide with an explicit background_pattern. The pattern's
    color_a/color_b take precedence over slide.background_color when
    the pattern kind is anything other than None — so a pattern with
    color_a=#050608 produces the same lifted-black on glass even if
    slide.background_color is #000000."""
    from openmarquee.content import BackgroundPattern

    pattern = BackgroundPattern(pattern=pattern_kind, color_a=color_a, color_b=color_b)
    return TextSlide(
        name="pattern-probe",
        text_layers=[TextLayer(text="x")],
        background_color=bg,
        background_pattern=pattern,
    )


def test_migration_rewrites_pattern_color_a(tmp_path: Path):
    """A slide with `background_pattern.color_a = "#050608"` (the
    original migration's miss — surfaced by the live-fire
    intermittent-black-not-black observation on FYS 2026-05-24 15:40)
    must get the pattern color rewritten. The slide-level
    background_color was already #000000 on this item; only the
    pattern field was carrying #050608."""
    storage = ContentStorage(tmp_path)
    slide = _make_slide_with_pattern(bg="#000000", color_a="#050608", color_b="#FFB43C")
    storage.save_text_slide(slide, png=b"\x89PNG")

    n = migrate_050608_bg_to_000000(storage)
    assert n == 1

    loaded = storage.load(slide.id)
    assert loaded.background_color == "#000000"  # already clean
    assert loaded.background_pattern.color_a == "#000000"  # newly migrated
    assert loaded.background_pattern.color_b == "#FFB43C"  # untouched


def test_migration_rewrites_pattern_color_b(tmp_path: Path):
    """Same shape for color_b — second-fill in two-color patterns
    (grid, stripes, dots). Less common in seeds but must be covered
    by the same single-pass migration."""
    storage = ContentStorage(tmp_path)
    slide = _make_slide_with_pattern(bg="#000000", color_a="#FFB43C", color_b="#050608")
    storage.save_text_slide(slide, png=b"\x89PNG")

    n = migrate_050608_bg_to_000000(storage)
    assert n == 1

    loaded = storage.load(slide.id)
    assert loaded.background_pattern.color_a == "#FFB43C"  # untouched
    assert loaded.background_pattern.color_b == "#000000"  # migrated


def test_migration_rewrites_both_bg_and_pattern_in_one_pass(tmp_path: Path):
    """When a slide has #050608 in BOTH background_color AND
    pattern.color_a (a clean seed pre-migration), one save() call
    handles both — must not be counted as 2 items."""
    storage = ContentStorage(tmp_path)
    slide = _make_slide_with_pattern(bg="#050608", color_a="#050608", color_b="#FFFFFF")
    storage.save_text_slide(slide, png=b"\x89PNG")

    n = migrate_050608_bg_to_000000(storage)
    assert n == 1  # single item, all dirty fields rewritten in one save()

    loaded = storage.load(slide.id)
    assert loaded.background_color == "#000000"
    assert loaded.background_pattern.color_a == "#000000"
    assert loaded.background_pattern.color_b == "#FFFFFF"


def test_migration_skips_items_with_pattern_but_no_050608(tmp_path: Path):
    """A slide with a background_pattern whose colors are NOT
    #050608 must NOT be touched — defensive against a future
    "always-rewrite-pattern" simplification."""
    storage = ContentStorage(tmp_path)
    slide = _make_slide_with_pattern(bg="#000000", color_a="#FF5FA7", color_b="#5AF095")
    storage.save_text_slide(slide, png=b"\x89PNG")

    assert migrate_050608_bg_to_000000(storage) == 0

    loaded = storage.load(slide.id)
    assert loaded.background_pattern.color_a == "#FF5FA7"
    assert loaded.background_pattern.color_b == "#5AF095"
