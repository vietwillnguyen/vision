from datetime import datetime
from pathlib import Path

from visio_recorder.muxer import CommandRunner


def segment_filename(started_at: datetime) -> str:
    return f"{started_at.strftime('%Y%m%d_%H%M%S')}.h264"


def build_rpicam_command(
    output_path: Path, duration_ms: int, framerate: int
) -> list[str]:
    return [
        "rpicam-vid",
        "-t",
        str(duration_ms),
        "--framerate",
        str(framerate),
        "--codec",
        "h264",
        "-n",
        "-o",
        str(output_path),
    ]


def record_segment(
    runner: CommandRunner,
    output_dir: Path,
    started_at: "datetime",
    duration_ms: int,
    framerate: int,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / segment_filename(started_at)
    runner.run(build_rpicam_command(output_path, duration_ms, framerate))
    return output_path
