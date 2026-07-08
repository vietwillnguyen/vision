from pathlib import Path

import pytest

from pipeline.models import ScoreWeights
from pipeline.scoring.audio import TranscriptionResult
from pipeline.scoring_pipeline import score_segment


class FakeVisionClient:
    def __init__(self, response: str) -> None:
        self.response = response
        self.called = False

    def score_frames(self, frame_paths: list[Path], prompt: str) -> str:
        self.called = True
        return self.response


def test_high_motion_segment_runs_scene_scoring():
    transcription = TranscriptionResult(
        speech_presence_ratio=0.5, silence_ratio=0.5, has_exclamation=False
    )
    vision_client = FakeVisionClient(
        '{"score": 10, "location": "outdoor", "people": true}'
    )

    composite, location = score_segment(
        transcription=transcription,
        frame_diffs=[200.0, 200.0],
        vision_client=vision_client,
        frame_paths=[Path("/f1.jpg")],
        weights=ScoreWeights(),
        manually_flagged=False,
    )

    assert vision_client.called is True
    assert location == "outdoor"
    assert composite == pytest.approx(0.4 * 1.0 + 0.3 * 0.5 + 0.2 * (200.0 / 255.0))


def test_low_motion_segment_skips_scene_scoring():
    transcription = TranscriptionResult(
        speech_presence_ratio=0.0, silence_ratio=1.0, has_exclamation=False
    )
    vision_client = FakeVisionClient(
        '{"score": 10, "location": "outdoor", "people": true}'
    )

    composite, location = score_segment(
        transcription=transcription,
        frame_diffs=[1.0],
        vision_client=vision_client,
        frame_paths=[Path("/f1.jpg")],
        weights=ScoreWeights(),
        manually_flagged=False,
    )

    assert vision_client.called is False
    assert location == "indoor"
    assert composite == pytest.approx(0.4 * 0.0 + 0.3 * 0.0 + 0.2 * (1.0 / 255.0))
