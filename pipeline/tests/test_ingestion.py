from datetime import datetime

import pytest

from pipeline.ingestion import (
    UnreadableSegmentError,
    apply_flag_markers,
    build_segments_from_object_keys,
    parse_flag_marker_filename,
    parse_segment_filename,
    stored_duration_sec,
)


def full_length(_key: str) -> float:
    return 300.0


class measured:  # noqa: N801 - reads as a function at call sites
    """A duration probe keyed by object key, recording what it was asked."""

    def __init__(self, durations: dict[str, float]) -> None:
        self.durations = durations
        self.calls: list[str] = []

    def __call__(self, key: str) -> float:
        self.calls.append(key)
        return self.durations[key]


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
        measure_duration_sec=full_length,
    )

    assert rejected == []
    assert len(segments) == 1
    assert segments[0].id == "device-abc/20260704_120000"
    assert segments[0].device_id == "device-abc"
    assert segments[0].recorded_at == datetime(2026, 7, 4, 12, 0, 0)
    assert segments[0].duration_sec == 300.0
    assert segments[0].s3_key == "device-abc/20260704_120000.mp4"
    assert segments[0].manually_flagged is False


def test_build_segments_rejects_malformed_mp4_keys_without_raising():
    segments, rejected = build_segments_from_object_keys(
        ["device-abc/20260704_120000.mp4", "device-abc/partial-upload.mp4"],
        device_id="device-abc",
        measure_duration_sec=full_length,
    )

    assert [s.id for s in segments] == ["device-abc/20260704_120000"]
    assert rejected == ["device-abc/partial-upload.mp4"]


def test_build_segments_rejects_keys_outside_the_device_prefix():
    segments, rejected = build_segments_from_object_keys(
        ["device-abc/20260704_120000.mp4", "device-xyz/20260704_120000.mp4"],
        device_id="device-abc",
        measure_duration_sec=full_length,
    )

    assert [s.id for s in segments] == ["device-abc/20260704_120000"]
    assert rejected == ["device-xyz/20260704_120000.mp4"]


def test_segment_ids_are_device_scoped_so_same_timestamp_ids_cannot_collide():
    abc_segments, _ = build_segments_from_object_keys(
        ["device-abc/20260704_120000.mp4"],
        device_id="device-abc",
        measure_duration_sec=full_length,
    )
    xyz_segments, _ = build_segments_from_object_keys(
        ["device-xyz/20260704_120000.mp4"],
        device_id="device-xyz",
        measure_duration_sec=full_length,
    )

    assert abc_segments[0].id != xyz_segments[0].id


def test_apply_flag_markers_flags_the_containing_segment():
    segments, _ = build_segments_from_object_keys(
        ["device-abc/20260704_120000.mp4", "device-abc/20260704_120500.mp4"],
        device_id="device-abc",
        measure_duration_sec=full_length,
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
        ["device-abc/20260704_120000.mp4"],
        device_id="device-abc",
        measure_duration_sec=full_length,
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
        ["device-abc/20260704_120000.mp4"],
        device_id="device-abc",
        measure_duration_sec=full_length,
    )

    _, rejected, unmatched = apply_flag_markers(
        segments,
        ["device-abc/FLAG_20260705_120100.marker"],
        device_id="device-abc",
    )

    assert rejected == []
    assert unmatched == ["device-abc/FLAG_20260705_120100.marker"]
    assert segments[0].manually_flagged is False


