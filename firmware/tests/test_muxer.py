from pathlib import Path

from visio_recorder.muxer import CommandRunner, mux_segment


class FakeCommandRunner(CommandRunner):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> None:
        self.calls.append(args)


def test_mux_segment_returns_mp4_path():
    runner = FakeCommandRunner()
    result = mux_segment(runner, Path("/data/20260704_120000.h264"))
    assert result == Path("/data/20260704_120000.mp4")


def test_mux_segment_invokes_ffmpeg_with_copy_codec():
    runner = FakeCommandRunner()
    mux_segment(runner, Path("/data/20260704_120000.h264"))
    assert runner.calls == [
        [
            "ffmpeg", "-y",
            "-i", "/data/20260704_120000.h264",
            "-c", "copy",
            "/data/20260704_120000.mp4",
        ]
    ]
