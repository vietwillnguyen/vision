from datetime import datetime
from pathlib import Path

from visio_recorder.capture import (
    VideoSettings,
    build_rpicam_command,
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
        "-n",
        "-o",
        "/data/20260709_080530.h264",
    ]


def test_default_video_settings_are_720p30_at_4_mbps():
    assert VideoSettings() == VideoSettings(
        width=1280, height=720, framerate=30, bitrate_bps=4_000_000
    )


def test_record_segment_runs_rpicam_and_returns_the_timestamped_path(tmp_path):
    runner = FakeRunner()
    started_at = datetime(2026, 7, 9, 8, 5, 30)

    path = record_segment(runner, tmp_path, started_at, 60000, VIDEO)

    assert path == tmp_path / "20260709_080530.h264"
    assert runner.calls == [build_rpicam_command(path, 60000, VIDEO)]
