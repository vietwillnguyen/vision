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

from pipeline.adapters.ffmpeg_media import VISION_FRAME_COUNT, FfmpegMediaProbe
from pipeline.ingestion import UnreadableSegmentError

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


def test_extract_audio_reads_the_microphone_track_of_an_audio_pendant_segment(
    tmp_path,
):
    # The shape firmware writes when a microphone opens: rpicam-vid's libav
    # backend emits MPEG-TS with H.264 and mono 48 kHz AAC, which
    # firmware/visio_recorder/muxer.py remuxes with `-map 0 -c copy`.
    ts = tmp_path / "seg.ts"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=size=160x90:rate={FRAMERATE}",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", "2", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k", "-ac", "1",
            "-f", "mpegts", str(ts),
        ],
        check=True,
    )  # fmt: skip
    mp4 = tmp_path / "seg.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(ts), "-map", "0", "-c", "copy",
         str(mp4)],
        check=True,
    )  # fmt: skip

    audio = FfmpegMediaProbe().extract_audio(mp4, tmp_path)

    assert audio is not None
    probed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=sample_rate,channels",
         "-of", "csv=p=0", str(audio)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip
    # What the transcriber is sent: 16 kHz mono, about as long as the segment.
    assert probed == "16000,1"
    assert FfmpegMediaProbe().probe_duration_sec(audio) == pytest.approx(2.0, abs=0.1)


def test_probe_duration_measures_a_short_pendant_segment(tmp_path):
    segment = make_pendant_segment(tmp_path, seconds=4)

    assert FfmpegMediaProbe().probe_duration_sec(segment) == pytest.approx(4.0, abs=0.1)


def test_probe_duration_rejects_a_file_that_is_not_video(tmp_path):
    garbage = tmp_path / "20260714_090000.mp4"
    garbage.write_bytes(b"stub mp4 bytes")

    with pytest.raises(UnreadableSegmentError):
        FfmpegMediaProbe().probe_duration_sec(garbage)


def test_extract_frames_spreads_vision_frames_over_a_short_segment(tmp_path):
    # Spacing frames for a nominal 300 s segment gave a 40 s one no frames at
    # all, so vision scored it blind.
    segment = make_pendant_segment(tmp_path, seconds=4)
    probe = FfmpegMediaProbe()

    frames = probe.extract_frames(segment, tmp_path, probe.probe_duration_sec(segment))

    assert len(frames) == VISION_FRAME_COUNT


def _volume_db(path: Path, start: float, seconds: float) -> float:
    out = subprocess.run(
        ["ffmpeg", "-v", "info", "-ss", str(start), "-t", str(seconds),
         "-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"],
        check=True, capture_output=True, text=True,
    ).stderr  # fmt: skip
    line = next(x for x in out.splitlines() if "mean_volume:" in x)
    return float(line.split("mean_volume:")[1].split("dB")[0])


def test_a_reel_mixing_video_only_and_audio_clips_keeps_audio_in_sync(tmp_path):
    # A day the microphone dropped out: the first selected segment is video
    # only, the second has sound. Before ClipAudio, the concat demuxer took
    # the first clip's layout and the reel had no audio at all.
    from pipeline.adapters.ffmpeg_media import SubprocessRunner
    from pipeline.assembly.ffmpeg_assembler import (
        ClipAudio,
        build_assembly_command,
        build_trim_command,
        render_concat_file_content,
    )

    silent = make_pendant_segment(tmp_path, seconds=6, name="silent")
    loud = tmp_path / "loud.mp4"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=size=160x90:rate={FRAMERATE}",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", "6", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "1", str(loud),
        ],
        check=True,
    )  # fmt: skip
    runner = SubprocessRunner()
    clips = [tmp_path / "clip_000.mp4", tmp_path / "clip_001.mp4"]
    runner.run(build_trim_command(silent, clips[0], 1.0, 4, ClipAudio.SILENCE))
    runner.run(build_trim_command(loud, clips[1], 1.0, 4, ClipAudio.SOURCE))
    concat = tmp_path / "concat.txt"
    concat.write_text(render_concat_file_content(clips))
    reel = tmp_path / "reel.mp4"
    runner.run(build_assembly_command(concat, reel, vintage=False))

    probe = FfmpegMediaProbe()
    assert probe.extract_audio(reel, tmp_path) is not None
    # Silence over the first clip, the tone over the second: no shift.
    assert _volume_db(reel, 0.5, 3) < -80
    assert _volume_db(reel, 4.5, 3) > -30
