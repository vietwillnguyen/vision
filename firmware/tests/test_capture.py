import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from tests.fakes import FakeCaptureRunner
from visio_recorder.capture import (
    AudioSettings,
    CaptureExit,
    SubprocessInterruptibleRunner,
    VideoSettings,
    build_rpicam_command,
    probe_audio_device,
    record_segment,
    segment_filename,
)


def test_segment_filename_uses_recording_start_time():
    assert segment_filename(datetime(2026, 7, 9, 8, 5, 30)) == "20260709_080530.h264"


def test_segment_filename_stem_matches_the_pipeline_parser_contract():
    # After muxing, the .mp4 keeps this stem; the pipeline's
    # parse_segment_filename hard-requires strptime("%Y%m%d_%H%M%S").
    started_at = datetime(2026, 7, 9, 8, 5, 30)
    stem = segment_filename(started_at).removesuffix(".h264")
    assert datetime.strptime(stem, "%Y%m%d_%H%M%S") == started_at


class FakeRunner:
    def __init__(self):
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> None:
        self.calls.append(args)


VIDEO = VideoSettings(width=1280, height=720, framerate=30, bitrate_bps=4_000_000)


def test_build_rpicam_command_pins_every_setting_that_decides_segment_size():
    # Nothing is left to rpicam-vid's defaults (640x480, and the encoder's own
    # 10 Mbps when --bitrate is absent): size is bitrate x duration, so the
    # bitrate must be the one the segment length was derived from.
    cmd = build_rpicam_command(Path("/data/20260709_080530.h264"), 60000, VIDEO)
    assert cmd == [
        "rpicam-vid",
        "-t",
        "60000",
        "--width",
        "1280",
        "--height",
        "720",
        "--framerate",
        "30",
        "--bitrate",
        "4000000",
        "--codec",
        "h264",
        "-s",
        "-n",
        "-o",
        "/data/20260709_080530.h264",
    ]


def test_default_video_settings_are_720p30_at_4_mbps():
    assert VideoSettings() == VideoSettings(
        width=1280, height=720, framerate=30, bitrate_bps=4_000_000
    )


def test_record_segment_runs_rpicam_and_returns_the_timestamped_path(tmp_path):
    runner = FakeCaptureRunner()
    started_at = datetime(2026, 7, 9, 8, 5, 30)

    path = record_segment(
        runner, tmp_path, started_at, 60000, VIDEO, cancel=threading.Event()
    )

    assert path == tmp_path / "20260709_080530.h264"
    assert runner.calls == [build_rpicam_command(path, 60000, VIDEO)]


AUDIO = AudioSettings(device="visio_mic", bitrate_bps=64_000, samplerate=48_000)


def test_build_rpicam_command_records_audio_and_video_in_one_libav_pass():
    # Flags from rpicam-apps v1.15.0 core/video_options.hpp: --audio-source
    # defaults to "pulse", which Raspberry Pi OS Lite does not run, so ALSA is
    # named explicitly. MPEG-TS, unlike MP4, stays readable when rpicam-vid is
    # killed mid-capture, so an orphaned capture can still be remuxed.
    cmd = build_rpicam_command(
        Path("/data/20260709_080530.ts"), 60000, VIDEO, audio=AUDIO
    )
    assert cmd == [
        "rpicam-vid",
        "-t",
        "60000",
        "--width",
        "1280",
        "--height",
        "720",
        "--framerate",
        "30",
        "--bitrate",
        "4000000",
        "--codec",
        "libav",
        "--libav-format",
        "mpegts",
        "--libav-audio",
        "--audio-source",
        "alsa",
        "--audio-device",
        "visio_mic",
        "--audio-channels",
        "1",
        "--audio-samplerate",
        "48000",
        "--audio-codec",
        "aac",
        "--audio-bitrate",
        "64000",
        "-s",
        "-n",
        "-o",
        "/data/20260709_080530.ts",
    ]


def test_record_segment_with_audio_writes_a_transport_stream(tmp_path):
    runner = FakeCaptureRunner()
    started_at = datetime(2026, 7, 9, 8, 5, 30)

    path = record_segment(
        runner,
        tmp_path,
        started_at,
        60000,
        VIDEO,
        audio=AUDIO,
        cancel=threading.Event(),
    )

    assert path == tmp_path / "20260709_080530.ts"
    assert runner.calls == [build_rpicam_command(path, 60000, VIDEO, audio=AUDIO)]


def test_default_audio_settings_are_mono_48_khz_aac_at_64_kbps_from_visio_mic():
    assert AudioSettings() == AudioSettings(
        device="visio_mic", bitrate_bps=64_000, samplerate=48_000
    )


def test_audio_probe_opens_the_alsa_device_the_way_rpicam_vid_will():
    runner = FakeRunner()

    assert probe_audio_device(runner, AUDIO) is True
    assert runner.calls == [
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "alsa",
            "-channels",
            "1",
            "-sample_rate",
            "48000",
            "-i",
            "visio_mic",
            "-t",
            "0.2",
            "-f",
            "null",
            "-",
        ]
    ]


def test_audio_probe_reports_a_missing_device_as_unavailable():
    class Failing(FakeRunner):
        def run(self, args: list[str]) -> None:
            raise subprocess.CalledProcessError(251, args)

    assert probe_audio_device(Failing(), AUDIO) is False


