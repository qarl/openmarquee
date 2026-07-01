"""Tests for HdmiAudioHelper — startup probe/sweep + per-clip
lifecycle. All external effects (aplay -L, pgrep, ffmpeg spawn,
killpg) are mocked; the tests verify the helper's ORCHESTRATION,
not the actual audio path.

Design constraint (per qa/hdmi-audio-build-brief-2026-07-01):
    - Audio must NEVER raise into the video advance path.
    - Every spawn/kill wrapped in try/except; exceptions swallowed.
    - Probe missing / no card → helper globally disabled.
    - Sweep + reap always safe on already-dead procs.
"""

from __future__ import annotations

import signal
import subprocess
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch
from uuid import uuid4

from openmarquee.hdmi_audio import HdmiAudioHelper

# --- Startup probe -------------------------------------------------------


_APLAY_L_WITH_VC4HDMI = """\
null
    Discard all samples (playback) or generate zero samples (capture)
default
    Default ALSA Output (currently PulseAudio Sound Server)
sysdefault:CARD=vc4hdmi
    vc4-hdmi, MAI PCM vc4-hdmi-hifi-0
    Default Audio Device
plughw:CARD=vc4hdmi,DEV=0
    vc4-hdmi, MAI PCM vc4-hdmi-hifi-0
    Hardware device with all software conversions
hw:CARD=vc4hdmi,DEV=0
    vc4-hdmi, MAI PCM vc4-hdmi-hifi-0
    Direct hardware device without any conversions
"""

_APLAY_L_NO_VC4HDMI = """\
null
    Discard all samples (playback) or generate zero samples (capture)
default
    Default ALSA Output (currently PulseAudio Sound Server)
"""


def _mock_run_result(stdout: str, returncode: int = 0, stderr: str = "") -> MagicMock:
    m = MagicMock(spec=subprocess.CompletedProcess)
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = returncode
    return m


class TestStartupProbe:
    def test_probe_returns_hdmi_device_when_vc4hdmi_present(self, tmp_path: Path) -> None:
        """PR#22 review 2026-07-01: probe now returns
        `hdmi:CARD=vc4hdmi,DEV=0` (channel-status-setting device),
        NOT the raw `plughw:` string. QA glass-test showed plughw
        caused warble; hdmi: drained a 20s tone cleanly.
        """
        h = HdmiAudioHelper(tmp_path)
        with patch(
            "openmarquee.hdmi_audio.subprocess.run",
            return_value=_mock_run_result(_APLAY_L_WITH_VC4HDMI),
        ):
            h.initialize()
        assert h._device_name == "hdmi:CARD=vc4hdmi,DEV=0"

    def test_probe_returns_none_when_no_vc4hdmi(self, tmp_path: Path) -> None:
        h = HdmiAudioHelper(tmp_path)
        with patch(
            "openmarquee.hdmi_audio.subprocess.run",
            return_value=_mock_run_result(_APLAY_L_NO_VC4HDMI),
        ):
            h.initialize()
        assert h._device_name is None

    def test_probe_returns_none_on_aplay_missing(self, tmp_path: Path) -> None:
        h = HdmiAudioHelper(tmp_path)
        with patch(
            "openmarquee.hdmi_audio.subprocess.run",
            side_effect=FileNotFoundError("aplay"),
        ):
            h.initialize()
        assert h._device_name is None

    def test_probe_returns_none_on_aplay_error(self, tmp_path: Path) -> None:
        h = HdmiAudioHelper(tmp_path)
        with patch(
            "openmarquee.hdmi_audio.subprocess.run",
            return_value=_mock_run_result("", returncode=1, stderr="no cards"),
        ):
            h.initialize()
        assert h._device_name is None

    def test_probe_returns_none_on_timeout(self, tmp_path: Path) -> None:
        h = HdmiAudioHelper(tmp_path)
        with patch(
            "openmarquee.hdmi_audio.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="aplay", timeout=5),
        ):
            h.initialize()
        assert h._device_name is None

    def test_initialize_is_idempotent(self, tmp_path: Path) -> None:
        h = HdmiAudioHelper(tmp_path)
        with patch(
            "openmarquee.hdmi_audio.subprocess.run",
            return_value=_mock_run_result(_APLAY_L_WITH_VC4HDMI),
        ) as run:
            h.initialize()
            h.initialize()  # second call must not re-probe
        # aplay -L + pgrep both call subprocess.run; second initialize
        # should not fire ANY additional subprocess.run.
        first_call_count = run.call_count
        h.initialize()
        assert run.call_count == first_call_count


# --- Startup sweep ------------------------------------------------------


