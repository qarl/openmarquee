"""r67 (2026-06-05): backend-side runtime asset verification.

The renderer's COLR dispatch at `renderer/src/hdmi_logic.rs:733` silently
falls back to a `FontMissing` MSDF tile when `noto-color-emoji-colrv1.ttf`
is absent at the runtime path. Operator sees tofu glyphs on emoji slides
and the service keeps reporting healthy. r64 perf telemetry doesn't surface
it. There's no error in the journal. The failure is fully silent.

This module runs at FastAPI lifespan startup, verifies the canonical
runtime font is present + reasonably sized, and logs CRITICAL with a
specific, actionable error message if it isn't. The backend does NOT
refuse to start — that would brick the device on any check bug. Operator
sees the CRITICAL line in journal + sees tofu on screen and the two
together point straight at the fix.

Defense layer atop `scripts/check_runtime_assets.sh`: the bash check
gates the DEPLOY (catches dev-host-side oversights), this Python check
gates RUNTIME observability (catches manual rm, partial-deploy by an
operator who bypassed deploy.sh, post-deploy /opt/openmarquee/ drift,
factory-SD-image bugs).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Canonical runtime path. install.sh + deploy.sh both write fonts to
# /opt/openmarquee/ui/fonts/; the renderer's IPC bootstrap roots the
# font catalog at the same path (see renderer/src/ipc_main.rs:1465).
_DEFAULT_FONTS_DIR = Path("/opt/openmarquee/ui/fonts")
_EMOJI_FONT_FILENAME = "noto-color-emoji-colrv1.ttf"

# 1 MB minimum. The real font is ~4.8 MB after SVG-table strip
# (scripts/download-emoji-font-colrv1.sh:86). 1 MB catches 0-byte
# stubs / truncated downloads without false-positiving a future
# smaller variant.
_MIN_FONT_BYTES = 1_048_576


@dataclass(frozen=True)
class AssetIssue:
    """One missing-or-broken runtime asset. Returned by check so
    tests can assert on shape; not part of any external contract."""

    asset: str
    path: Path
    reason: str
    remedy: str


def _fonts_dir() -> Path:
    """Resolve the runtime font directory.

    Resolution order (most-specific wins):
        1. OPENMARQUEE_UI_FONTS_DIR -- explicit override (test/dev only;
           does NOT redirect the renderer, which hardcodes the path).
        2. OPENMARQUEE_UI_DIR + "/fonts" -- tracks the same env var
           that app._resolve_ui_dir honors, so a relocated /opt/
           openmarquee/ui/ (Docker, custom systemd Environment=) lands
           the check at the right place automatically.
        3. /opt/openmarquee/ui/fonts -- canonical production default.
    """
    override = os.environ.get("OPENMARQUEE_UI_FONTS_DIR")
    if override:
        return Path(override)
    ui_dir = os.environ.get("OPENMARQUEE_UI_DIR")
    if ui_dir:
        return Path(ui_dir) / "fonts"
    return _DEFAULT_FONTS_DIR


def check_runtime_assets(fonts_dir: Path | None = None) -> list[AssetIssue]:
    """Verify required runtime assets exist + are not 0-byte stubs.

    Returns a list of `AssetIssue` for every problem found. Empty
    list means everything is healthy.

    `fonts_dir` overrides the default lookup. None means
    `OPENMARQUEE_UI_FONTS_DIR` env var, falling back to the canonical
    `/opt/openmarquee/ui/fonts`.

    If the parent fonts_dir doesn't exist (dev-laptop case where
    /opt/openmarquee/ was never provisioned), the check returns an
    empty list with an INFO log line — there's no production asset
    to check, so don't false-positive.
    """
    resolved = fonts_dir or _fonts_dir()

    if not resolved.is_dir():
        log.info(
            "runtime asset check skipped: %s does not exist "
            "(dev environment without /opt/openmarquee/ provisioning)",
            resolved,
        )
        return []

    issues: list[AssetIssue] = []
    emoji_font = resolved / _EMOJI_FONT_FILENAME

    if not emoji_font.is_file():
        issues.append(
            AssetIssue(
                asset="COLRv1 emoji font",
                path=emoji_font,
                reason="file missing",
                remedy=(
                    "run scripts/download-emoji-font-colrv1.sh on the dev host, "
                    "then re-deploy"
                ),
            )
        )
    else:
        size = emoji_font.stat().st_size
        if size < _MIN_FONT_BYTES:
            issues.append(
                AssetIssue(
                    asset="COLRv1 emoji font",
                    path=emoji_font,
                    reason=f"file only {size} bytes (< {_MIN_FONT_BYTES} minimum); "
                    "likely a 0-byte stub or truncated download",
                    remedy=(
                        "run scripts/download-emoji-font-colrv1.sh on the dev host, "
                        "then re-deploy"
                    ),
                )
            )

    return issues


def log_runtime_asset_issues(issues: list[AssetIssue]) -> None:
    """Emit a CRITICAL log line per issue, with a specific, actionable
    remedy. Called from app.py:lifespan startup. Does NOT raise — the
    backend keeps running so operators see tofu + the journal line
    pointing at the fix, rather than a refusing-to-start service that
    masks the diagnostic."""
    for issue in issues:
        log.critical(
            "runtime asset missing/broken: %s at %s — %s. "
            "Emoji slides will render as tofu (FontMissing -> MSDF fallback). "
            "Fix: %s",
            issue.asset,
            issue.path,
            issue.reason,
            issue.remedy,
        )
