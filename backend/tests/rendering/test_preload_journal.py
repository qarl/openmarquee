"""Unit tests for the live preload+transition journal analyzer.

The analyzer is the host-portable piece of the 2026-06-13 FYS-regression
coverage extension. It runs in CI so a future refactor that breaks the
parser surfaces here rather than silently failing the live-runner shell
script on a Pi.
"""

from __future__ import annotations

import pytest

from openmarquee.rendering.preload_journal import (
    PreloadJournalSummary,
    assert_production_clean,
    classify,
)

# Sample line fragments matching the real eprintln! shapes in
# hdmi.rs, ipc_main.rs, video_decode.rs. Wrapped in a fake
# journalctl prefix to confirm the substring matchers don't care
# about framing.
_JCTL_PREFIX = "Jun 13 19:30:00 fireplacesign openmarquee-render[1234]: "


def _j(line: str) -> str:
    return _JCTL_PREFIX + line


SAMPLE_NORMAL_HANDOFF = _j(
    "[perf] preload_handoff slide_id=11111111-aaaa-4000-8000-000000000001 "
    "frames_drained=2 prime_only_us=12000 drain_us=45000 budget_ms=500 "
    "was_deferred=false"
)
SAMPLE_DEFERRED_HANDOFF = _j(
    "[perf] preload_handoff slide_id=22222222-aaaa-4000-8000-000000000001 "
    "frames_drained=0 prime_only_us=0 drain_us=0 budget_ms=0 was_deferred=true"
)
SAMPLE_STALL_HANDOFF = _j(
    "[perf] preload_handoff slide_id=33333333-aaaa-4000-8000-000000000001 "
    "frames_drained=0 prime_only_us=55000 drain_us=503000 budget_ms=500 "
    "was_deferred=false"
)
SAMPLE_ENDPOINT_A_SKIP = _j(
    "warn: paint_transition_skip kind=fade progress=0.066 reason=endpoint_a_no_frame"
)
SAMPLE_ENDPOINT_B_SKIP = _j(
    "warn: paint_transition_skip kind=iris progress=0.033 reason=endpoint_b_no_frame"
)
SAMPLE_DEFERRED_PROBE = _j(
    "[perf] preload_deferred_for_codec_contention "
    "slide_id=11111111-bbbb-4000-8000-000000000001 active_decoder_count=1 "
    "active_decoder_ids=[44444444-aaaa-4000-8000-000000000001] deferral_us=234"
)
SAMPLE_BAKE_B_DEADLINE = _j(
    "[perf] bake_b_poll_outcome kind=wipe progress=0.066 iterations=4 "
    "elapsed_us=128000 result=deadline_exhausted deadline_ms=100"
)
SAMPLE_EXPERIMENT_WARN_MAX = _j(
    "WARNING openmarquee.playback: OPENMARQUEE_PRELOAD_MODE='max' is an "
    "EXPERIMENT-ONLY knob (1080p dual-decode investigation surface)..."
)
# 2026-06-14 Option A: the still-coverage skip path.
SAMPLE_DEFER_SKIPPED_STILL = _j(
    "[perf] preload_defer_skipped_for_still_coverage "
    "slide_id=55555555-aaaa-4000-8000-000000000001 "
    "bg_video_id=55555555-bbbb-4000-8000-000000000001 active_decoder_count=1"
)


