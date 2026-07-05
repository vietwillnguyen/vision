from pathlib import Path

from visio_recorder.muxer import CommandRunner, mux_segment


class FakeCommandRunner(CommandRunner):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> None:
        self.calls.append(args)


def test_mux_segment_returns_mp4_path():
    runner = FakeCommandRunner()
    result = mux_segment(runner, Path("/data/20260704_120000.h264"), framerate=30)
    assert result == Path("/data/20260704_120000.mp4")


def test_mux_segment_invokes_ffmpeg_with_input_framerate_and_copy_codec():
    runner = FakeCommandRunner()
    mux_segment(runner, Path("/data/20260704_120000.h264"), framerate=30)
    assert runner.calls == [
        [
            "ffmpeg", "-y",
            "-framerate", "30",
            "-i", "/data/20260704_120000.h264",
            "-c", "copy",
            "/data/20260704_120000.mp4",
        ]
    ]


def test_mux_segment_threads_through_the_given_framerate():
    runner = FakeCommandRunner()
    mux_segment(runner, Path("/data/20260704_120000.h264"), framerate=25)
    framerate_idx = runner.calls[0].index("-framerate") + 1
    assert runner.calls[0][framerate_idx] == "25"
