"""FFmpeg-backed MediaProbe and CommandRunner implementations.

Motion analysis avoids a numpy/PIL dependency by having ffmpeg emit small
grayscale rawvideo frames (one byte per pixel) and computing mean absolute
frame-to-frame differences in pure Python; at 1 fps and 160x90 a 5-minute
segment is ~300 frames of 14.4 KB each.

``probe_duration_sec`` is the source of every segment length the pipeline
uses; see ``pipeline.ingestion``.
"""

import subprocess
from pathlib import Path

from pipeline.ingestion import UnreadableSegmentError

FRAME_WIDTH = 160
FRAME_HEIGHT = 90
FRAME_SIZE = FRAME_WIDTH * FRAME_HEIGHT
SAMPLE_FPS = 1
VISION_FRAME_COUNT = 3
STDERR_TAIL_BYTES = 2000


def run_captured(command: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        stderr_tail = (exc.stderr or b"")[-STDERR_TAIL_BYTES:].decode(
            "utf-8", "replace"
        )
        raise RuntimeError(
            f"{command[0]} exited with code {exc.returncode}: {stderr_tail}"
        ) from exc


def compute_frame_diffs_from_raw(raw: bytes, frame_size: int) -> list[float]:
    frame_count = len(raw) // frame_size
    diffs = []
    for i in range(1, frame_count):
        prev = raw[(i - 1) * frame_size : i * frame_size]
        curr = raw[i * frame_size : (i + 1) * frame_size]
        # Both slices are always exactly `frame_size` long: frame_count floor-
        # divides, so a trailing partial frame is dropped before the loop runs
        # and the strict check can never fire today. strict=True satisfies B905
        # and pins that invariant, so a future change to the slicing fails
        # loudly instead of silently scoring a short frame.
        total = sum(abs(a - b) for a, b in zip(prev, curr, strict=True))
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


def build_frame_extract_command(
    video_path: Path, frames_dir: Path, duration_sec: float
) -> list[str]:
    # VISION_FRAME_COUNT frames spread evenly over the file's real length.
    return [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-vf",
        f"fps={VISION_FRAME_COUNT}/{duration_sec:.3f}",
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


def build_duration_probe_command(video_path: Path) -> list[str]:
    return [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]


def build_audio_stream_probe_command(video_path: Path) -> list[str]:
    return [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=index",
        "-of",
        "csv=p=0",
        str(video_path),
    ]


class SubprocessRunner:
    def run(self, command: list[str]) -> None:
        run_captured(command)


class FfmpegMediaProbe:
    def probe_duration_sec(self, video_path: Path) -> float:
        try:
            completed = run_captured(build_duration_probe_command(video_path))
        except RuntimeError as exc:
            raise UnreadableSegmentError(f"{video_path.name}: {exc}") from exc
        raw = completed.stdout.decode("utf-8", "replace").strip()
        try:
            duration = float(raw)
        except ValueError as exc:
            raise UnreadableSegmentError(
                f"{video_path.name}: ffprobe reported no duration ({raw!r})"
            ) from exc
        if not duration > 0:
            raise UnreadableSegmentError(
                f"{video_path.name}: non-positive duration {duration}"
            )
        return duration

    def extract_audio(self, video_path: Path, workdir: Path) -> Path | None:
        # The pendant records video only (capture.py runs rpicam-vid with no
        # ALSA source), and ffmpeg refuses to write a WAV from a file with no
        # audio stream, so check first and report silence as None.
        streams = run_captured(build_audio_stream_probe_command(video_path))
        if not streams.stdout.strip():
            return None
        audio_path = workdir / (video_path.stem + ".wav")
        run_captured(build_audio_extract_command(video_path, audio_path))
        return audio_path

    def extract_frame_diffs(self, video_path: Path) -> list[float]:
        completed = run_captured(build_raw_gray_command(video_path))
        return compute_frame_diffs_from_raw(completed.stdout, FRAME_SIZE)

    def extract_frames(
        self, video_path: Path, workdir: Path, duration_sec: float
    ) -> list[Path]:
        frames_dir = workdir / (video_path.stem + "_frames")
        frames_dir.mkdir(parents=True, exist_ok=True)
        run_captured(build_frame_extract_command(video_path, frames_dir, duration_sec))
        return sorted(frames_dir.glob("frame_*.jpg"))
