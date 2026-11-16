"""FfmpegMediaProbe against real ffmpeg/ffprobe on generated media.

The command builders are unit-tested in test_adapters.py; this file proves the
adapter's behaviour on files shaped like the pendant's: raw H.264 muxed with
firmware/visio_recorder/muxer.py's command, so silent and of real length. CI
installs ffmpeg for the pipeline job, so these run there rather than skip.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from pipeline.adapters.ffmpeg_media import FfmpegMediaProbe

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="needs ffmpeg and ffprobe on PATH",
)

FRAMERATE = 30


def make_pendant_segment(tmp_path: Path, seconds: float, name: str = "seg") -> Path:
    h264 = tmp_path / f"{name}.h264"
    mp4 = tmp_path / f"{name}.mp4"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=size=160x90:rate={FRAMERATE}",
            "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-f", "h264", str(h264),
        ],
        check=True,
    )  # fmt: skip
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-framerate", str(FRAMERATE),
            "-i", str(h264), "-c", "copy", str(mp4),
        ],
        check=True,
    )  # fmt: skip
    return mp4


def test_extract_audio_returns_none_for_a_silent_pendant_segment(tmp_path):
    segment = make_pendant_segment(tmp_path, seconds=2)

    assert FfmpegMediaProbe().extract_audio(segment, tmp_path) is None


def test_extract_audio_writes_a_wav_when_the_segment_has_sound(tmp_path):
    segment = tmp_path / "with_sound.mp4"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=160x90:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000",
            "-t", "2", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(segment),
        ],
        check=True,
    )  # fmt: skip

    audio = FfmpegMediaProbe().extract_audio(segment, tmp_path)

    assert audio is not None
    assert audio.suffix == ".wav"
    assert audio.stat().st_size > 0
