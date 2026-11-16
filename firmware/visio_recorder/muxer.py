import subprocess
from pathlib import Path
from typing import Protocol


class CommandRunner(Protocol):
    def run(self, args: list[str]) -> None: ...


class SubprocessCommandRunner:
    def run(self, args: list[str]) -> None:
        subprocess.run(args, check=True)


def mux_segment(runner: CommandRunner, h264_path: Path, framerate: int) -> Path:
    """Wrap a capture in MP4 without re-encoding.

    A raw ``.h264`` elementary stream has no timestamps, so ffmpeg is told its
    framerate. A ``.ts`` (audio + video from rpicam-vid's libav backend)
    carries its own timestamps; every stream is copied, and ffmpeg converts
    the AAC track's ADTS framing for MP4 itself.
    """
    mp4_path = h264_path.with_suffix(".mp4")
    if h264_path.suffix == ".ts":
        runner.run(
            ["ffmpeg", "-y", "-i", str(h264_path), "-map", "0", "-c", "copy",
             str(mp4_path)]
        )  # fmt: skip
        return mp4_path
    runner.run(
        [
            "ffmpeg",
            "-y",
            "-framerate",
            str(framerate),
            "-i",
            str(h264_path),
            "-c",
            "copy",
            str(mp4_path),
        ]
    )
    return mp4_path
