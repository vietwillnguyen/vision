"""The demo seed's scoring switch: stubbed unless real scoring is asked for."""

import json
from pathlib import Path

import pytest

from pipeline.adapters.litellm_clients import LiteLLMTranscriber, LiteLLMVisionClient
from pipeline.demo.scoring import (
    ScoringMode,
    StubTranscriber,
    StubVision,
    scoring_clients,
)
from pipeline.scoring.scene import parse_scene_response


def test_stub_scoring_is_the_default_and_needs_no_keys():
    transcriber, vision = scoring_clients(ScoringMode.STUB, env={}, needs_audio=True)

    assert isinstance(transcriber, StubTranscriber)
    assert isinstance(vision, StubVision)


def test_stub_scoring_ignores_keys_that_happen_to_be_set():
    # A developer shell often exports ANTHROPIC_API_KEY; that alone must never
    # turn a seed into a paid run.
    transcriber, vision = scoring_clients(
        ScoringMode.STUB,
        env={"ANTHROPIC_API_KEY": "sk-ant-x", "OPENAI_API_KEY": "sk-x"},
        needs_audio=True,
    )

    assert isinstance(transcriber, StubTranscriber)
    assert isinstance(vision, StubVision)


def test_real_scoring_refuses_to_start_without_the_vision_key():
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        scoring_clients(ScoringMode.REAL, env={}, needs_audio=False)


def test_real_scoring_needs_the_transcription_key_only_when_a_clip_has_sound():
    _, vision = scoring_clients(
        ScoringMode.REAL, env={"ANTHROPIC_API_KEY": "sk-ant-x"}, needs_audio=False
    )
    assert isinstance(vision, LiteLLMVisionClient)

    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        scoring_clients(
            ScoringMode.REAL, env={"ANTHROPIC_API_KEY": "sk-ant-x"}, needs_audio=True
        )


def test_real_scoring_uses_the_pipeline_models_and_their_overrides():
    transcriber, vision = scoring_clients(
        ScoringMode.REAL,
        env={
            "ANTHROPIC_API_KEY": "sk-ant-x",
            "OPENAI_API_KEY": "sk-x",
            "VISIO_VISION_MODEL": "anthropic/some-model",
        },
        needs_audio=True,
    )

    assert isinstance(transcriber, LiteLLMTranscriber)
    assert isinstance(vision, LiteLLMVisionClient)
    assert vision._model == "anthropic/some-model"


def test_stub_vision_answers_in_the_scene_schema_and_is_deterministic(tmp_path):
    frames_dir = tmp_path / "20261120_090000_frames"
    frames_dir.mkdir()
    frames = [frames_dir / "frame_01.jpg"]

    first = StubVision().score_frames(frames, "prompt")
    second = StubVision().score_frames(frames, "prompt")

    assert first == second
    scene = parse_scene_response(first)
    assert 3 <= json.loads(first)["score"] <= 9
    assert scene.location in ("indoor", "outdoor")


def test_stub_vision_varies_between_segments_so_the_reel_has_an_order(tmp_path):
    scores = set()
    for minute in range(10):
        frames_dir = tmp_path / f"20261120_09{minute:02d}00_frames"
        frames_dir.mkdir()
        scores.add(
            json.loads(StubVision().score_frames([frames_dir / "f.jpg"], "p"))["score"]
        )

    assert len(scores) > 1


def test_stub_vision_falls_back_when_handed_no_frames():
    scene = parse_scene_response(StubVision().score_frames([], "prompt"))

    assert scene.location == "indoor"


def test_stub_transcriber_reports_some_speech_without_calling_anything():
    result = StubTranscriber().transcribe(Path("anything.wav"))

    assert 0 < result.speech_presence_ratio < 1
    assert result.silence_ratio == pytest.approx(1 - result.speech_presence_ratio)


def test_stub_vision_alternates_locations_through_the_day_it_is_given(tmp_path):
    stems = [f"20261120_09{minute:02d}00" for minute in (5, 6, 12, 19)]
    vision = StubVision(day_stems=list(reversed(stems)))
    locations = []
    for stem in stems:
        frames_dir = tmp_path / f"{stem}_frames"
        frames_dir.mkdir()
        locations.append(
            parse_scene_response(
                vision.score_frames([frames_dir / "f.jpg"], "p")
            ).location
        )

    assert locations == ["indoor", "outdoor", "indoor", "outdoor"]
