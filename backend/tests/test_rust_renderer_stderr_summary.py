"""The sidecar's per-second "[perf] frame over budget" stderr line is
collapsed into a periodic journal summary instead of being relayed
line-by-line (it was ~58% of journal volume on the sign, rotating the
persistent journal in ~3 days). Signal is kept: frames over budget /
observed from the sidecar's cumulative counters, warn-line count, worst
sampled delta_ms, in-transition count, totals; perf_stats still sees
every line.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from openmarquee.rendering import rust_renderer
from openmarquee.rendering.rust_renderer import RustRenderer, _OverBudgetSummary

LOGGER = "openmarquee.rendering.rust_renderer"
HDMI_RS = Path(__file__).resolve().parents[2] / "renderer" / "src" / "hdmi.rs"


def _ob(delta: int, trans: bool, over: int, observed: int) -> str:
    return (
        f"[perf] frame over budget: delta_ms={delta} "
        f"in_transition={'true' if trans else 'false'} "
        f"over_budget_total={over} observed_total={observed}"
    )


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _summaries(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "perf summary" in r.getMessage()]


def test_window_held_then_summarized_with_true_frame_counts(caplog) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    clock = _Clock()
    s = _OverBudgetSummary(window_s=60.0, clock=clock)

    # Sidecar counters: 10 -> 13 over budget, 100 -> 160 observed. Only 4
    # warn lines (rate-limited), but 4 frames over (10..13) of 61 observed.
    assert s.offer(_ob(37, False, 10, 100)) is True
    clock.t += 20
    assert s.offer(_ob(91, True, 11, 120)) is True
    clock.t += 20
    assert s.offer(_ob(40, False, 12, 140)) is True
    assert _summaries(caplog) == []  # window not elapsed yet

    clock.t += 25  # 65s since window opened -> this line rolls it
    assert s.offer(_ob(55, False, 13, 160)) is True

    [msg] = _summaries(caplog)
    assert "4 of 61 frames over budget in 65s" in msg
    assert "4 warn lines" in msg
    assert "worst sampled delta_ms=91" in msg
    assert "1 in transition" in msg
    assert "over_budget=13 observed=160" in msg


def test_frame_count_exceeds_warn_lines_when_rate_limited(caplog) -> None:
    """The Rust side logs <=1 line/s; the counters carry the real rate."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    clock = _Clock()
    s = _OverBudgetSummary(window_s=60.0, clock=clock)
    s.offer(_ob(40, False, 1000, 5000))
    clock.t += 1
    s.offer(_ob(42, False, 1024, 5025))
    s.flush()
    [msg] = _summaries(caplog)
    assert "25 of 26 frames over budget" in msg and "2 warn lines" in msg


def test_burst_then_silence_flushed_by_any_later_stderr_line(caplog) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    clock = _Clock()
    s = _OverBudgetSummary(window_s=60.0, clock=clock)
    s.offer(_ob(70, False, 1, 10))
    clock.t += 3
    s.offer(_ob(80, False, 3, 13))
    clock.t += 300  # smooth play: no over-budget lines for 5 min
    s.tick()  # e.g. a "[perf] begin_slide_load" line arrives
    [msg] = _summaries(caplog)
    assert "3 of 4 frames over budget in 3s" in msg  # span ends at last line


def test_tick_before_window_elapses_does_not_flush(caplog) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    clock = _Clock()
    s = _OverBudgetSummary(window_s=60.0, clock=clock)
    s.offer(_ob(70, False, 1, 10))
    clock.t += 30
    s.tick()
    assert _summaries(caplog) == []


def test_window_restarts_after_roll(caplog) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    clock = _Clock()
    s = _OverBudgetSummary(window_s=60.0, clock=clock)
    s.offer(_ob(30, False, 1, 10))
    clock.t += 60
    s.offer(_ob(31, False, 2, 20))  # rolls window 1
    clock.t += 5
    s.offer(_ob(99, False, 3, 30))  # opens window 2
    s.flush()
    first, second = _summaries(caplog)
    assert "2 of 11 frames" in first and "worst sampled delta_ms=31" in first
    assert "1 of 1 frames" in second and "worst sampled delta_ms=99" in second


def test_non_matching_lines_are_not_consumed() -> None:
    s = _OverBudgetSummary()
    assert s.offer("[mem] v4l2 cma_used=12") is False
    assert s.offer("[perf] begin_slide_load slide_id=x") is False
    assert s.offer("frame over budget: delta_ms=3") is False  # not anchored


def test_flush_with_nothing_pending_logs_nothing(caplog) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    s = _OverBudgetSummary()
    s.tick()
    s.flush()
    assert _summaries(caplog) == []


def test_drainer_summarizes_over_budget_and_relays_everything_else(
    caplog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end through `_drain_stderr`: over-budget lines are NOT
    relayed one-by-one, other lines are, perf_stats still parses every
    line, and drainer exit flushes the partial window (so a sidecar
    death keeps its last stats in the journal)."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    parsed: list[str] = []
    monkeypatch.setattr(
        "openmarquee.perf_stats.parse_and_record_perf_line",
        lambda line: parsed.append(line) or True,
    )
    lines = [
        _ob(37, False, 1, 10),
        "[mem] v4l2 cma_used=12",
        _ob(80, True, 2, 20),
        "[perf] begin_slide_load slide_id=abc",
        _ob(45, False, 3, 30),
    ]
    proc = SimpleNamespace(stderr=io.StringIO("\n".join(lines) + "\n"))

    RustRenderer._drain_stderr(SimpleNamespace(_proc=proc))

    relayed = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("rust-sidecar stderr:")
    ]
    assert relayed == [
        "rust-sidecar stderr: [mem] v4l2 cma_used=12",
        "rust-sidecar stderr: [perf] begin_slide_load slide_id=abc",
    ]
    [msg] = _summaries(caplog)
    assert "3 of 21 frames over budget" in msg and "3 warn lines" in msg
    assert "worst sampled delta_ms=80" in msg and "1 in transition" in msg
    assert "over_budget=3 observed=30" in msg
    assert parsed == lines  # perf_stats sees every line, unchanged


def test_regex_tracks_the_rust_emit_format() -> None:
    """Pin against the sidecar SOURCE (renderer/src/hdmi.rs
    record_present) so a Rust-side rename fails here instead of
    silently re-flooding the journal via the verbatim relay."""
    src = HDMI_RS.read_text()
    fmt = (
        '"[perf] frame over budget: delta_ms={} in_transition={} '
        'over_budget_total={} observed_total={}"'
    )
    assert fmt in src, "hdmi.rs over-budget eprintln! format changed; update _OVER_BUDGET_RE"
    rendered = fmt.strip('"').format(37, "false", 437103, 458857)
    assert rust_renderer._OVER_BUDGET_RE.match(rendered) is not None
