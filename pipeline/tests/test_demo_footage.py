"""Planning and command building for the demo seed's firmware-shaped footage."""

from datetime import date, datetime, time
from pathlib import Path

import pytest

from pipeline.demo.footage import (
    SourceClip,
    capture_command,
    find_source_clips,
    mux_command,
    plan_day,
)
from pipeline.demo.manifest import ClipSettings, Manifest, parse_manifest
from pipeline.ingestion import match_marker, parse_flag_marker_filename

DAY = date(2026, 11, 20)


def clip(name: str, seconds: float, **kwargs) -> SourceClip:
    return SourceClip(path=Path(name), duration_sec=seconds, has_audio=False, **kwargs)


def test_a_short_clip_becomes_one_segment_named_like_the_firmware_names_it():
    plan = plan_day([clip("a.mp4", 40)], DAY, default_start=time(9, 0))

    assert [s.filename for s in plan.segments] == ["20261120_090000.mp4"]
    assert plan.segments[0].offset_sec == 0
    assert plan.segments[0].duration_sec == 40


def test_a_long_clip_splits_into_one_minute_segments_with_a_restart_gap():
    plan = plan_day([clip("a.mp4", 150)], DAY, default_start=time(9, 0))

    assert [(s.filename, s.offset_sec, s.duration_sec) for s in plan.segments] == [
        ("20261120_090000.mp4", 0, 60),
        # rpicam-vid restarts per segment, so each starts a second after the
        # previous one ended.
        ("20261120_090101.mp4", 60, 60),
        ("20261120_090202.mp4", 120, 30),
    ]


def test_a_sub_second_remainder_is_dropped_rather_than_uploaded():
    plan = plan_day([clip("a.mp4", 60.4)], DAY, default_start=time(9, 0))

    assert [s.duration_sec for s in plan.segments] == [60]


def test_clips_follow_each_other_with_a_gap_unless_they_carry_a_start():
    plan = plan_day(
        [
            clip("a.mp4", 30.5),
            clip("b.mp4", 20),
            clip("c.mp4", 10, start=time(11, 15)),
        ],
        DAY,
        default_start=time(9, 0),
        gap_sec=300,
    )

    assert [s.filename for s in plan.segments] == [
        "20261120_090000.mp4",
        # a ends at 09:00:30.5, rounded up to the next whole second, plus 300 s.
        "20261120_090531.mp4",
        "20261120_111500.mp4",
    ]


def test_a_clip_whose_start_overlaps_the_previous_clip_is_rejected():
    with pytest.raises(ValueError, match="b.mp4"):
        plan_day(
            [clip("a.mp4", 120), clip("b.mp4", 10, start=time(9, 1))],
            DAY,
            default_start=time(9, 0),
        )


def test_flags_become_markers_the_pipeline_matches_to_the_right_segment():
    plan = plan_day(
        [clip("a.mp4", 150, flags=(5.5, 75.0)), clip("b.mp4", 20, flags=(19.9,))],
        DAY,
        default_start=time(9, 0),
        gap_sec=60,
    )

    assert plan.markers == [
        "FLAG_20261120_090005.marker",
        # 75 s into the clip is 15 s into its second segment (09:01:01).
        "FLAG_20261120_090116.marker",
        # a's last segment ends 09:02:32; b starts a 60 s gap later.
        "FLAG_20261120_090351.marker",
    ]
    windows = [(s.recorded_at, s.duration_sec) for s in plan.segments]
    matched = [
        match_marker(windows, parse_flag_marker_filename(marker))
        for marker in plan.markers
    ]
    assert matched == [0, 1, 3]


@pytest.mark.parametrize("offset", [-1.0, 150.0, 151.0])
def test_a_flag_outside_its_clip_is_rejected(offset):
    with pytest.raises(ValueError, match="a.mp4"):
        plan_day([clip("a.mp4", 150, flags=(offset,))], DAY, default_start=time(9, 0))


def test_a_plan_that_runs_past_midnight_is_rejected():
    with pytest.raises(ValueError, match="midnight"):
        plan_day([clip("a.mp4", 120)], DAY, default_start=time(23, 59))


