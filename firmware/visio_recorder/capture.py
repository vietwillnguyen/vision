from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from visio_recorder.muxer import CommandRunner


@dataclass(frozen=True)
class VideoSettings:
    """What ``rpicam-vid`` is told to record; nothing is left to its defaults.

    rpicam-apps defaults to 640x480 when no size is given, and with no
    ``--bitrate`` the Pi's V4L2 H.264 encoder uses its own 10 Mbps default.
    Segment size is bitrate x duration, so the bitrate is pinned here and the
    segment length is derived from it (see ``visio_recorder.segment_size``).
    The defaults match the design spec's 720p/30fps; 4 Mbps is ample for
    hardware H.264 at 720p30 (about 0.15 bits per pixel per frame).
    """

    width: int = 1280
    height: int = 720
    framerate: int = 30
    bitrate_bps: int = 4_000_000


def segment_filename(started_at: datetime) -> str:
    return f"{started_at.strftime('%Y%m%d_%H%M%S')}.h264"


def build_rpicam_command(
    output_path: Path, duration_ms: int, video: VideoSettings
) -> list[str]:
    return [
        "rpicam-vid",
        "-t",
        str(duration_ms),
        "--width",
        str(video.width),
        "--height",
        str(video.height),
        "--framerate",
        str(video.framerate),
        "--bitrate",
        str(video.bitrate_bps),
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
    video: VideoSettings,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / segment_filename(started_at)
    runner.run(build_rpicam_command(output_path, duration_ms, video))
    return output_path
