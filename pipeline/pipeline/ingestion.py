"""Turn a device day's storage keys into segments and flag them.

A segment's length is never assumed: the firmware's nominal segment length is
only an upper bound (a stop press, a crash or a dead battery all end a segment
early), so the caller measures every file and that measurement is what clip
extraction, flag-marker windows and the stored ``duration_sec`` all use. The
contract is written down in the pendant design spec, "Segment duration".
"""

import math
from collections.abc import Callable
from datetime import datetime, timedelta

from pipeline.models import DEFAULT_LOCATION, Segment


class UnreadableSegmentError(Exception):
    """The segment file has no measurable video duration."""


def stored_duration_sec(duration_sec: float) -> int:
    """Whole seconds for the integer ``segments.duration_sec`` column.

    Rounded up so ``[recorded_at, recorded_at + stored)`` still covers every
    one-second-resolution flag marker the file covers; rounding to the
    millisecond first keeps probe noise (300.0001 s) from adding a second.
    """
    return math.ceil(round(duration_sec, 3))


def segment_window_end(recorded_at: datetime, duration_sec: float) -> datetime:
    """End (exclusive) of the stretch of the day a segment covers."""
    return recorded_at + timedelta(seconds=duration_sec)


def parse_segment_filename(filename: str) -> datetime:
    stem = filename.removesuffix(".mp4")
    return datetime.strptime(stem, "%Y%m%d_%H%M%S")


def parse_flag_marker_filename(filename: str) -> datetime:
    stem = filename.removeprefix("FLAG_").removesuffix(".marker")
    return datetime.strptime(stem, "%Y%m%d_%H%M%S")


def build_segments_from_object_keys(
    object_keys: list[str],
    device_id: str,
    measure_duration_sec: Callable[[str], float],
) -> tuple[list[Segment], list[str]]:
    """Parse segment keys and measure each accepted one.

    ``measure_duration_sec`` maps an object key to the file's real length in
    seconds and raises ``UnreadableSegmentError`` for a file with none; such a
    key is rejected like a malformed one. Any other exception (a failed
    download, say) propagates, so the device day is retried next run.
    """
    device_prefix = f"{device_id}/"
    segments = []
    rejected_keys = []
    for key in object_keys:
        filename = key.split("/")[-1]
        if not filename.endswith(".mp4"):
            continue
        if not key.startswith(device_prefix):
            rejected_keys.append(key)
            continue
        try:
            recorded_at = parse_segment_filename(filename)
        except ValueError:
            rejected_keys.append(key)
            continue
        try:
            duration_sec = measure_duration_sec(key)
        except UnreadableSegmentError:
            rejected_keys.append(key)
            continue
        if not duration_sec > 0:
            rejected_keys.append(key)
            continue
        segments.append(
            Segment(
                id=f"{device_id}/{filename.removesuffix('.mp4')}",
                device_id=device_id,
                recorded_at=recorded_at,
                duration_sec=duration_sec,
                s3_key=key,
                location=DEFAULT_LOCATION,
            )
        )
    return segments, rejected_keys


def apply_flag_markers(
    segments: list[Segment], flag_marker_keys: list[str], device_id: str
) -> tuple[list[Segment], list[str], list[str]]:
    """Flag segments whose window contains a marker's timestamp.

    Marker filenames (FLAG_YYYYMMDD_HHMMSS.marker) carry their full date, so a
    stale or cross-day-retried marker can never flag another day's segment: a
    marker whose date falls outside every segment window is returned as
    unmatched for DLQ-style retry, and undated legacy markers are rejected.
    """
    device_prefix = f"{device_id}/"
    rejected_keys = []
    unmatched_keys = []
    for key in flag_marker_keys:
        filename = key.split("/")[-1]
        if not filename.endswith(".marker"):
            continue
        if not key.startswith(device_prefix):
            rejected_keys.append(key)
            continue
        try:
            flag_at = parse_flag_marker_filename(filename)
        except ValueError:
            rejected_keys.append(key)
            continue
        for segment in segments:
            window_end = segment_window_end(segment.recorded_at, segment.duration_sec)
            if segment.recorded_at <= flag_at < window_end:
                segment.manually_flagged = True
                break
        else:
            unmatched_keys.append(key)
    return segments, rejected_keys, unmatched_keys