def test_segments_carry_their_recorded_at_as_a_naive_utc_datetime():
    plan = plan_day([clip("a.mp4", 10)], DAY, default_start=time(9, 0))

    assert plan.segments[0].recorded_at == datetime(2026, 11, 20, 9, 0, 0)


def test_a_silent_capture_is_raw_h264_muxed_with_the_firmware_command(tmp_path):
    plan = plan_day([clip("src.mov", 40)], DAY, default_start=time(9, 0))
    segment = plan.segments[0]
    capture = tmp_path / "20261120_090000.h264"

    command = capture_command(segment, capture)

    assert command[-1] == str(capture)
    assert command[command.index("-f", command.index("src.mov")) + 1] == "h264"
    assert "-an" in command
    assert command[command.index("-ss") + 1] == "0"
    assert command[command.index("-t") + 1] == "40"
    assert (
        "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720"
        in (command[command.index("-vf") + 1])
    )
    assert mux_command(capture, 30) == [
        "ffmpeg", "-y", "-framerate", "30", "-i", str(capture), "-c", "copy",
        str(tmp_path / "20261120_090000.mp4"),
    ]  # fmt: skip


def test_a_capture_with_sound_is_mpeg_ts_remuxed_with_every_stream(tmp_path):
    source = SourceClip(path=Path("talk.mp4"), duration_sec=90, has_audio=True)
    segment = plan_day([source], DAY, default_start=time(9, 0)).segments[1]
    capture = tmp_path / "20261120_090101.ts"

    command = capture_command(segment, capture)

    assert command[command.index("-ss") + 1] == "60"
    assert command[command.index("-t") + 1] == "30"
    assert command[command.index("-c:a") + 1] == "aac"
    assert command[command.index("-ac") + 1] == "1"
    assert command[command.index("-b:a") + 1] == "64000"
    assert command[command.index("-f", command.index("talk.mp4")) + 1] == "mpegts"
    assert mux_command(capture, 30) == [
        "ffmpeg", "-y", "-i", str(capture), "-map", "0", "-c", "copy",
        str(tmp_path / "20261120_090101.mp4"),
    ]  # fmt: skip


def test_the_capture_is_capped_at_the_firmware_bitrate_so_it_fits_the_bucket():
    segment = plan_day([clip("a.mp4", 60)], DAY, default_start=time(9, 0)).segments[0]

    command = capture_command(segment, Path("x.h264"))

    assert command[command.index("-maxrate") + 1] == "4000000"
    assert command[command.index("-r") + 1] == "30"


def test_find_source_clips_lists_videos_by_name_and_ignores_everything_else(
    tmp_path,
):
    for name in ["b.MOV", "a.mp4", "notes.txt", "demo.toml", "c.webm"]:
        (tmp_path / name).write_bytes(b"")
    (tmp_path / "sub").mkdir()

    assert [p.name for p in find_source_clips(tmp_path)] == ["a.mp4", "b.MOV", "c.webm"]


def test_parse_manifest_reads_start_gap_and_per_clip_settings():
    manifest = parse_manifest(
        """
        start = "08:30"
        gap_minutes = 4

        [clips."coffee.mp4"]
        start = "09:12:30"
        flags = [4.5, 30]

        [clips."walk.mov"]
        flags = [12]
        """
    )

    assert manifest == Manifest(
        start=time(8, 30),
        gap_sec=240,
        clips={
            "coffee.mp4": ClipSettings(start=time(9, 12, 30), flags=(4.5, 30.0)),
            "walk.mov": ClipSettings(start=None, flags=(12.0,)),
        },
    )


def test_parse_manifest_defaults_when_empty():
    assert parse_manifest("") == Manifest()


@pytest.mark.parametrize(
    "text",
    [
        'start = "9am"',
        "gap_minutes = -1",
        '[clips."a.mp4"]\nflags = ["soon"]',
        '[clips."a.mp4"]\ncolour = "red"',
        'surprise = "key"',
    ],
)
def test_parse_manifest_rejects_what_it_does_not_understand(text):
    with pytest.raises(ValueError):
        parse_manifest(text)
