from datetime import datetime

from visio_recorder.capture import segment_filename


def test_segment_filename_uses_recording_start_time():
    assert segment_filename(datetime(2026, 7, 9, 8, 5, 30)) == "20260709_080530.h264"


def test_segment_filename_stem_matches_the_pipeline_parser_contract():
    # After muxing, the .mp4 keeps this stem; the pipeline's
    # parse_segment_filename hard-requires strptime("%Y%m%d_%H%M%S").
    started_at = datetime(2026, 7, 9, 8, 5, 30)
    stem = segment_filename(started_at).removesuffix(".h264")
    assert datetime.strptime(stem, "%Y%m%d_%H%M%S") == started_at