class TestClassify:
    def test_empty_input_yields_zero_counters(self):
        s = classify([])
        assert s.endpoint_a_no_frame == 0
        assert s.preload_handoff_normal == 0
        assert s.preload_handoff_deferred == 0
        assert s.handoff_drained_counts == []
        assert s.experiment_warning_modes == set()

    def test_normal_handoff_counted_with_drained_value(self):
        s = classify([SAMPLE_NORMAL_HANDOFF])
        assert s.preload_handoff_normal == 1
        assert s.preload_handoff_deferred == 0
        assert s.preload_handoff_frames_drained_zero_normal == 0
        assert s.handoff_drained_counts == [2]

    def test_deferred_handoff_counted_separately(self):
        s = classify([SAMPLE_DEFERRED_HANDOFF])
        assert s.preload_handoff_deferred == 1
        assert s.preload_handoff_normal == 0
        # Deferred handoff with drained=0 is NORMAL (the r97 path
        # documents drained=0 budget_ms=0 for the graceful defer).
        # It must NOT count toward the "codec stalled" counter.
        assert s.preload_handoff_frames_drained_zero_normal == 0

    def test_stall_handoff_flagged_separately(self):
        # ⚠ the 2026-06-13 FYS regression smoking gun: drained=0 +
        # was_deferred=false means the codec was actually feeding but
        # produced zero frames during the preload window.
        s = classify([SAMPLE_STALL_HANDOFF])
        assert s.preload_handoff_normal == 1
        assert s.preload_handoff_frames_drained_zero_normal == 1
        assert s.is_starvation_signature_present()

    def test_endpoint_a_skip_counted_as_starvation(self):
        s = classify([SAMPLE_ENDPOINT_A_SKIP, SAMPLE_NORMAL_HANDOFF])
        assert s.endpoint_a_no_frame == 1
        assert s.is_starvation_signature_present()

    def test_endpoint_b_skip_NOT_counted_as_starvation(self):
        # B-side skip is common at cold-start and is NOT the FYS
        # 2026-06-13 regression signature on its own. The FROM-side
        # bake_a starvation is what blacks the outgoing video.
        s = classify([SAMPLE_ENDPOINT_B_SKIP, SAMPLE_NORMAL_HANDOFF])
        assert s.endpoint_b_no_frame == 1
        assert s.endpoint_a_no_frame == 0
        assert not s.is_starvation_signature_present()

    def test_deferred_for_contention_probe_counted(self):
        s = classify([SAMPLE_DEFERRED_PROBE])
        assert s.deferred_for_codec_contention == 1

    def test_bake_b_deadline_counted(self):
        s = classify([SAMPLE_BAKE_B_DEADLINE])
        assert s.bake_b_deadline_exhausted == 1

    def test_defer_skipped_for_still_coverage_counted(self):
        # 2026-06-14 Option A regression-lock: a binary WITH the fix
        # fires this probe on every video→video transition where a
        # poster exists. A binary WITHOUT the fix (origin/main pre-
        # 2026-06-14) never fires it because the r97 defer arm
        # returns ok_empty unconditionally. The analyzer must
        # distinguish: defer_skipped > 0 ⇒ Option A active; == 0 +
        # bg_is_video runs ⇒ pre-Option A binary or no posters on disk.
        s = classify([SAMPLE_DEFER_SKIPPED_STILL])
        assert s.defer_skipped_for_still_coverage == 1
        assert s.deferred_for_codec_contention == 0  # different path

    def test_experiment_warn_captured_per_mode(self):
        s = classify([SAMPLE_EXPERIMENT_WARN_MAX])
        assert s.experiment_warning_modes == {"max"}

    def test_unrelated_lines_silently_skipped(self):
        # Real journalctl streams carry kernel logs, other unit logs,
        # etc. The parser must not raise on any of them.
        noise = [
            "Jun 13 19:30:00 fireplacesign kernel: tcp: SYN flooding...",
            _j("[mem] v3d_bos_at_phase phase=begin_slide_load_entry v3d_bos=12"),
            _j("ipc: warning -- text slide ABC references bg video XYZ but..."),
            "",
            "garbage that doesn't match anything",
        ]
        s = classify(noise)
        # All counters must remain zero — no false-positive matches.
        assert s.endpoint_a_no_frame == 0
        assert s.preload_handoff_normal == 0
        assert s.preload_handoff_deferred == 0
        assert s.deferred_for_codec_contention == 0
        assert s.bake_b_deadline_exhausted == 0
        assert s.experiment_warning_modes == set()


