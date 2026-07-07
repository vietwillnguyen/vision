from pathlib import Path

import pytest

from pipeline.assembly.ffmpeg_assembler import (
    VINTAGE_FILTER,
    build_assembly_command,
    build_trim_command,
    compute_clip_offset_sec,
    render_concat_file_content,
)


def test_compute_clip_offset_centers_the_clip_in_a_longer_segment():
    assert compute_clip_offset_sec(
        segment_duration_sec=300, clip_duration_sec=15
    ) == pytest.approx(142.5)


def test_compute_clip_offset_clamps_to_zero_when_segment_shorter_than_clip():
    assert compute_clip_offset_sec(segment_duration_sec=10, clip_duration_sec=15) == 0.0


def test_build_trim_command_reencodes_for_frame_accurate_cuts():
    args = build_trim_command(
        Path("/tmp/segment.mp4"),
        Path("/tmp/clip.mp4"),
        offset_sec=142.5,
        clip_duration_sec=15,
    )
    assert args == [
        "ffmpeg",
        "-y",
        "-ss",
        "142.5",
        "-i",
        "/tmp/segment.mp4",
        "-t",
        "15",
        "-c:v",
        "libx264",
        "/tmp/clip.mp4",
    ]


def test_render_concat_file_content_lists_each_clip():
    content = render_concat_file_content([Path("/tmp/a.mp4"), Path("/tmp/b.mp4")])
    assert content == "file '/tmp/a.mp4'\nfile '/tmp/b.mp4'\n"


def test_render_concat_file_content_escapes_single_quotes_in_paths():
    content = render_concat_file_content([Path("/tmp/dad's reel.mp4")])
    assert content == "file '/tmp/dad'\\''s reel.mp4'\n"


def test_build_assembly_command_clean_style():
    args = build_assembly_command(
        Path("/tmp/concat.txt"), Path("/tmp/out.mp4"), vintage=False
    )
    assert args == [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        "/tmp/concat.txt",
        "-s",
        "1280x720",
        "-c:v",
        "libx264",
        "/tmp/out.mp4",
    ]


def test_build_assembly_command_vintage_style_adds_filter():
    args = build_assembly_command(
        Path("/tmp/concat.txt"), Path("/tmp/out.mp4"), vintage=True
    )
    assert args == [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        "/tmp/concat.txt",
        "-s",
        "1280x720",
        "-c:v",
        "libx264",
        "-vf",
        VINTAGE_FILTER,
        "/tmp/out.mp4",
    ]
