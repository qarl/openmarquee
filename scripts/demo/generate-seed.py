#!/usr/bin/env python3
"""Snapshot a running openMarquee backend into the demo's static seed.

Usage:
    ./scripts/demo/generate-seed.py                      # defaults to http://127.0.0.1:9886
    ./scripts/demo/generate-seed.py http://host:port     # point at any backend

What it does:
    - GETs /api/content, /api/playlists, /api/schedules, /api/settings from
      the running backend.
    - Downloads each content item's PNG asset (and MP4 for videos) into
      $OPENMARQUEE_BUILD_DIR/demo/assets/.
    - Dumps a single `seed.json` manifest that the in-browser mock
      backend reads on first load.
    - Bakes in fake flock peers (3 of them) with pre-rendered thumbnails
      so the Flock tab has something live to show without needing real
      peers. Thumbnails reuse seeded content PNGs.

The demo's mock backend serves those GETs from seed.json + disk; every
modification (playlist edits, sync toggles, settings) stays in the
visitor's localStorage and unwinds via the "Reset demo" button.

Re-run after changing the backend's seed to refresh the demo.

Demo-state hardening (option B from QA, 2026-04-26):
The source backend's state is captured verbatim, so a clicked-through
dev backend (welcome dismissed, default playlist edited) used to ship
broken seeds. To make deploys idempotent regardless of source state,
this script enforces these invariants on the snapshot before write:
  * settings.ui_first_run_seen = False (so visitors see the welcome gate)
  * default playlist is non-empty (auto-populates from text slides if
    cleared on the source)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_BACKEND = "http://127.0.0.1:9886"

# Fake peers baked into the demo so the Flock tab isn't empty. Names
# match the device-naming scheme (Sign<XXX>) and their "current
# thumbnail" is a known seeded content id — populated at generation
# time so the references always resolve.
FAKE_PEERS = [
    # The new flock-grid card layout (per 2026-04-28 design handoff)
    # surfaces a stats row with model / mode / signal / uptime per
    # peer. We bake plausible variety here so the demo Flock tab
    # shows the design's intended visual richness rather than four
    # identical "—" placeholders. Real backends populate these via
    # Phase B health probes (TBD); for the demo they're cosmetic.
    {
        "address": "lobby.ts.net",
        "name": "SignA7F",
        "sync": True,
        "model": "Pi Zero 2 W",
        "mode": "hub75-128x64",
        "signal": 92,
        "uptime": "4d 7h",
        # Phase B.3: items_behind surfaces in the sync pill as
        # "K items behind". Lobby is mid-backlog so the demo Flock
        # tab shows the affordance live; cafeteria is off-sync so
        # items_behind is meaningless (UI ignores it for sync=False
        # peers); lab-corner is caught up so the pill stays at
        # "syncing". Three demo cases at a glance.
        "items_behind": 3,
    },
    {
        "address": "cafeteria.ts.net",
        "name": "SignC3D",
        "sync": False,
        "model": "Pi 4",
        "mode": "hdmi-1080",
        "signal": 76,
        "uptime": "12d 2h",
        "items_behind": None,  # sync=False → meaningless, UI hides
    },
    {
        "address": "lab-corner.ts.net",
        "name": "SignB82",
        "sync": True,
        "model": "Pi Zero 2 W",
        "mode": "hub75-64x32",
        "signal": 54,
        "uptime": "2d 18h",
        "items_behind": 0,  # caught up — pill stays at "syncing"
    },
]


def http_get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.read()


def http_get_json(url: str):
    return json.loads(http_get(url))


# Mirrors backend/openmarquee/playlist.py's DEFAULT_PLAYLIST_ID. Identity,
# not name — kept in sync manually since this script doesn't import the
# backend (runs against a remote URL). If this UUID drifts from the
# backend's, the hardening branch silently warns "no default in source"
# and the demo seed lacks a populated default playlist.
_DEFAULT_PLAYLIST_ID = "00000000-0000-4000-8000-000000000001"


def find_default_playlist(playlists: dict) -> dict | None:
    """Return the default playlist dict (or None) from the v4 collection.

    Lookup is by stable UUID, not display name — the seed-time display
    name flipped from "default" → "Welcome" in commit e0a3093, and
    operator-renamed playlists could drift further. UUID is the only
    reliable anchor.
    """
    for pl in playlists.get("playlists", []) or []:
        if str(pl.get("id")) == _DEFAULT_PLAYLIST_ID:
            return pl
    return None


def enforce_demo_invariants(content: list, playlists: dict, settings: dict) -> None:
    """Mutate snapshot in-place to guarantee a working demo regardless of
    source-backend state. Logs each correction to stderr so the operator
    can spot a clicked-through source before it ships."""

    # 1. Welcome gate must fire on first visit.
    if settings.get("ui_first_run_seen"):
        print("  [hardening] forcing settings.ui_first_run_seen = False", file=sys.stderr)
        settings["ui_first_run_seen"] = False

    # 2. Default playlist must be non-empty (otherwise nothing plays in
    #    the preview and the visitor sees the empty-state hint).
    default = find_default_playlist(playlists)
    if default is None:
        print(
            "  [hardening] WARNING: no 'default' playlist in source — demo will fall back",
            file=sys.stderr,
        )
        return
    items = default.get("items") or []
    if items:
        return

    # Order matters — visitors greet "Welcome → to → openMarquee" in
    # English reading order, not "openMarquee → to → Welcome" (which
    # /api/content would yield, since it iterates in alphabetic-by-id
    # order when the default playlist is empty).
    welcome_names = ("Welcome", "to", "openMarquee")
    text_slides = [c for c in content if c.get("type") == "text_slide"]
    by_name = {c.get("name"): c["id"] for c in text_slides}
    if all(name in by_name for name in welcome_names):
        text_ids = [by_name[name] for name in welcome_names]
    else:
        # Fallback: created_at ascending. seed.py emits Welcome → to →
        # openMarquee in that order on a fresh device, so creation
        # order matches the canonical order whenever named-match fails.
        sorted_text = sorted(text_slides, key=lambda c: c.get("created_at", ""))
        text_ids = [c["id"] for c in sorted_text[:3]]
    if not text_ids:
        # No text slides at all — fall back to first 3 of any type,
        # preserving created_at order so the visitor sees the oldest /
        # most-canonical content first.
        sorted_any = sorted(content, key=lambda c: c.get("created_at", ""))
        text_ids = [c["id"] for c in sorted_any[:3]]
    if not text_ids:
        print(
            "  [hardening] WARNING: source content is empty — demo will be blank",
            file=sys.stderr,
        )
        return

    print(
        f"  [hardening] default playlist was empty — populating with {len(text_ids)} item(s)",
        file=sys.stderr,
    )
    default["items"] = [
        {"item_id": i, "transition": "fade", "transition_ms": 500} for i in text_ids
    ]
    default["item_ids"] = text_ids


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backend", nargs="?", default=DEFAULT_BACKEND)
    args = parser.parse_args()

    build_dir = Path(
        os.environ.get("OPENMARQUEE_BUILD_DIR")
        or (Path.home() / "tmp" / "openmarquee-build")
    )
    demo_dir = build_dir / "demo"
    assets_dir = demo_dir / "assets"
    seed_path = demo_dir / "seed.json"

    assets_dir.mkdir(parents=True, exist_ok=True)
    # Clean out stale PNGs/MP4s so a re-run doesn't accumulate orphans.
    for child in assets_dir.iterdir():
        if child.is_file():
            child.unlink()

    print(f"pulling seed from {args.backend}")
    content = http_get_json(f"{args.backend}/api/content")
    playlists = http_get_json(f"{args.backend}/api/playlists")
    schedules = http_get_json(f"{args.backend}/api/schedules")
    settings = http_get_json(f"{args.backend}/api/settings")

    # Override a few settings so visitors see a demo-flavored device name
    # and aren't confused by WiFi credentials / Tailscale keys that are
    # obviously not theirs to fill in.
    settings["sign_name"] = "SignDEMO"
    settings["wifi_ssid"] = "openMarqueeDEMO"
    settings["wifi_password"] = "openmarquee"
    settings["tailscale_enabled"] = False
    settings["tailscale_hostname"] = None
    settings["tailscale_auth_key"] = None

    enforce_demo_invariants(content, playlists, settings)

    # Download each content item's asset bytes. Store them keyed by the
    # content id so the mock backend can serve /api/content/<id>/asset.
    for item in content:
        item_id = item["id"]
        asset_url = f"{args.backend}/api/content/{item_id}/asset"
        asset_bytes = http_get(asset_url)
        (assets_dir / f"{item_id}.png").write_bytes(asset_bytes)
        if item["type"] == "video":
            video_url = f"{args.backend}/api/content/{item_id}/video"
            video_bytes = http_get(video_url)
            (assets_dir / f"{item_id}.mp4").write_bytes(video_bytes)

    # Assign fake peers a plausible "currently playing" content id so
    # their Flock tiles show a live-looking thumbnail. Cycles through
    # the seeded content items.
    peers = []
    for i, peer in enumerate(FAKE_PEERS):
        thumb_id = content[i % len(content)]["id"] if content else None
        peers.append(
            {
                **peer,
                "id": f"00000000-0000-0000-0000-{i:012d}",
                "added_at": "2026-01-01T00:00:00+00:00",
                "last_seen_at": None,
                "current_thumbnail_content_id": thumb_id,
            }
        )

    seed = {
        "schema_version": 1,
        # Stamp every regeneration so the mock-backend can invalidate a
        # visitor's stale localStorage when we roll out a new seed. Without
        # this, reload sees the old state indefinitely since `schema_version`
        # alone doesn't change across seed refreshes.
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "content": content,
        "playlists": playlists,
        "schedules": schedules,
        "settings": settings,
        "flock_peers": peers,
    }
    seed_path.write_text(json.dumps(seed, indent=2))

    print(f"wrote {len(content)} items + {len(peers)} fake peers")
    print(f"seed.json: {seed_path}")
    print(f"assets:    {assets_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
