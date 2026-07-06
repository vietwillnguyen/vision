import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class SceneScoreParseError(ValueError):
    pass


@dataclass
class SceneScore:
    novelty: float
    location: str
    people_present: bool


class VisionClient(Protocol):
    def score_frames(self, frame_paths: list[Path], prompt: str) -> str:
        ...


SCENE_SCORING_PROMPT = (
    "Rate the visual interest of this moment on a scale of 1-10.\n"
    "Consider: Is this a new location? Are people present and engaged?\n"
    "Is there an interesting activity? Is this indoors or outdoors?\n"
    'Reply with JSON: {"score": N, "location": "indoor|outdoor", "people": true|false}'
)


def parse_scene_response(raw_json: str) -> SceneScore:
    try:
        data = json.loads(raw_json)
        raw_score = data["score"]
        location = data["location"]
        people = data["people"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise SceneScoreParseError(f"invalid scene scoring response: {raw_json!r}") from exc

    if location not in ("indoor", "outdoor"):
        raise SceneScoreParseError(f"unexpected location value: {location!r}")

    return SceneScore(novelty=raw_score / 10.0, location=location, people_present=bool(people))


def score_scene(client: VisionClient, frame_paths: list[Path]) -> SceneScore:
    raw_json = client.score_frames(frame_paths, SCENE_SCORING_PROMPT)
    return parse_scene_response(raw_json)