class TestStartupSweep:
    def test_sweep_kills_matching_pgrep_pids(self, tmp_path: Path) -> None:
        h = HdmiAudioHelper(tmp_path)
        # Interleave: aplay -L returns valid, pgrep returns two stray pids
        with (
            patch(
                "openmarquee.hdmi_audio.subprocess.run",
                side_effect=[
                    _mock_run_result("1234\n5678\n", returncode=0),  # pgrep
                    _mock_run_result(_APLAY_L_WITH_VC4HDMI),  # aplay -L
                ],
            ),
            patch("openmarquee.hdmi_audio.os.getpgid", side_effect=[1234, 5678]),
            patch(
                "openmarquee.hdmi_audio.os.killpg",
            ) as killpg,
        ):
            h.initialize()
        assert killpg.call_count == 2
        killpg.assert_any_call(1234, signal.SIGTERM)
        killpg.assert_any_call(5678, signal.SIGTERM)

    def test_sweep_no_op_when_pgrep_no_match(self, tmp_path: Path) -> None:
        h = HdmiAudioHelper(tmp_path)
        with (
            patch(
                "openmarquee.hdmi_audio.subprocess.run",
                side_effect=[
                    _mock_run_result("", returncode=1),  # pgrep no match
                    _mock_run_result(_APLAY_L_WITH_VC4HDMI),  # aplay -L
                ],
            ),
            patch("openmarquee.hdmi_audio.os.killpg") as killpg,
        ):
            h.initialize()
        assert killpg.call_count == 0

    def test_sweep_swallows_killpg_errors(self, tmp_path: Path) -> None:
        """A stray pid that vanishes between pgrep + killpg must not
        raise. This is the failure mode where killpg loses race to a
        process that self-exited."""
        h = HdmiAudioHelper(tmp_path)
        with (
            patch(
                "openmarquee.hdmi_audio.subprocess.run",
                side_effect=[
                    _mock_run_result("9999\n", returncode=0),
                    _mock_run_result(_APLAY_L_WITH_VC4HDMI),
                ],
            ),
            patch("openmarquee.hdmi_audio.os.getpgid", side_effect=ProcessLookupError),
        ):
            # No exception — sweep must be silent on ProcessLookupError.
            h.initialize()
        assert h._device_name == "hdmi:CARD=vc4hdmi,DEV=0"


# --- start_for_slide ----------------------------------------------------


class TestStartForSlide:
    def _initialized(self, tmp_path: Path) -> HdmiAudioHelper:
        h = HdmiAudioHelper(tmp_path)
        with patch(
            "openmarquee.hdmi_audio.subprocess.run",
            return_value=_mock_run_result(_APLAY_L_WITH_VC4HDMI),
        ):
            h.initialize()
        return h

    def test_spawn_when_asset_exists(self, tmp_path: Path) -> None:
        h = self._initialized(tmp_path)
        slide_id = uuid4()
        asset_dir = tmp_path / str(slide_id)
        asset_dir.mkdir()
        (asset_dir / "asset.mp4").write_bytes(b"fake mp4")
        fake_proc = MagicMock(spec=subprocess.Popen)
        fake_proc.pid = 12345
        with patch(
            "openmarquee.hdmi_audio.subprocess.Popen",
            return_value=fake_proc,
        ) as popen:
            h.start_for_slide(slide_id)
        popen.assert_called_once()
        args, kwargs = popen.call_args
        # argv[0] is the sentinel used by the startup sweep
        assert args[0][0] == "openmarquee-hdmi-audio-helper"
        # ffmpeg flags: no stdin, alsa output to the probed device
        assert "-nostdin" in args[0]
        assert "-vn" in args[0]
        assert "-stream_loop" in args[0]
        assert "-f" in args[0]
        assert "alsa" in args[0]
        assert "hdmi:CARD=vc4hdmi,DEV=0" in args[0]
        # Own process group for killpg
        assert kwargs["start_new_session"] is True
        # DEVNULL stdin/stdout/stderr so the ffmpeg doesn't fight
        # our journal
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert h._current is fake_proc

    def test_noop_when_disabled(self, tmp_path: Path) -> None:
        """No vc4hdmi card → all subsequent start_for_slide calls no-op."""
        h = HdmiAudioHelper(tmp_path)
        with patch(
            "openmarquee.hdmi_audio.subprocess.run",
            return_value=_mock_run_result(_APLAY_L_NO_VC4HDMI),
        ):
            h.initialize()
        assert h._device_name is None
        with patch("openmarquee.hdmi_audio.subprocess.Popen") as popen:
            h.start_for_slide(uuid4())
        popen.assert_not_called()

    def test_noop_when_asset_missing(self, tmp_path: Path) -> None:
        h = self._initialized(tmp_path)
        # No asset dir at all — should silently skip
        with patch("openmarquee.hdmi_audio.subprocess.Popen") as popen:
            h.start_for_slide(uuid4())
        popen.assert_not_called()

    def test_swallow_popen_exception(self, tmp_path: Path) -> None:
        """Popen raising must NEVER propagate — fail-safe to silence."""
        h = self._initialized(tmp_path)
        slide_id = uuid4()
        asset_dir = tmp_path / str(slide_id)
        asset_dir.mkdir()
        (asset_dir / "asset.mp4").write_bytes(b"fake mp4")
        with patch(
            "openmarquee.hdmi_audio.subprocess.Popen",
            side_effect=OSError("no ffmpeg"),
        ):
            h.start_for_slide(slide_id)  # must not raise
        assert h._current is None

    def test_kills_prior_before_starting_new(self, tmp_path: Path) -> None:
        """Back-to-back start_for_slide calls stop the prior helper first."""
        h = self._initialized(tmp_path)
        slide1, slide2 = uuid4(), uuid4()
        for sid in (slide1, slide2):
            d = tmp_path / str(sid)
            d.mkdir()
            (d / "asset.mp4").write_bytes(b"fake")
        proc1 = MagicMock(spec=subprocess.Popen)
        proc1.pid = 11111
        proc2 = MagicMock(spec=subprocess.Popen)
        proc2.pid = 22222
        with (
            patch(
                "openmarquee.hdmi_audio.subprocess.Popen",
                side_effect=[proc1, proc2],
            ),
            patch("openmarquee.hdmi_audio.os.getpgid", return_value=11111),
            patch(
                "openmarquee.hdmi_audio.os.killpg",
            ) as killpg,
        ):
            h.start_for_slide(slide1)
            h.start_for_slide(slide2)
        # slide2's start_for_slide should have SIGTERM'd proc1
        killpg.assert_any_call(11111, signal.SIGTERM)
        assert h._current is proc2


