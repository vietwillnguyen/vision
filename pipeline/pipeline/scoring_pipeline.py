from pathlib import Path

from pipeline.models import DEFAULT_LOCATION, ScoreWeights
from pipeline.scoring.audio import TranscriptionResult, compute_audio_activity
from pipeline.scoring.composite import compute_composite_score
from pipeline.scoring.motion import compute_motion_intensity, should_run_scene_scoring
from pipeline.scoring.scene import VisionClient, score_scene


def score_segment(
    transcription: TranscriptionResult,
    frame_diffs: list[float],
    vision_client: VisionClient | None,
    frame_paths: list[Path],
    weights: ScoreWeights,
    manually_flagged: bool,
) -> tuple[float, str]:
    audio_activity = compute_audio_activity(transcription)
    motion_intensity = compute_motion_intensity(frame_diffs)

    if vision_client is not None and should_run_scene_scoring(motion_intensity):
        scene = score_scene(vision_client, frame_paths)
        scene_novelty = scene.novelty
        location = scene.location
    else:
        scene_novelty = 0.0
        location = DEFAULT_LOCATION

    composite = compute_composite_score(
        scene_novelty=scene_novelty,
        audio_activity=audio_activity,
        motion_intensity=motion_intensity,
        weights=weights,
        manually_flagged=manually_flagged,
    )
    return composite, location
