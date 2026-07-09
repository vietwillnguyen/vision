from datetime import datetime

import pytest

from pipeline.ingestion import (
    apply_flag_markers,
    build_segments_from_object_keys,
    parse_flag_marker_filename,
    parse_segment_filename,
)


def test_parse_segment_filename():
    assert parse_segment_filename("20260704_120000.mp4") == datetime(
        2026, 7, 4, 12, 0, 0
    )


def test_parse_flag_marker_filename():
    assert parse_flag_marker_filename("FLAG_20260704_120300.marker") == datetime(
        2026, 7, 4, 12, 3, 0
    )


def test_parse_flag_marker_filename_rejects_undated_legacy_format():
    with pytest.raises(ValueError):
        parse_flag_marker_filename("FLAG_120300.marker")


def test_build_segments_from_object_keys_ignores_marker_files():
    segments, rejected = build_segments_from_object_keys(
        ["device-abc/20260704_120000.mp4", "device-abc/FLAG_20260704_120300.marker"],
        device_id="device-abc",
    )

    assert rejected == []
    assert len(segments) == 1
    assert segments[0].id == "device-abc/20260704_120000"
    assert segments[0].device_id == "device-abc"
    assert segments[0].recorded_at == datetime(2026, 7, 4, 12, 0, 0)
    assert segments[0].duration_sec == 300
    assert segments[0].s3_key == "device-abc/20260704_120000.mp4"
    assert segments[0].manually_flagged is False


def test_build_segments_rejects_malformed_mp4_keys_without_raising():
    segments, rejected = build_segments_from_object_keys(
        ["device-abc/20260704_120000.mp4", "device-abc/partial-upload.mp4"],
        device_id="device-abc",
    )

    assert [s.id for s in segments] == ["device-abc/20260704_120000"]
    assert rejected == ["device-abc/partial-upload.mp4"]


def test_build_segments_rejects_keys_outside_the_device_prefix():
    segments, rejected = build_segments_from_object_keys(
        ["device-abc/20260704_120000.mp4", "device-xyz/20260704_120000.mp4"],
        device_id="device-abc",
    )

    assert [s.id for s in segments] == ["device-abc/20260704_120000"]
    assert rejected == ["device-xyz/20260704_120000.mp4"]


def test_segment_ids_are_device_scoped_so_same_timestamp_ids_cannot_collide():
    abc_segments, _ = build_segments_from_object_keys(
        ["device-abc/20260704_120000.mp4"], device_id="device-abc"
    )
    xyz_segments, _ = build_segments_from_object_keys(
        ["device-xyz/20260704_120000.mp4"], device_id="device-xyz"
    )

    assert abc_segments[0].id != xyz_segments[0].id


def test_apply_flag_markers_flags_the_containing_segment():
    segments, _ = build_segments_from_object_keys(
        ["device-abc/20260704_120000.mp4", "device-abc/20260704_120500.mp4"],
        device_id="device-abc",
    )

    _, rejected, unmatched = apply_flag_markers(
        segments,
        ["device-abc/FLAG_20260704_120300.marker"],
        device_id="device-abc",
    )

    assert rejected == []
    assert unmatched == []
    assert segments[0].manually_flagged is True
    assert segments[1].manually_flagged is False


def test_apply_flag_markers_reports_markers_outside_any_segment_window_as_unmatched():
    segments, _ = build_segments_from_object_keys(
        ["device-abc/20260704_120000.mp4"], device_id="device-abc"
    )

    _, rejected, unmatched = apply_flag_markers(
        segments,
        ["device-abc/FLAG_20260704_235900.marker"],
        device_id="device-abc",
    )

    assert rejected == []
    assert unmatched == ["device-abc/FLAG_20260704_235900.marker"]
    assert segments[0].manually_flagged is False


def test_apply_flag_markers_does_not_flag_a_same_time_segment_from_another_day():
    segments, _ = build_segments_from_object_keys(
        ["device-abc/20260704_120000.mp4"], device_id="device-abc"
    )

    _, rejected, unmatched = apply_flag_markers(
        segments,
        ["device-abc/FLAG_20260705_120100.marker"],
        device_id="device-abc",
    )

    assert rejected == []
    assert unmatched == ["device-abc/FLAG_20260705_120100.marker"]
    assert segments[0].manually_flagged is False


def test_apply_flag_markers_rejects_undated_legacy_markers_instead_of_guessing_the_day():
    segments, _ = build_segments_from_object_keys(
        ["device-abc/20260704_120000.mp4"], device_id="device-abc"
    )

    _, rejected, unmatched = apply_flag_markers(
        segments,
        ["device-abc/FLAG_120100.marker"],
        device_id="device-abc",
    )

    assert rejected == ["device-abc/FLAG_120100.marker"]
    assert unmatched == []
    assert segments[0].manually_flagged is False


def test_apply_flag_markers_rejects_unparseable_markers_without_raising():
    segments, _ = build_segments_from_object_keys(
        ["device-abc/20260704_120000.mp4"], device_id="device-abc"
    )

    _, rejected, unmatched = apply_flag_markers(
        segments,
        [
            "device-abc/FLAG_garbage.marker",
            "device-abc/FLAG_20260704_120100.marker",
        ],
        device_id="device-abc",
    )

    assert rejected == ["device-abc/FLAG_garbage.marker"]
    assert unmatched == []
    assert segments[0].manually_flagged is True


def test_apply_flag_markers_rejects_markers_outside_the_device_prefix():
    segments, _ = build_segments_from_object_keys(
        ["device-abc/20260704_120000.mp4"], device_id="device-abc"
    )

    _, rejected, unmatched = apply_flag_markers(
        segments,
        [
            "device-xyz/FLAG_20260704_120100.marker",
            "device-abc/FLAG_20260704_120200.marker",
        ],
        device_id="device-abc",
    )

    assert rejected == ["device-xyz/FLAG_20260704_120100.marker"]
    assert unmatched == []
    assert segments[0].manually_flagged is True
