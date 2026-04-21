"""Filesystem persistence for content items.

Layout under `root`:

    <id>/item.json   — metadata envelope (schema-versioned)
    <id>/asset.png   — the rendered asset (PNG for text slides)

Envelope format:

    {
        "schema_version": <int>,
        "item": { ...serialized ContentItem... }
    }

The schema version lives on the envelope (not the model) so we can migrate
the on-disk format without churning the pure-data model. Writes are atomic
(write-to-temp + rename) so a crashed process can't leave a half-written
`item.json` that load() would choke on.
"""

import json
import shutil
from pathlib import Path
from uuid import UUID

from openmarquee.content import ContentItem, TextSlide

# Bump when the on-disk envelope format changes in a non-backward-compatible
# way. load() will refuse to read older versions until a migration is written.
SCHEMA_VERSION = 1

_ENVELOPE_FILENAME = "item.json"
_ASSET_FILENAME = "asset.png"


class ContentStorage:
    """Persists content items as files on disk — one subdirectory per item."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # --- writes ---

    # TODO(image/video): this method is text-slide-specific because the asset
    # filename is hardcoded to `asset.png`. Video will need `asset.mp4`; HUB75
    # raw-frame sequences will need a whole `assets/` subdir per SPEC §3.3.
    # When Image/Video land, refactor to `save(item, asset_bytes, asset_ext)`
    # and dispatch the filename from the item's type.
    def save_text_slide(self, slide: TextSlide, png: bytes) -> None:
        """Persist a text slide and its rendered PNG. Overwrites if the id exists."""
        item_dir = self.root / str(slide.id)
        item_dir.mkdir(parents=True, exist_ok=True)

        envelope = {
            "schema_version": SCHEMA_VERSION,
            "item": slide.model_dump(mode="json"),
        }
        self._atomic_write_text(item_dir / _ENVELOPE_FILENAME, json.dumps(envelope, indent=2))
        self._atomic_write_bytes(item_dir / _ASSET_FILENAME, png)

    # --- reads ---

    def exists(self, item_id: UUID) -> bool:
        """True if a content item with this id has been persisted."""
        return (self.root / str(item_id) / _ENVELOPE_FILENAME).exists()

    def load(self, item_id: UUID) -> ContentItem:
        """Load a content item by id. Raises FileNotFoundError if missing."""
        envelope_path = self.root / str(item_id) / _ENVELOPE_FILENAME
        if not envelope_path.exists():
            raise FileNotFoundError(f"no content item at {envelope_path}")

        data = json.loads(envelope_path.read_text())
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"item {item_id} has schema_version {version}, "
                f"expected {SCHEMA_VERSION} — migration needed"
            )

        # TODO(image/video): dispatch on data["item"]["type"] — or, once
        # ContentItem is a proper discriminated union, use
        # TypeAdapter(ContentItem).validate_python(data["item"]).
        return TextSlide.model_validate(data["item"])

    def read_asset(self, item_id: UUID) -> bytes:
        """Read the rendered asset bytes for a content item."""
        path = self.asset_path(item_id)
        if not path.exists():
            raise FileNotFoundError(f"no asset at {path}")
        return path.read_bytes()

    def asset_path(self, item_id: UUID) -> Path:
        """Return the filesystem path to an item's rendered asset (no IO)."""
        return self.root / str(item_id) / _ASSET_FILENAME

    def list_all(self) -> list[ContentItem]:
        """Return all persisted content items, sorted by id string.

        Resilient to the root being deleted at runtime (e.g. SD card swap,
        manual cleanup) — returns an empty list rather than raising.
        """
        if not self.root.exists():
            return []
        items: list[ContentItem] = []
        for child in sorted(self.root.iterdir()):
            if not child.is_dir():
                continue
            try:
                item_id = UUID(child.name)
            except ValueError:
                continue  # skip non-UUID dirs (could be editor scratch, etc.)
            if not (child / _ENVELOPE_FILENAME).exists():
                continue
            items.append(self.load(item_id))
        return items

    def delete(self, item_id: UUID) -> None:
        """Delete a content item and everything in its directory.

        Uses shutil.rmtree so it's safe when the item grows into a subtree
        (e.g. HUB75 raw-frame sequences store many files under assets/).
        """
        item_dir = self.root / str(item_id)
        if not item_dir.exists():
            raise FileNotFoundError(f"no content item at {item_dir}")
        shutil.rmtree(item_dir)

    # --- internals ---

    @staticmethod
    def _atomic_write_text(path: Path, content: str) -> None:
        # Append ".tmp" to the full name (so "item.json" → "item.json.tmp"),
        # not with_suffix which would replace the last suffix.
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(content)
        tmp.replace(path)

    @staticmethod
    def _atomic_write_bytes(path: Path, content: bytes) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(content)
        tmp.replace(path)
