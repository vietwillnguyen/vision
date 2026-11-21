"""The demo footage path against real ffmpeg: placeholder clips in, pendant-shaped
segments out. CI installs ffmpeg for the pipeline job, so these run there."""

import json
import shutil
import subprocess
from datetime import date, time
from pathlib import Path

import pytest

from pipeline.adapters.ffmpeg_media import SubprocessRunner
from pipeline.demo.footage import load_source_clips, plan_day, render_day
from pipeline.demo.placeholder import PlaceholderClip, generate_placeholder_clips

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="needs ffmpeg and ffprobe on PATH",
)

TINY_CLIPS = (
    PlaceholderClip("01-pattern.mp4", 3.0, "testsrc2", sound=False, flags=(1.5,)),
    PlaceholderClip("02-tone.mp4", 1.5, "gradients", sound=True),
)


def ffprobe_streams(path: Path) -> list[dict]:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "stream=codec_type,codec_name,width,height,r_frame_rate,channels",
            "-show_entries", "format=duration,format_name",
            "-of", "json", str(path),
        ],
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    data = json.loads(out)
    return [
        {**s, "duration": float(data["format"]["duration"])} for s in data["streams"]
    ]


def test_placeholder_clips_render_to_segments_shaped_like_the_pendants(tmp_path):
    clips_dir = generate_placeholder_clips(
        SubprocessRunner(), tmp_path / "clips", TINY_CLIPS
    )
    sources, manifest = load_source_clips(clips_dir)

    assert [s.path.name for s in sources] == ["01-pattern.mp4", "02-tone.mp4"]
    assert [s.has_audio for s in sources] == [False, True]
    assert sources[0].flags == (1.5,)

    plan = plan_day(
        sources,
        date(2026, 11, 20),
        default_start=manifest.start or time(9, 0),
        gap_sec=manifest.gap_sec,
        segment_sec=2,
    )
    rendered = render_day(SubprocessRunner(), plan, tmp_path / "out")

    assert [p.name for p in rendered] == [s.filename for s in plan.segments]
    assert len(rendered) == 3  # 3 s splits into 2 s + 1 s; 1.5 s stays whole
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == sorted(
        p.name for p in rendered
    ), "capture intermediates should not be left behind"
    for path, segment in zip(rendered, plan.segments, strict=True):
        streams = ffprobe_streams(path)
        video = next(s for s in streams if s["codec_type"] == "video")
        assert (video["codec_name"], video["width"], video["height"]) == (
            "h264",
            1280,
            720,
        )
        assert video["r_frame_rate"] == "30/1"
        assert video["duration"] == pytest.approx(segment.duration_sec, abs=0.15)
        audio = [s for s in streams if s["codec_type"] == "audio"]
        if segment.has_audio:
            assert [(a["codec_name"], a["channels"]) for a in audio] == [("aac", 1)]
        else:
            assert audio == []


def test_load_source_clips_rejects_a_manifest_entry_with_no_clip(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"")
    (tmp_path / "demo.toml").write_text('[clips."missing.mp4"]\nflags = [1]\n')

    with pytest.raises(ValueError, match="missing.mp4"):
        load_source_clips(tmp_path)


def test_load_source_clips_rejects_an_empty_directory(tmp_path):
    with pytest.raises(ValueError, match="no video clips"):
        load_source_clips(tmp_path)
