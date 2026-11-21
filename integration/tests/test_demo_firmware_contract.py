"""The demo seed muxes footage with the firmware's own command.

``pipeline.demo.footage`` cannot import the firmware (the pipeline does not
depend on it), so it repeats ``visio_recorder.muxer.mux_segment``'s two ffmpeg
invocations. This holds the copies equal: if the firmware changes how it wraps
a capture, the demo's "pendant-shaped" footage has to change with it.
"""

from datetime import datetime
from pathlib import Path

import pytest

from pipeline.demo.footage import FIRMWARE_FRAMERATE, flag_marker_name, mux_command
from pipeline.ingestion import parse_flag_marker_filename
from visio_recorder.flag_button import write_flag_marker
from visio_recorder.muxer import mux_segment


class RecordingRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(self, args: list[str]) -> None:
        self.commands.append(args)


@pytest.mark.parametrize("suffix", [".h264", ".ts"])
def test_demo_mux_command_is_the_firmware_mux_command(suffix):
    capture = Path(f"/captures/20261120_090500{suffix}")
    runner = RecordingRunner()

    mp4 = mux_segment(runner, capture, framerate=FIRMWARE_FRAMERATE)

    assert runner.commands == [mux_command(capture, FIRMWARE_FRAMERATE)]
    assert mux_command(capture, FIRMWARE_FRAMERATE)[-1] == str(mp4)


def test_demo_flag_marker_names_match_the_firmware_writer(tmp_path):
    pressed_at = datetime(2026, 11, 20, 9, 5, 20)

    written = write_flag_marker(tmp_path, pressed_at)

    assert written.name == flag_marker_name(pressed_at)
    assert parse_flag_marker_filename(written.name) == pressed_at
