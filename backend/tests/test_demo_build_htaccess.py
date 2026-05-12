"""Regression guard: scripts/demo/build.sh emits .htaccess into
$DEST/dist/ with the short-cache Cache-Control header (Batch 12.x
followup, www-Jimmy 2026-05-11).

The 2026-05-05 .htaccess that set Cache-Control max-age=300 was
silently dropped by a subsequent refactor of build.sh. The live demo
on DreamHost then served the host's default 30-day cache, so a
sortable/main.js update wouldn't reach operators' browsers for a
month. This test pins the build.sh output contract so the next time
the file is touched, the .htaccess persistence stays load-bearing.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_BUILD = _REPO_ROOT / "scripts" / "demo" / "build.sh"


def _stand_up_fake_src_ui(src_ui: Path) -> None:
    """Build the minimum SRC_UI/dist + sibling files build.sh expects:
    main.js (the prereq check at the top), styles.css, fonts/."""
    (src_ui / "dist").mkdir(parents=True)
    (src_ui / "dist" / "main.js").write_text("// stub entry\n")
    (src_ui / "styles.css").write_text(":root {}\n")
    (src_ui / "fonts").mkdir()
    (src_ui / "fonts" / "Stub.ttf").write_text("not really a ttf\n")


def test_build_emits_htaccess_with_short_cache(tmp_path: Path) -> None:
    """End-to-end: run build.sh against fake SRC_UI + tmp BUILD_DIR;
    assert .htaccess lands in $DEST/dist/ with `max-age=300`. www-Jimmy
    flagged this as the silent regression that re-bit on 2026-05-11."""
    src_ui = tmp_path / "src-ui"
    build_dir = tmp_path / "build"
    _stand_up_fake_src_ui(src_ui)

    env = {
        **os.environ,
        "OPENMARQUEE_BUILD_DIR": str(build_dir),
    }
    subprocess.run(
        ["bash", str(_BUILD), str(src_ui)],
        check=True, capture_output=True, text=True, env=env,
    )

    htaccess = build_dir / "demo" / "dist" / ".htaccess"
    assert htaccess.exists(), (
        ".htaccess missing from build output -- live demo will revert to "
        "DreamHost's 30-day default Cache-Control. Re-land the cat block "
        "in scripts/demo/build.sh after the dotfile + orphan sweeps."
    )
    body = htaccess.read_text()
    assert "max-age=300" in body, (
        f"Cache-Control max-age regressed; body was:\n{body!r}"
    )
    assert "must-revalidate" in body


def test_build_htaccess_survives_dotfile_and_orphan_sweeps(tmp_path: Path) -> None:
    """The .htaccess starts with '.' so the dotfile-deleting sweep
    (find ... -name '.*' -delete) would wipe it if placed earlier.
    The orphan-chunk sweep looks for `*-<HASH>.js` so doesn't touch
    .htaccess, but a future refactor could change that. Pin the
    survival contract: AFTER both sweeps run, .htaccess is still
    present with the cache header."""
    src_ui = tmp_path / "src-ui"
    build_dir = tmp_path / "build"
    _stand_up_fake_src_ui(src_ui)
    # Inject a stale orphan chunk to force the sweep to fire. main.js
    # stub doesn't reference it -> sweep removes it -> we verify
    # .htaccess wasn't collateral damage.
    (src_ui / "dist" / "chunk-DEADBEEF.js").write_text("stale orphan\n")

    env = {**os.environ, "OPENMARQUEE_BUILD_DIR": str(build_dir)}
    result = subprocess.run(
        ["bash", str(_BUILD), str(src_ui)],
        check=True, capture_output=True, text=True, env=env,
    )
    # Orphan sweep should have fired.
    assert "removed=1" in result.stdout or "removed orphan" in result.stdout

    htaccess = build_dir / "demo" / "dist" / ".htaccess"
    assert htaccess.exists()
    assert "max-age=300" in htaccess.read_text()
