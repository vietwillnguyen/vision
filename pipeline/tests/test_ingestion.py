from datetime import date, datetime, time

from pipeline.ingestion import (
    apply_flag_markers,
    build_segments_from_object_keys,
    parse_flag_marker_filename,
    parse_segment_filename,
)


def test_parse_segment_filename():
    assert parse_segment_filename("20260704_120000.mp4") == datetime(2026, 7, 4, 12, 0, 0)


def test_parse_flag_marker_filename():
    assert parse_flag_marker_filename("FLAG_120300.marker") == time(12, 3, 0)


def test_build_segments_from_object_keys_ignores_marker_files():
    segments = build_segments_from_object_keys(
        ["device-abc/20260704_120000.mp4", "device-abc/FLAG_120300.marker"],
        device_id="device-abc",
    )

    assert len(segments) == 1
    assert segments[0].id == "20260704_120000"
    assert segments[0].recorded_at == datetime(2026, 7, 4, 12, 0, 0)
    assert segments[0].duration_sec == 300
    assert segments[0].s3_key == "device-abc/20260704_120000.mp4"
    assert segments[0].manually_flagged is False


def test_apply_flag_markers_flags_the_containing_segment():
    segments = build_segments_from_object_keys(
        ["device-abc/20260704_120000.mp4", "device-abc/20260704_120500.mp4"],
        device_id="device-abc",
    )

    apply_flag_markers(segments, date(2026, 7, 4), ["device-abc/FLAG_120300.marker"])

    assert segments[0].manually_flagged is True
    assert segments[1].manually_flagged is False


def test_apply_flag_markers_ignores_markers_outside_any_segment_window():
    segments = build_segments_from_object_keys(
        ["device-abc/20260704_120000.mp4"], device_id="device-abc"
    )

    apply_flag_markers(segments, date(2026, 7, 4), ["device-abc/FLAG_235900.marker"])

    assert segments[0].manually_flagged is False
