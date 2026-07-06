import pytest

from pipeline.models import ScoreWeights
from pipeline.scoring.composite import compute_composite_score


def test_default_weights_unflagged():
    score = compute_composite_score(
        scene_novelty=1.0, audio_activity=1.0, motion_intensity=1.0,
        weights=ScoreWeights(), manually_flagged=False,
    )
    assert score == pytest.approx(0.9)


def test_flagged_segment_gets_1_5x_multiplier():
    score = compute_composite_score(
        scene_novelty=1.0, audio_activity=1.0, motion_intensity=1.0,
        weights=ScoreWeights(), manually_flagged=True,
    )
    assert score == pytest.approx(1.35)


def test_custom_weights_are_respected():
    weights = ScoreWeights(scene_weight=1.0, audio_weight=0.0, motion_weight=0.0)
    score = compute_composite_score(
        scene_novelty=0.5, audio_activity=1.0, motion_intensity=1.0,
        weights=weights, manually_flagged=False,
    )
    assert score == pytest.approx(0.5)
