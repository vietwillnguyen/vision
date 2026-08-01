from datetime import datetime
from pathlib import Path

from visio_recorder.capture import (
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


def test_build_rpicam_command_records_h264_for_the_given_duration():
    cmd = build_rpicam_command(Path("/data/20260709_080530.h264"), 300000, 30)
    assert cmd == [
        "rpicam-vid",
        "-t",
        "300000",
        "--framerate",
        "30",
        "--codec",
        "h264",
        "-n",
        "-o",
        "/data/20260709_080530.h264",
    ]


def test_record_segment_runs_rpicam_and_returns_the_timestamped_path(tmp_path):
    runner = FakeRunner()
    started_at = datetime(2026, 7, 9, 8, 5, 30)

    path = record_segment(runner, tmp_path, started_at, 300000, 30)

    assert path == tmp_path / "20260709_080530.h264"
    assert runner.calls == [build_rpicam_command(path, 300000, 30)]
