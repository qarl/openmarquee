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


def migrate_050608_bg_to_000000(storage: "ContentStorage") -> int:
    """One-shot: rewrite text-slide `background_color == "#050608"`
    to `"#000000"`.

    The seed defaults pre-2026-05-24 baked `#050608` (a near-black) as
    the demo-content background. That value pre-dates the Bug 7
    Broadcast-RGB-Full fix (commit 69f7546) — at the time `#050608`
    and `#000000` rendered identically on a Limited-range HDMI signal,
    so the off-by-three was invisible. Post-Bug-7, full-range RGB now
    correctly displays `#050608` as visibly-lifted `(5, 6, 8)`, making
    the seed defaults look "not quite black" on glass.

    The migration touches **only the slide-level `background_color`**.
    Text-color hex values that happen to equal `#050608` are LEFT
    ALONE -- seed.py line 854 deliberately uses `#050608` as
    dark text against the "10 · Scream" pink-to-green gradient bg,
    and operator-chosen text colors are not the renderer fix's
    target.

    Idempotent: items already at `#000000` (or anything other than
    `#050608`) are untouched.

    Returns the count of items updated.
    """
    count = 0
    for item in storage.list_all():
        bg = getattr(item, "background_color", None)
        if bg != "#050608":
            continue
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
        updated = item.model_copy(update={"background_color": "#000000"})
        storage.save(updated, png)
        count += 1
        log.info(
            "migrate_050608_bg: rewrote item %s background_color #050608 -> #000000",
            item.id,
        )
    return count
