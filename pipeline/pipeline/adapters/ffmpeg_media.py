"""FFmpeg-backed MediaProbe and CommandRunner implementations.

Motion analysis avoids a numpy/PIL dependency by having ffmpeg emit small
grayscale rawvideo frames (one byte per pixel) and computing mean absolute
frame-to-frame differences in pure Python; at 1 fps and 160x90 a 5-minute
segment is ~300 frames of 14.4 KB each.
"""

import subprocess
from pathlib import Path

FRAME_WIDTH = 160
FRAME_HEIGHT = 90
FRAME_SIZE = FRAME_WIDTH * FRAME_HEIGHT
SAMPLE_FPS = 1
VISION_FRAME_COUNT = 3


def compute_frame_diffs_from_raw(raw: bytes, frame_size: int) -> list[float]:
    frame_count = len(raw) // frame_size
    diffs = []
    for i in range(1, frame_count):
        prev = raw[(i - 1) * frame_size : i * frame_size]
        curr = raw[i * frame_size : (i + 1) * frame_size]
        total = sum(abs(a - b) for a, b in zip(prev, curr))
        diffs.append(total / frame_size)
    return diffs


def build_raw_gray_command(video_path: Path) -> list[str]:
    return [
        "ffmpeg",
        "-i",
        str(video_path),
        "-vf",
        f"fps={SAMPLE_FPS},scale={FRAME_WIDTH}:{FRAME_HEIGHT}",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-",
    ]


def build_frame_extract_command(video_path: Path, frames_dir: Path) -> list[str]:
    return [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-vf",
        f"fps={VISION_FRAME_COUNT}/300",
        "-frames:v",
        str(VISION_FRAME_COUNT),
        str(frames_dir / "frame_%03d.jpg"),
    ]


def build_audio_extract_command(video_path: Path, audio_path: Path) -> list[str]:
    return [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        str(audio_path),
    ]


class SubprocessRunner:
    def run(self, command: list[str]) -> None:
        subprocess.run(command, check=True, capture_output=True)


class FfmpegMediaProbe:
    def extract_audio(self, video_path: Path, workdir: Path) -> Path:
        audio_path = workdir / (video_path.stem + ".wav")
        subprocess.run(
            build_audio_extract_command(video_path, audio_path),
            check=True,
            capture_output=True,
        )
        return audio_path

    def extract_frame_diffs(self, video_path: Path) -> list[float]:
        completed = subprocess.run(
            build_raw_gray_command(video_path), check=True, capture_output=True
        )
        return compute_frame_diffs_from_raw(completed.stdout, FRAME_SIZE)

    def extract_frames(self, video_path: Path, workdir: Path) -> list[Path]:
        frames_dir = workdir / (video_path.stem + "_frames")
        frames_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            build_frame_extract_command(video_path, frames_dir),
            check=True,
            capture_output=True,
        )
        return sorted(frames_dir.glob("frame_*.jpg"))
