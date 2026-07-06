from datetime import datetime

from pipeline.models import ScoreWeights, Segment


def test_score_weights_defaults_match_spec():
    weights = ScoreWeights()
    assert weights.scene_weight == 0.4
    assert weights.audio_weight == 0.3
    assert weights.motion_weight == 0.2


def test_segment_defaults_composite_score_and_manually_flagged():
    segment = Segment(
        id="seg-1",
        recorded_at=datetime(2026, 7, 4, 12, 0, 0),
        duration_sec=300,
        s3_key="device-abc/20260704_120000.mp4",
        location="indoor",
    )
    assert segment.composite_score == 0.0
    assert segment.manually_flagged is False
