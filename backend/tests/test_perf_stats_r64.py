"""r64 (2026-06-05): unit tests for openmarquee.perf_stats.

Different namespace from the pre-existing test_perf_stats.py (which
tests the Batch 6.1 /api/system/perf-stats endpoint). r64's
/api/perf-stats endpoint + openmarquee/perf_stats.py module are
distinct surfaces.

Covers:
  * Each record_* method appends to the right buffer + counter.
  * Snapshot reflects pushed state + computes CMA aggregates.
  * Stderr `[perf] ...` line parser routes each kind correctly.
  * Frame-over-budget filter (< 100 ms doesn't append to buffer
    but DOES bump the running counter).
  * /proc/meminfo CMA parser handles missing fields gracefully.
"""

from __future__ import annotations

import pytest

from openmarquee.perf_stats import (
    PerfStats,
    parse_and_record_perf_line,
    read_cma_used_mb,
)


@pytest.fixture
def stats() -> PerfStats:
    """Fresh PerfStats per test so counters don't leak between cases."""
    return PerfStats()


# ----------------------------------------------------------------
# Direct push API
# ----------------------------------------------------------------


def test_record_preload_appends_to_buffer(stats: PerfStats) -> None:
    stats.record_preload("aaaa-bbbb", 1234567)
    snap = stats.snapshot()
    assert len(snap["preload_recent"]) == 1
    entry = snap["preload_recent"][0]
    assert entry["slide_id"] == "aaaa-bbbb"
    assert entry["us"] == 1234567
    assert entry["ts"].endswith("Z")


def test_record_first_frame_paint_with_cache_hit(stats: PerfStats) -> None:
    stats.record_first_frame_paint(
        slide_id="x",
        bake_us=2160525,
        composite_us=0,
        present_us=100,
        scanout_us=33000,
        total_us=2193625,
        cache_hit=True,
    )
    snap = stats.snapshot()
    e = snap["first_frame_paint_recent"][0]
    assert e["cache_hit"] is True
    assert e["bake_us"] == 2160525
    assert e["composite_us"] == 0
    assert e["total_us"] == 2193625


def test_record_frame_observation_under_threshold_buffer_skipped(
    stats: PerfStats,
) -> None:
    """50ms over budget is under the 100ms filter -- counter
    bumps, buffer does NOT receive an entry."""
    stats.record_frame_observation(delta_ms=50, in_transition=False)
    snap = stats.snapshot()
    assert snap["frame_over_budget_total"] == 1
    assert snap["frames_observed_total"] == 1
    assert snap["frame_over_budget_recent"] == []


def test_record_frame_observation_over_threshold_appends(
    stats: PerfStats,
) -> None:
    stats.record_frame_observation(delta_ms=250, in_transition=True)
    snap = stats.snapshot()
    assert snap["frame_over_budget_total"] == 1
    assert len(snap["frame_over_budget_recent"]) == 1
    e = snap["frame_over_budget_recent"][0]
    assert e["delta_ms"] == 250
    assert e["in_transition"] is True


def test_record_frame_observation_in_budget_no_counter_bump(
    stats: PerfStats,
) -> None:
    """delta_ms == 0 means tick was within budget;
    frames_observed bumps but frame_over_budget_total does NOT."""
    stats.record_frame_observation(delta_ms=0, in_transition=False)
    snap = stats.snapshot()
    assert snap["frames_observed_total"] == 1
    assert snap["frame_over_budget_total"] == 0


def test_buffer_bounded_at_max_samples(stats: PerfStats) -> None:
    """Rolling buffer evicts oldest at max length (50)."""
    for i in range(60):
        stats.record_preload(f"slide-{i}", i * 1000)
    snap = stats.snapshot()
    assert len(snap["preload_recent"]) == 50
    # Oldest evicted: first remaining is slide-10.
    assert snap["preload_recent"][0]["slide_id"] == "slide-10"
    assert snap["preload_recent"][-1]["slide_id"] == "slide-59"


def test_cma_samples_compute_aggregates(stats: PerfStats) -> None:
    for v in (200.0, 250.0, 245.0, 230.0):
        stats.record_cma_sample(v)
    snap = stats.snapshot()
    assert snap["cma_used_max_mb_1m"] == 250.0
    assert snap["cma_used_avg_mb_1m"] == pytest.approx(231.25, rel=1e-3)
    assert snap["cma_window_samples"] == 4


def test_cma_no_samples_returns_none(stats: PerfStats) -> None:
    snap = stats.snapshot()
    assert snap["cma_used_max_mb_1m"] is None
    assert snap["cma_used_avg_mb_1m"] is None
    assert snap["cma_window_samples"] == 0


def test_transition_observation_increments_counter(stats: PerfStats) -> None:
    stats.record_transition_observation()
    stats.record_transition_observation()
    snap = stats.snapshot()
    assert snap["transitions_total"] == 2


# ----------------------------------------------------------------
# `[perf]` stderr line parser
# ----------------------------------------------------------------


