"""FastAPI dependency providers.

Centralizes how API routes get their collaborators (right now just
ContentStorage) so tests can override them via app.dependency_overrides
and the production wiring stays in one place.
"""

import os
from functools import lru_cache
from pathlib import Path

from openmarquee.content.storage import ContentStorage


def _resolve_content_root() -> Path:
    """Pick a content root: env var override, then a sensible default.

    On the device the systemd unit will set OPENMARQUEE_CONTENT_ROOT to
    /var/openmarquee/content per SYSTEM_SPEC §3.3. For local dev we fall
    back to a relative ./openmarquee-content/ so running the app from
    anywhere gives a writable directory next to it.
    """
    override = os.environ.get("OPENMARQUEE_CONTENT_ROOT")
    if override:
        return Path(override)
    return Path("openmarquee-content").resolve()


@lru_cache
def _content_storage_singleton() -> ContentStorage:
    return ContentStorage(_resolve_content_root())


def get_content_storage() -> ContentStorage:
    """Dependency provider for the content storage layer."""
    return _content_storage_singleton()
