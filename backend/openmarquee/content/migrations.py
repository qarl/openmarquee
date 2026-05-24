"""One-shot, env-gated content migrations.

These are NOT schema migrations (those live in `content/__init__.py`'s
model_validator(mode="before") shims). They are operator-driven
content rewrites that need to be opted into via env var, run once on
the next boot, then dropped from the operator's deploy config.

Each migration is idempotent: running it twice in a row updates zero
items the second time. The lifespan caller checks the env var on
every boot; the operator keeps the env var until they've confirmed
the content is in the desired state, then removes it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openmarquee.content.storage import ContentStorage

log = logging.getLogger(__name__)


def migrate_050608_bg_to_000000(storage: ContentStorage) -> int:
    """One-shot: rewrite text-slide `#050608` near-black to `#000000`
    in any of the bg-color carrying fields:

    - `slide.background_color` (the slide-level fallback when no
      pattern is set)
    - `slide.background_pattern.color_a` (the pattern's primary
      color — used for solid, stripes, dots, etc. when pattern is
      set; takes precedence over slide.background_color)
    - `slide.background_pattern.color_b` (the pattern's secondary
      color — used for the second fill in two-color patterns)

    The seed defaults pre-2026-05-24 baked `#050608` (a near-black)
    as the demo-content background AND as several pattern color_a
    values. That value pre-dates the Bug 7 Broadcast-RGB-Full fix
    (commit 69f7546) — at the time `#050608` and `#000000` rendered
    identically on a Limited-range HDMI signal, so the off-by-three
    was invisible. Post-Bug-7, full-range RGB now correctly
    displays `#050608` as visibly-lifted `(5, 6, 8)`, making the
    seed defaults look "not quite black" on glass.

    The original migration (bbd64c5, 2026-05-24) only touched
    `background_color`. Live-fire observation later that day showed
    the lift was INTERMITTENT — some slides true-black, some lifted
    — because the migration missed `background_pattern.color_a`.
    When the playlist hit a pattern-using slide, the operator saw
    a lifted background; pattern-less slides showed true black. The
    intermittent rotation matched playlist slide-cadence. This
    expanded migration closes the gap.

    The migration deliberately does NOT touch text-color hex
    values — seed.py line 854 uses `#050608` as dark text against
    the "10 · Scream" pink-to-green gradient bg, and operator-
    chosen text colors are not the renderer fix's target.

    Idempotent: items with NO `#050608` values are skipped (zero
    save() calls).

    Returns the count of items updated.
    """
    count = 0
    for item in storage.list_all():
        # Detect any of the three fields holding "#050608" without
        # touching items that have none — saves an unnecessary disk
        # write per clean item.
        bg = getattr(item, "background_color", None)
        pattern = getattr(item, "background_pattern", None)
        pattern_a = getattr(pattern, "color_a", None) if pattern else None
        pattern_b = getattr(pattern, "color_b", None) if pattern else None
        if bg != "#050608" and pattern_a != "#050608" and pattern_b != "#050608":
            continue

        # Build the update dict scoped to just the dirty fields.
        # model_copy(update=...) replaces the named field whole, so
        # to update pattern.color_a without flattening other pattern
        # fields, mutate a copy of the pattern model and pass it as
        # the new background_pattern value.
        updates: dict = {}
        if bg == "#050608":
            updates["background_color"] = "#000000"
        if pattern is not None and (pattern_a == "#050608" or pattern_b == "#050608"):
            pattern_updates: dict = {}
            if pattern_a == "#050608":
                pattern_updates["color_a"] = "#000000"
            if pattern_b == "#050608":
                pattern_updates["color_b"] = "#000000"
            updates["background_pattern"] = pattern.model_copy(update=pattern_updates)

        # storage.save() needs the PNG asset to persist alongside the
        # envelope. Read the existing PNG so the asset bytes are
        # preserved; the Rust renderer re-renders text slides from
        # spec at slide-bind time, so the cached PNG is just an
        # editor-side cover image and doesn't need to be re-baked.
        try:
            png = storage.read_asset(item.id)
        except FileNotFoundError:
            log.warning(
                "migrate_050608_bg: %s has no asset.png; skipping",
                item.id,
            )
            continue
        updated = item.model_copy(update=updates)
        storage.save(updated, png)
        count += 1
        log.info(
            "migrate_050608_bg: rewrote item %s fields=%s",
            item.id,
            sorted(updates.keys()),
        )
    return count
