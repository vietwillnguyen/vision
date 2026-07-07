from datetime import date, datetime, time, timedelta

from pipeline.models import DEFAULT_LOCATION, Segment

SEGMENT_DURATION_SEC = 300


def parse_segment_filename(filename: str) -> datetime:
    stem = filename.removesuffix(".mp4")
    return datetime.strptime(stem, "%Y%m%d_%H%M%S")


def parse_flag_marker_filename(filename: str) -> time:
    stem = filename.removeprefix("FLAG_").removesuffix(".marker")
    return datetime.strptime(stem, "%H%M%S").time()


def build_segments_from_object_keys(
    object_keys: list[str], device_id: str
) -> tuple[list[Segment], list[str]]:
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
        segments.append(
            Segment(
                id=f"{device_id}/{filename.removesuffix('.mp4')}",
                device_id=device_id,
                recorded_at=recorded_at,
                duration_sec=SEGMENT_DURATION_SEC,
                s3_key=key,
                location=DEFAULT_LOCATION,
            )
        )
    return segments, rejected_keys


def apply_flag_markers(
    segments: list[Segment], day: date, flag_marker_keys: list[str], device_id: str
) -> tuple[list[Segment], list[str]]:
    device_prefix = f"{device_id}/"
    rejected_keys = []
    for key in flag_marker_keys:
        filename = key.split("/")[-1]
        if not filename.endswith(".marker"):
            continue
        if not key.startswith(device_prefix):
            rejected_keys.append(key)
            continue
        try:
            flag_time = parse_flag_marker_filename(filename)
        except ValueError:
            rejected_keys.append(key)
            continue
        flag_at = datetime.combine(day, flag_time)
        for segment in segments:
            window_end = segment.recorded_at + timedelta(seconds=segment.duration_sec)
            if segment.recorded_at <= flag_at < window_end:
                segment.manually_flagged = True
                break
    return segments, rejected_keys