def test_build_rpicam_command_lets_the_daemon_signal_the_capture():
    # -s/--signal is the whole basis of the interruptible capture: SIGUSR1
    # toggles the output and SIGUSR2 quits, finalising the file. rpicam-vid
    # documents the starting state as recording, so a segment nobody signals
    # behaves exactly as it did before this flag.
    assert "-s" in build_rpicam_command(Path("/data/x.h264"), 60000, VIDEO)


def test_record_segment_returns_the_path_of_a_capture_that_was_cut_short(tmp_path):
    # A cut-short capture is not an error: the caller gets the same path and
    # muxes the shorter segment like any other.
    cancel = threading.Event()
    cancel.set()
    runner = FakeCaptureRunner(duration_sec=30.0)

    path = record_segment(
        runner, tmp_path, datetime(2026, 11, 18, 9, 0, 0), 60000, VIDEO, cancel=cancel
    )

    assert path == tmp_path / "20261118_090000.h264"
    assert runner.cancelled_captures == 1


# The interruptible runner is tested against real child processes, because
# what it has to get right is signal delivery and process teardown - the
# parts a fake would define away. The children stand in for rpicam-vid: each
# runs far longer than the test's patience, so a cancel that did not work
# would be measured as a timeout, not read off a flag.
_POLL_SEC = 0.01
_GRACE_SEC = 0.2
_CHILD_RUNS_FOR_SEC = 60
_CANCEL_BUDGET_SEC = 5.0


def _runner() -> SubprocessInterruptibleRunner:
    return SubprocessInterruptibleRunner(poll_sec=_POLL_SEC, grace_sec=_GRACE_SEC)


def _child(ready_path: Path, body: str) -> list[str]:
    """A child that announces itself, then waits to be signalled.

    The ready file closes the race the real device does not have: a capture
    cancelled before ``rpicam-vid`` installed its handlers would take the
    default action for SIGUSR2 and die instead of finalising its file.
    """
    return [
        sys.executable,
        "-c",
        "import signal, sys, time\n"
        f"{body}\n"
        f"open({str(ready_path)!r}, 'w').close()\n"
        f"time.sleep({_CHILD_RUNS_FOR_SEC})\n",
    ]


def _cancel_once_ready(ready_path: Path, cancel: threading.Event) -> threading.Thread:
    def wait_then_cancel() -> None:
        deadline = time.monotonic() + 10
        while not ready_path.exists() and time.monotonic() < deadline:
            time.sleep(_POLL_SEC)
        cancel.set()

    thread = threading.Thread(target=wait_then_cancel)
    thread.start()
    return thread


def _run_and_cancel(args_body: str, tmp_path: Path) -> tuple[CaptureExit, float]:
    ready = tmp_path / "ready"
    cancel = threading.Event()
    waiter = _cancel_once_ready(ready, cancel)
    started = time.monotonic()
    try:
        capture_exit = _runner().run(_child(ready, args_body), cancel)
    finally:
        waiter.join(timeout=10)
    return capture_exit, time.monotonic() - started


def test_a_capture_nobody_cancels_runs_to_completion():
    assert _runner().run([sys.executable, "-c", "pass"], threading.Event()) == (
        CaptureExit(returncode=0, cancelled=False)
    )


def test_a_capture_that_fails_on_its_own_raises_like_subprocess_check_true():
    # The recording loop's audio fallback is driven by this exception, so a
    # genuine camera failure must keep raising it.
    with pytest.raises(subprocess.CalledProcessError) as failure:
        _runner().run([sys.executable, "-c", "raise SystemExit(3)"], threading.Event())

    assert failure.value.returncode == 3


def test_cancelling_a_capture_quits_it_with_sigusr2(tmp_path):
    capture_exit, elapsed_sec = _run_and_cancel(
        "signal.signal(signal.SIGUSR2, lambda *_: sys.exit(0))", tmp_path
    )

    assert capture_exit == CaptureExit(returncode=0, cancelled=True)
    assert elapsed_sec < _CANCEL_BUDGET_SEC


def test_a_cancelled_capture_exiting_non_zero_is_still_reported_not_raised(tmp_path):
    # BENCH-1: whether rpicam-vid -s exits 0 on SIGUSR2 is unverified on the
    # device. Raising here would cost the segment the cancel just finalised,
    # so the exit code is reported and the caller decides.
    capture_exit, elapsed_sec = _run_and_cancel(
        "signal.signal(signal.SIGUSR2, lambda *_: sys.exit(4))", tmp_path
    )

    assert capture_exit == CaptureExit(returncode=4, cancelled=True)
    assert elapsed_sec < _CANCEL_BUDGET_SEC


def test_a_capture_deaf_to_sigusr2_is_escalated_to_sigterm(tmp_path):
    capture_exit, elapsed_sec = _run_and_cancel(
        "signal.signal(signal.SIGUSR2, signal.SIG_IGN)\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))",
        tmp_path,
    )

    assert capture_exit == CaptureExit(returncode=0, cancelled=True)
    # It took the grace period, which is what proves SIGUSR2 was tried first.
    assert _GRACE_SEC <= elapsed_sec < _CANCEL_BUDGET_SEC


def test_a_capture_deaf_to_every_signal_is_killed(tmp_path, caplog):
    capture_exit, elapsed_sec = _run_and_cancel(
        "signal.signal(signal.SIGUSR2, signal.SIG_IGN)\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
        tmp_path,
    )

    assert capture_exit == CaptureExit(returncode=-9, cancelled=True)
    assert 2 * _GRACE_SEC <= elapsed_sec < _CANCEL_BUDGET_SEC
    # The operator gets to see why a segment ended up truncated.
    assert "SIGUSR2" in caplog.text
    assert "SIGTERM" in caplog.text
