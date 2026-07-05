import subprocess
from pathlib import Path
from typing import Protocol


class CommandRunner(Protocol):
    def run(self, args: list[str]) -> None:
        ...


class SubprocessCommandRunner:
    def run(self, args: list[str]) -> None:
        subprocess.run(args, check=True)


def mux_segment(runner: CommandRunner, h264_path: Path) -> Path:
    mp4_path = h264_path.with_suffix(".mp4")
    runner.run(["ffmpeg", "-y", "-i", str(h264_path), "-c", "copy", str(mp4_path)])
    return mp4_path
