import pytest

from pipeline.models import ScoreWeights
from pipeline.scoring.composite import compute_composite_score


def test_default_weights_unflagged():
    score = compute_composite_score(
        scene_novelty=1.0,
        audio_activity=1.0,
        motion_intensity=1.0,
        weights=ScoreWeights(),
        manually_flagged=False,
    )
    # The spec's 0.4 + 0.3 + 0.2.
    assert score == pytest.approx(0.9)


def test_flagged_segment_gets_1_5x_multiplier():
    score = compute_composite_score(
        scene_novelty=1.0,
        audio_activity=1.0,
        motion_intensity=1.0,
        weights=ScoreWeights(),
        manually_flagged=True,
    )
    assert score == pytest.approx(1.35)


def test_default_weights_count_audio_activity_at_the_spec_weight():
    # A video-only segment is scored as silence (activity 0), so the audio
    # term only lifts segments whose microphone track has activity.
    silent = compute_composite_score(
        scene_novelty=0.5,
        audio_activity=0.0,
        motion_intensity=0.5,
        weights=ScoreWeights(),
        manually_flagged=False,
    )
    speech = compute_composite_score(
        scene_novelty=0.5,
        audio_activity=1.0,
        motion_intensity=0.5,
        weights=ScoreWeights(),
        manually_flagged=False,
    )

    assert silent == pytest.approx(0.4 * 0.5 + 0.2 * 0.5)
    assert speech - silent == pytest.approx(0.3)


def test_custom_weights_are_respected():
    weights = ScoreWeights(scene_weight=1.0, audio_weight=0.0, motion_weight=0.0)
    score = compute_composite_score(
        scene_novelty=0.5,
        audio_activity=1.0,
        motion_intensity=1.0,
        weights=weights,
        manually_flagged=False,
    )
    assert score == pytest.approx(0.5)