# --- stop_current -------------------------------------------------------


class TestStopCurrent:
    def _initialized(self, tmp_path: Path) -> HdmiAudioHelper:
        h = HdmiAudioHelper(tmp_path)
        with patch(
            "openmarquee.hdmi_audio.subprocess.run",
            return_value=_mock_run_result(_APLAY_L_WITH_VC4HDMI),
        ):
            h.initialize()
        return h

    def _spawn(self, h: HdmiAudioHelper, tmp_path: Path, pid: int = 33333) -> MagicMock:
        slide_id = uuid4()
        d = tmp_path / str(slide_id)
        d.mkdir()
        (d / "asset.mp4").write_bytes(b"fake")
        proc = MagicMock(spec=subprocess.Popen)
        proc.pid = pid
        with patch(
            "openmarquee.hdmi_audio.subprocess.Popen",
            return_value=proc,
        ):
            h.start_for_slide(slide_id)
        return proc

    def test_stop_reaps_current(self, tmp_path: Path) -> None:
        h = self._initialized(tmp_path)
        proc = self._spawn(h, tmp_path)
        proc.wait.return_value = 0
        with (
            patch("openmarquee.hdmi_audio.os.getpgid", return_value=proc.pid),
            patch(
                "openmarquee.hdmi_audio.os.killpg",
            ) as killpg,
        ):
            h.stop_current()
        killpg.assert_called_once_with(proc.pid, signal.SIGTERM)
        proc.wait.assert_called_once_with(timeout=ANY)
        assert h._current is None

    def test_stop_is_idempotent(self, tmp_path: Path) -> None:
        h = self._initialized(tmp_path)
        # No spawn — stop should be a silent no-op
        with patch("openmarquee.hdmi_audio.os.killpg") as killpg:
            h.stop_current()
            h.stop_current()
        killpg.assert_not_called()

    def test_stop_swallows_processlookuperror(self, tmp_path: Path) -> None:
        """killpg on a process that just self-exited must not raise."""
        h = self._initialized(tmp_path)
        proc = self._spawn(h, tmp_path)
        proc.wait.return_value = 0
        with patch(
            "openmarquee.hdmi_audio.os.getpgid",
            side_effect=ProcessLookupError,
        ):
            h.stop_current()  # must not raise
        assert h._current is None

    def test_stop_sigkill_escalation_on_wait_timeout(self, tmp_path: Path) -> None:
        h = self._initialized(tmp_path)
        proc = self._spawn(h, tmp_path)
        # First wait times out; SIGKILL fires; second wait succeeds
        proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1.0),
            0,
        ]
        with (
            patch("openmarquee.hdmi_audio.os.getpgid", return_value=proc.pid),
            patch(
                "openmarquee.hdmi_audio.os.killpg",
            ) as killpg,
        ):
            h.stop_current()
        # SIGTERM + SIGKILL both fired
        killpg.assert_any_call(proc.pid, signal.SIGTERM)
        killpg.assert_any_call(proc.pid, signal.SIGKILL)
        assert h._current is None


# --- Non-Linux / no-ALSA robustness ------------------------------------


def test_helper_constructs_without_content_root_error(tmp_path: Path) -> None:
    """content_root doesn't need to exist yet at construction."""
    non_existent = tmp_path / "does-not-exist"
    h = HdmiAudioHelper(non_existent)
    # No probe yet — device_name should still be None.
    assert h._device_name is None
    assert h._current is None


def test_uninitialized_start_for_slide_noops(tmp_path: Path) -> None:
    """start_for_slide before initialize should not spawn (probe never ran)."""
    h = HdmiAudioHelper(tmp_path)
    slide_id = uuid4()
    d = tmp_path / str(slide_id)
    d.mkdir()
    (d / "asset.mp4").write_bytes(b"fake")
    with patch("openmarquee.hdmi_audio.subprocess.Popen") as popen:
        h.start_for_slide(slide_id)  # device_name is None → noop
    popen.assert_not_called()
