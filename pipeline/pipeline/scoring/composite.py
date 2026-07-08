from pipeline.models import ScoreWeights


def compute_composite_score(
    scene_novelty: float,
    audio_activity: float,
    motion_intensity: float,
    weights: ScoreWeights,
    manually_flagged: bool,
) -> float:
    base_score = (
        weights.scene_weight * scene_novelty
        + weights.audio_weight * audio_activity
        + weights.motion_weight * motion_intensity
    )
    return base_score * 1.5 if manually_flagged else base_score