def test_parse_preload_line(monkeypatch) -> None:
    """parse_and_record_perf_line routes a preload line into STATS.
    Patches the module-level singleton with a fresh instance so we
    don't leak counters into other tests."""
    fresh = PerfStats()
    monkeypatch.setattr("openmarquee.perf_stats.STATS", fresh)
    line = "rust-sidecar stderr: [perf] preload slide_id=abc us=423456"
    handled = parse_and_record_perf_line(line)
    assert handled is True
    snap = fresh.snapshot()
    assert snap["preload_recent"][0]["slide_id"] == "abc"
    assert snap["preload_recent"][0]["us"] == 423456


def test_parse_first_frame_paint_cache_hit(monkeypatch) -> None:
    fresh = PerfStats()
    monkeypatch.setattr("openmarquee.perf_stats.STATS", fresh)
    line = (
        "[perf] first_frame_paint slide_id=ee04 cache_hit=true "
        "bake_us=2160525 composite_us=0 present_us=100 scanout_us=33000 "
        "total_us=2193625"
    )
    assert parse_and_record_perf_line(line) is True
    snap = fresh.snapshot()
    e = snap["first_frame_paint_recent"][0]
    assert e["cache_hit"] is True
    assert e["bake_us"] == 2160525
    assert e["total_us"] == 2193625


def test_parse_frame_over_budget_is_no_op_at_parser_level(monkeypatch) -> None:
    """r64 subagent BLOCKER fix: renderer-side `[perf] frame over
    budget: ...` lines are RECOGNIZED (return True so the caller
    doesn't double-log narrative) but DO NOT push into STATS.
    Backend tick (_record_tick) is the single ingest path to
    avoid double-counting."""
    fresh = PerfStats()
    monkeypatch.setattr("openmarquee.perf_stats.STATS", fresh)
    line = (
        "[perf] frame over budget: delta_ms=9645 in_transition=false "
        "over_budget_total=14 observed_total=20"
    )
    assert parse_and_record_perf_line(line) is True
    snap = fresh.snapshot()
    # Backend tick is the ONLY ingest path; the parser does not
    # bump counters or append to the rolling buffer.
    assert snap["frames_observed_total"] == 0
    assert snap["frame_over_budget_total"] == 0
    assert snap["frame_over_budget_recent"] == []


def test_parse_v4l2_prime_passes_subphases(monkeypatch) -> None:
    fresh = PerfStats()
    monkeypatch.setattr("openmarquee.perf_stats.STATS", fresh)
    line = (
        "[perf] v4l2_prime device_open_us=185 s_fmt_us=35 reqbufs_us=30000 "
        "streamon_us=4400 primer_feed_us=400 warmup_us=1000 total_us=36020 "
        "samples=300 dims=1280x720"
    )
    assert parse_and_record_perf_line(line) is True
    snap = fresh.snapshot()
    e = snap["v4l2_prime_recent"][0]
    assert e["device_open_us"] == 185
    assert e["reqbufs_us"] == 30000
    assert e["dims"] == "1280x720"


def test_parse_non_perf_line_returns_false() -> None:
    """Lines without `[perf]` should pass through (not handled)."""
    assert parse_and_record_perf_line("rust-sidecar stderr: just a log") is False
    assert parse_and_record_perf_line("") is False


def test_parse_unknown_perf_kind_returns_true_no_record(monkeypatch) -> None:
    """A future perf kind not yet wired here is considered handled
    (so the caller doesn't double-log) but produces no STATS push."""
    fresh = PerfStats()
    monkeypatch.setattr("openmarquee.perf_stats.STATS", fresh)
    assert parse_and_record_perf_line("[perf] future_kind x=1 y=2") is True
    snap = fresh.snapshot()
    assert all(
        snap[k] == []
        for k in (
            "preload_recent",
            "first_frame_paint_recent",
            "v4l2_prime_recent",
            "video_load_recent",
            "begin_slide_load_recent",
            "begin_transition_load_recent",
            "frame_over_budget_recent",
        )
    )


# ----------------------------------------------------------------
# /proc/meminfo CMA parser
# ----------------------------------------------------------------


def test_read_cma_used_mb_parses_well_formed(tmp_path, monkeypatch) -> None:
    fixture = tmp_path / "meminfo"
    fixture.write_text(
        "MemTotal:        523456 kB\n"
        "CmaTotal:        262144 kB\n"
        "CmaFree:          12345 kB\n"
        "MemFree:         100000 kB\n"
    )
    monkeypatch.setattr("openmarquee.perf_stats._MEMINFO_PATH", fixture)
    used_mb = read_cma_used_mb()
    # (262144 - 12345) / 1024 = 243.9... MB
    assert used_mb == pytest.approx((262144 - 12345) / 1024.0, rel=1e-3)


def test_read_cma_used_mb_missing_cma_returns_none(tmp_path, monkeypatch) -> None:
    fixture = tmp_path / "meminfo"
    fixture.write_text("MemTotal: 1024 kB\nMemFree: 512 kB\n")
    monkeypatch.setattr("openmarquee.perf_stats._MEMINFO_PATH", fixture)
    assert read_cma_used_mb() is None


def test_read_cma_used_mb_no_file_returns_none(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "openmarquee.perf_stats._MEMINFO_PATH",
        tmp_path / "does-not-exist",
    )
    assert read_cma_used_mb() is None
