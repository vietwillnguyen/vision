from pathlib import Path

import pytest

from pipeline.scoring.scene import (
    SCENE_SCORING_PROMPT,
    SceneScoreParseError,
    parse_scene_response,
    score_scene,
)


def test_prompt_matches_spec_exactly():
    assert SCENE_SCORING_PROMPT == (
        "Rate the visual interest of this moment on a scale of 1-10.\n"
        "Consider: Is this a new location? Are people present and engaged?\n"
        "Is there an interesting activity? Is this indoors or outdoors?\n"
        'Reply with JSON: {"score": N, "location": "indoor|outdoor", "people": true|false}'
    )


def test_parse_valid_response_normalizes_score_to_0_1():
    result = parse_scene_response('{"score": 8, "location": "outdoor", "people": true}')
    assert result.novelty == 0.8
    assert result.location == "outdoor"
    assert result.people_present is True


def test_parse_invalid_json_raises():
    with pytest.raises(SceneScoreParseError):
        parse_scene_response("not json")


def test_parse_missing_field_raises():
    with pytest.raises(SceneScoreParseError):
        parse_scene_response('{"score": 8, "location": "outdoor"}')


def test_parse_unexpected_location_raises():
    with pytest.raises(SceneScoreParseError):
        parse_scene_response('{"score": 8, "location": "space", "people": false}')


class FakeVisionClient:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[tuple[list[Path], str]] = []

    def score_frames(self, frame_paths: list[Path], prompt: str) -> str:
        self.calls.append((frame_paths, prompt))
        return self.response


def test_score_scene_calls_client_with_spec_prompt_and_parses_result():
    client = FakeVisionClient('{"score": 5, "location": "indoor", "people": false}')
    frame_paths = [Path("/frames/1.jpg"), Path("/frames/2.jpg"), Path("/frames/3.jpg")]

    result = score_scene(client, frame_paths)

    assert result.novelty == 0.5
    assert client.calls == [(frame_paths, SCENE_SCORING_PROMPT)]
