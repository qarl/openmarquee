"""Coverage for scripts/demo/sweep_orphan_chunks.sh (Batch 12.x followup).

Catches the same class of leak as the 2026-05-03 ffmpeg-worker tempfile
patch: npm run build produces content-hashed chunk filenames; when the
hash flips, the old hashed file isn't cleaned and rsync ships both to
the live demo. The sweep removes hashed files that no non-hashed entry
file (main.js / login.js / set-password.js / ...) references.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SWEEP = _REPO_ROOT / "scripts" / "demo" / "sweep_orphan_chunks.sh"


def _build_fake_dist(tmp_path: Path) -> Path:
    """Build a fake dist/ layout: main.js references 2 hashed chunks,
    a third hashed chunk is orphaned, plus a non-hashed entry that
    shouldn't be touched."""
    dist = tmp_path / "dist"
    dist.mkdir()

    # main.js references chunk-AAAAAAAA + sortable.esm-BBBBBBBB.
    (dist / "main.js").write_text(
        '// minified entry\n'
        'import("./chunk-AAAAAAAA.js");\n'
        'import("./sortable.esm-BBBBBBBB.js");\n'
    )
    # set-password.js references esm-CCCCCCCC.
    (dist / "set-password.js").write_text(
        'const m = "./esm-CCCCCCCC.js";\n'
    )
    # Three referenced chunks (alive).
    (dist / "chunk-AAAAAAAA.js").write_text("alive A\n")
    (dist / "sortable.esm-BBBBBBBB.js").write_text("alive B\n")
    (dist / "esm-CCCCCCCC.js").write_text("alive C\n")
    # Two orphans (no entry references them).
    (dist / "sortable.esm-DEADBEAD.js").write_text("orphan stale\n")
    (dist / "chunk-OOOOOOOO.js").write_text("orphan stale\n")
    # A non-hashed file with a numeric-looking name -- must NOT match
    # the orphan regex (no `-<8HASH>.js` shape).
    (dist / "ffmpeg-worker.js").write_text("worker code\n")
    return dist


def test_sweep_removes_orphan_hashed_chunks(tmp_path: Path) -> None:
    dist = _build_fake_dist(tmp_path)
    result = subprocess.run(
        ["bash", str(_SWEEP), str(dist)],
        check=True, capture_output=True, text=True,
    )
    # Two orphans removed; three referenced + worker + main + set-password kept.
    surviving = {p.name for p in dist.iterdir()}
    assert "chunk-AAAAAAAA.js" in surviving
    assert "sortable.esm-BBBBBBBB.js" in surviving
    assert "esm-CCCCCCCC.js" in surviving
    assert "ffmpeg-worker.js" in surviving  # non-hashed, immune
    assert "main.js" in surviving
    assert "set-password.js" in surviving
    # Orphans gone.
    assert "sortable.esm-DEADBEAD.js" not in surviving
    assert "chunk-OOOOOOOO.js" not in surviving
    # Summary line correct.
    # `kept` counts only hashed-chunk survivors (3: AAAA, BBBB, CCCC);
    # non-hashed entry files are not included in the sweep's denominator.
    assert "kept=3 removed=2" in result.stdout


def test_sweep_dry_run_does_not_mutate(tmp_path: Path) -> None:
    """--dry-run reports what it WOULD remove without touching the
    filesystem. Same regression-guard discipline as flash-sd.sh and
    install.sh."""
    dist = _build_fake_dist(tmp_path)
    pre = {p.name for p in dist.iterdir()}
    result = subprocess.run(
        ["bash", str(_SWEEP), "--dry-run", str(dist)],
        check=True, capture_output=True, text=True,
    )
    post = {p.name for p in dist.iterdir()}
    assert pre == post, f"dry-run mutated dist: {pre.symmetric_difference(post)}"
    # Report still names the two orphans + kept count.
    assert "DRYRUN: would remove" in result.stdout
    assert "sortable.esm-DEADBEAD.js" in result.stdout
    assert "chunk-OOOOOOOO.js" in result.stdout
    # `kept` counts only hashed-chunk survivors (3: AAAA, BBBB, CCCC);
    # non-hashed entry files are not included in the sweep's denominator.
    assert "kept=3 removed=2" in result.stdout


def test_sweep_no_op_on_clean_dist(tmp_path: Path) -> None:
    """A dist/ with only referenced hashed chunks reports 0 removed."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "main.js").write_text('import("./chunk-AAAAAAAA.js");\n')
    (dist / "chunk-AAAAAAAA.js").write_text("alive\n")
    result = subprocess.run(
        ["bash", str(_SWEEP), str(dist)],
        check=True, capture_output=True, text=True,
    )
    assert "kept=1 removed=0" in result.stdout


def test_sweep_rejects_missing_dist(tmp_path: Path) -> None:
    """Argument validation: nonexistent dist path exits non-zero."""
    result = subprocess.run(
        ["bash", str(_SWEEP), str(tmp_path / "nope")],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "usage" in result.stderr.lower()


def test_sweep_script_is_executable() -> None:
    """systemd-style: executable bit so build.sh can invoke without
    explicit `bash`."""
    assert _SWEEP.exists()
    mode = _SWEEP.stat().st_mode
    assert mode & stat.S_IXUSR, "sweep_orphan_chunks.sh must be +x"


def test_sweep_discovers_entry_files_dynamically(tmp_path: Path) -> None:
    """A future entry file lands in dist/ that isn't in any hardcoded
    list. The sweep MUST treat its references as live -- otherwise it
    deletes a real dependency on the next build. Dynamic-discovery
    contract: any non-hashed .js in dist/ contributes to the
    referenced set."""
    dist = tmp_path / "dist"
    dist.mkdir()
    # A NEW entry file with a name no current entry-file list contains.
    (dist / "admin.js").write_text('import("./chunk-AAAAAAAA.js");\n')
    # main.js doesn't reference AAAAAAAA -- only admin.js does.
    (dist / "main.js").write_text('// no chunk imports here\n')
    (dist / "chunk-AAAAAAAA.js").write_text("alive via admin.js\n")
    (dist / "chunk-BBBBBBBB.js").write_text("real orphan\n")

    subprocess.run(
        ["bash", str(_SWEEP), str(dist)],
        check=True, capture_output=True, text=True,
    )
    surviving = {p.name for p in dist.iterdir()}
    # AAAA must survive because admin.js references it.
    assert "chunk-AAAAAAAA.js" in surviving
    # BBBB is genuinely orphaned.
    assert "chunk-BBBBBBBB.js" not in surviving


def test_sweep_handles_missing_entry_files(tmp_path: Path) -> None:
    """If some entry files don't exist (e.g. spike.js only built in
    some configs), the sweep tolerates the absence and uses the
    entry files that ARE present. Worst case: nothing in dist that
    looks hashed = nothing to sweep."""
    dist = tmp_path / "dist"
    dist.mkdir()
    # Only main.js exists; references chunk-AAAAAAAA.
    (dist / "main.js").write_text('import("./chunk-AAAAAAAA.js");\n')
    (dist / "chunk-AAAAAAAA.js").write_text("alive\n")
    (dist / "chunk-ZZZZZZZZ.js").write_text("orphan\n")
    result = subprocess.run(
        ["bash", str(_SWEEP), str(dist)],
        check=True, capture_output=True, text=True,
    )
    surviving = {p.name for p in dist.iterdir()}
    assert "chunk-AAAAAAAA.js" in surviving
    assert "chunk-ZZZZZZZZ.js" not in surviving
    assert "kept=1 removed=1" in result.stdout
