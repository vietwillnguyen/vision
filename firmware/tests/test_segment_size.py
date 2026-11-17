import pytest

from visio_recorder.segment_size import (
    AUDIO_BITRATE_RESERVE_BPS,
    DEFAULT_MAX_UPLOAD_BYTES,
    MAX_SEGMENT_DURATION_MS,
    SIZE_BUDGET_FRACTION,
    default_segment_duration_ms,
    predicted_segment_bytes,
)


def test_default_max_upload_is_the_hosted_free_plan_limit():
    assert DEFAULT_MAX_UPLOAD_BYTES == 50 * 1024 * 1024


def test_predicted_size_is_bitrate_times_duration_plus_audio_and_overhead():
    # 4 Mbps video + 128 kbps reserved for audio = 516 000 B/s, plus 2 %.
    assert predicted_segment_bytes(60_000, 4_000_000) == 31_579_200


def test_predicted_size_grows_linearly_with_duration():
    assert predicted_segment_bytes(120_000, 4_000_000) == 2 * predicted_segment_bytes(
        60_000, 4_000_000
    )


def test_default_duration_for_720p_at_4_mbps_is_one_minute():
    assert default_segment_duration_ms(DEFAULT_MAX_UPLOAD_BYTES, 4_000_000) == 60_000


def test_default_duration_keeps_the_predicted_size_inside_the_budget():
    for bitrate in (1_000_000, 2_500_000, 4_000_000, 6_000_000, 10_000_000):
        duration_ms = default_segment_duration_ms(DEFAULT_MAX_UPLOAD_BYTES, bitrate)
        assert predicted_segment_bytes(duration_ms, bitrate) <= (
            DEFAULT_MAX_UPLOAD_BYTES * SIZE_BUDGET_FRACTION
        )


def test_default_duration_is_a_whole_number_of_ten_second_steps():
    assert default_segment_duration_ms(DEFAULT_MAX_UPLOAD_BYTES, 6_000_000) == 40_000


def test_default_duration_never_exceeds_five_minutes():
    # A raised upload limit (paid plan) must not produce hour-long segments.
    assert (
        default_segment_duration_ms(5 * 1024**3, 4_000_000) == MAX_SEGMENT_DURATION_MS
    )


def test_default_duration_rejects_a_bitrate_too_high_for_any_segment():
    with pytest.raises(ValueError, match="VISIO_VIDEO_BITRATE"):
        default_segment_duration_ms(1_000_000, 25_000_000)


def test_audio_reserve_covers_a_128_kbps_aac_track():
    assert AUDIO_BITRATE_RESERVE_BPS >= 128_000