class TestProductionClean:
    def test_healthy_run_is_production_clean(self):
        # Defer mode + healthy decoder: normal handoffs with drained>=1,
        # zero endpoint_a skips, possibly some endpoint_b skips at
        # cold-start (allowed).
        s = classify(
            [
                SAMPLE_DEFERRED_HANDOFF,
                SAMPLE_NORMAL_HANDOFF,
                SAMPLE_NORMAL_HANDOFF,
                SAMPLE_ENDPOINT_B_SKIP,  # tolerated
            ]
        )
        assert s.production_clean()

    def test_empty_capture_is_NOT_production_clean(self):
        # An empty window means we don't actually know — refuse to
        # green-light it. The shell driver should re-capture if this
        # fires.
        s = classify([])
        assert not s.production_clean()

    def test_starvation_signature_is_NOT_production_clean(self):
        s = classify([SAMPLE_ENDPOINT_A_SKIP, SAMPLE_NORMAL_HANDOFF])
        assert not s.production_clean()

    def test_stall_handoff_alone_is_NOT_production_clean(self):
        s = classify([SAMPLE_STALL_HANDOFF])
        assert not s.production_clean()


class TestAssertProductionClean:
    def test_passes_on_clean_summary(self):
        s = classify(
            [
                SAMPLE_NORMAL_HANDOFF,
                SAMPLE_DEFERRED_HANDOFF,
            ]
        )
        # Should NOT raise.
        assert_production_clean(s)

    def test_raises_with_empty_capture(self):
        with pytest.raises(AssertionError, match="no preload_handoff events seen"):
            assert_production_clean(PreloadJournalSummary())

    def test_raises_on_endpoint_a_starvation(self):
        s = classify([SAMPLE_ENDPOINT_A_SKIP, SAMPLE_NORMAL_HANDOFF])
        with pytest.raises(AssertionError, match="LIVE STARVATION SIGNATURE DETECTED") as exc:
            assert_production_clean(s)
        # The failure message must point at the PRELOAD_MODE config
        # knob so the QA reading the output goes straight to the fix.
        assert "OPENMARQUEE_PRELOAD_MODE" in str(exc.value)
        assert "hardware-ceilings.md" in str(exc.value)

    def test_raises_on_stall_handoff(self):
        s = classify([SAMPLE_STALL_HANDOFF])
        with pytest.raises(AssertionError, match="LIVE STARVATION SIGNATURE DETECTED"):
            assert_production_clean(s)


class TestFysRegressionShape:
    """The actual journal shape from the FYS 2026-06-13 max-mode soak.

    Per QA's reproduction notes: PRELOAD_MODE=max produces 'from-side
    no-frame' counts > 0 across two soaks, vs zero under defer. These
    tests pin the analyzer's classification against the empirical
    shape so a future analyzer refactor can't accidentally rescue the
    bug pattern as 'looks clean.'
    """

    def test_max_mode_soak_signature_is_flagged_as_regression(self):
        # 10 transitions worth: max mode means r97 defer never fires.
        # Every transition shows from-side starvation.
        lines = []
        for _ in range(10):
            lines.append(SAMPLE_NORMAL_HANDOFF)  # preload completes
            lines.append(SAMPLE_ENDPOINT_A_SKIP)  # but transition starves A
            lines.append(SAMPLE_EXPERIMENT_WARN_MAX)  # warn line at startup
        s = classify(lines)
        assert s.endpoint_a_no_frame == 10
        assert s.preload_handoff_normal == 10
        assert s.preload_handoff_deferred == 0  # max bypasses defer
        assert s.is_starvation_signature_present()
        assert not s.production_clean()
        assert s.experiment_warning_modes == {"max"}

    def test_defer_mode_soak_signature_is_clean(self):
        # 10 transitions: r97 defer fires once per transition; normal
        # handoffs happen at slide entry; zero from-side starvation.
        lines = []
        for _ in range(10):
            lines.append(SAMPLE_DEFERRED_HANDOFF)  # r97 defer at preload time
            lines.append(SAMPLE_NORMAL_HANDOFF)  # slide entry primes cleanly
        s = classify(lines)
        assert s.endpoint_a_no_frame == 0
        assert s.preload_handoff_deferred == 10
        assert s.preload_handoff_normal == 10
        assert s.production_clean()
        assert s.experiment_warning_modes == set()