def test_apply_flag_markers_rejects_undated_legacy_markers_instead_of_guessing():
    segments, _ = build_segments_from_object_keys(
        ["device-abc/20260704_120000.mp4"],
        device_id="device-abc",
        measure_duration_sec=full_length,
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
        ["device-abc/20260704_120000.mp4"],
        device_id="device-abc",
        measure_duration_sec=full_length,
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
        ["device-abc/20260704_120000.mp4"],
        device_id="device-abc",
        measure_duration_sec=full_length,
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


def test_build_segments_carries_the_measured_duration_of_each_file():
    measure = measured(
        {
            "device-abc/20260704_120000.mp4": 299.967,
            "device-abc/20260704_120500.mp4": 40.0,
        }
    )

    segments, rejected = build_segments_from_object_keys(
        ["device-abc/20260704_120000.mp4", "device-abc/20260704_120500.mp4"],
        device_id="device-abc",
        measure_duration_sec=measure,
    )

    assert rejected == []
    assert [s.duration_sec for s in segments] == [299.967, 40.0]


def test_build_segments_only_measures_keys_it_accepts():
    measure = measured({"device-abc/20260704_120000.mp4": 300.0})

    build_segments_from_object_keys(
        [
            "device-abc/20260704_120000.mp4",
            "device-abc/partial-upload.mp4",
            "device-xyz/20260704_120000.mp4",
            "device-abc/FLAG_20260704_120300.marker",
        ],
        device_id="device-abc",
        measure_duration_sec=measure,
    )

    assert measure.calls == ["device-abc/20260704_120000.mp4"]


def test_build_segments_rejects_a_file_with_no_measurable_duration():
    def measure(key: str) -> float:
        if key.endswith("120500.mp4"):
            raise UnreadableSegmentError("no video stream")
        return 300.0

    segments, rejected = build_segments_from_object_keys(
        ["device-abc/20260704_120000.mp4", "device-abc/20260704_120500.mp4"],
        device_id="device-abc",
        measure_duration_sec=measure,
    )

    assert [s.id for s in segments] == ["device-abc/20260704_120000"]
    assert rejected == ["device-abc/20260704_120500.mp4"]


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_build_segments_rejects_a_non_positive_duration(bad):
    segments, rejected = build_segments_from_object_keys(
        ["device-abc/20260704_120000.mp4"],
        device_id="device-abc",
        measure_duration_sec=lambda _key: bad,
    )

    assert segments == []
    assert rejected == ["device-abc/20260704_120000.mp4"]


def test_marker_after_a_short_segment_flags_the_restarted_segment():
    # A stop press at 12:05:40 ends a 40 s segment; recording restarts at
    # 12:06:00. A press at 12:07:00 belongs to the restarted segment, not to
    # the short one's nominal five-minute window.
    segments, _ = build_segments_from_object_keys(
        ["device-abc/20260704_120500.mp4", "device-abc/20260704_120600.mp4"],
        device_id="device-abc",
        measure_duration_sec=measured(
            {
                "device-abc/20260704_120500.mp4": 40.0,
                "device-abc/20260704_120600.mp4": 300.0,
            }
        ),
    )

    _, rejected, unmatched = apply_flag_markers(
        segments,
        ["device-abc/FLAG_20260704_120700.marker"],
        device_id="device-abc",
    )

    assert rejected == unmatched == []
    assert [s.manually_flagged for s in segments] == [False, True]


def test_marker_in_the_gap_after_a_short_segment_is_unmatched():
    segments, _ = build_segments_from_object_keys(
        ["device-abc/20260704_120500.mp4"],
        device_id="device-abc",
        measure_duration_sec=lambda _key: 40.0,
    )

    _, _, unmatched = apply_flag_markers(
        segments,
        ["device-abc/FLAG_20260704_120540.marker"],
        device_id="device-abc",
    )

    assert unmatched == ["device-abc/FLAG_20260704_120540.marker"]
    assert segments[0].manually_flagged is False


def test_marker_in_the_last_partial_second_of_a_segment_matches_it():
    # Markers have one-second resolution: a press at 12:05:40.3 writes
    # FLAG_..._120540, which lies inside a 40.5 s segment started at 12:05:00.
    segments, _ = build_segments_from_object_keys(
        ["device-abc/20260704_120500.mp4"],
        device_id="device-abc",
        measure_duration_sec=lambda _key: 40.5,
    )

    _, _, unmatched = apply_flag_markers(
        segments,
        ["device-abc/FLAG_20260704_120540.marker"],
        device_id="device-abc",
    )

    assert unmatched == []
    assert segments[0].manually_flagged is True


@pytest.mark.parametrize(
    ("measured_sec", "stored"),
    [(40.0, 40), (39.9996, 40), (40.5, 41), (299.967, 300), (300.0001, 300)],
)
def test_stored_duration_rounds_up_to_whole_seconds(measured_sec, stored):
    # Rounding up keeps [recorded_at, recorded_at + stored) covering every
    # one-second marker the file covers; sub-millisecond probe noise is not a
    # second of footage.
    assert stored_duration_sec(measured_sec) == stored
