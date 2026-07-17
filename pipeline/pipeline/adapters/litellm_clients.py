"""LiteLLM-backed vision and transcription adapters.

All LLM traffic is provider-agnostic via LiteLLM routing: vision scene
scoring defaults to Claude Haiku with ``response_format`` ``json_schema``
structured outputs (the scene Protocol's contract), and transcription
defaults to Whisper with a verbose response so speech timing features can
be derived.
"""

import base64
from pathlib import Path

import litellm

from pipeline.scoring.audio import TranscriptionResult

SCENE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "number", "minimum": 0, "maximum": 10},
        "location": {"type": "string", "enum": ["indoor", "outdoor"]},
        "people": {"type": "boolean"},
    },
    "required": ["score", "location", "people"],
    "additionalProperties": False,
}


def build_vision_request(model: str, frame_paths: list[Path], prompt: str) -> dict:
    content: list[dict] = [{"type": "text", "text": prompt}]
    for frame_path in frame_paths:
        encoded = base64.b64encode(frame_path.read_bytes()).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
            }
        )
    return {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "scene_score",
                "schema": SCENE_RESPONSE_SCHEMA,
                "strict": True,
            },
        },
    }


class LiteLLMVisionClient:
    def __init__(self, model: str):
        self._model = model

    def score_frames(self, frame_paths: list[Path], prompt: str) -> str:
        request = build_vision_request(self._model, frame_paths, prompt)
        response = litellm.completion(**request)
        return response.choices[0].message.content


def transcription_result_from_verbose(verbose: dict) -> TranscriptionResult:
    duration = float(verbose.get("duration") or 0.0)
    if duration <= 0:
        return TranscriptionResult(
            speech_presence_ratio=0.0, silence_ratio=1.0, has_exclamation=False
        )
    speech_sec = sum(
        float(s["end"]) - float(s["start"]) for s in verbose.get("segments", [])
    )
    speech_ratio = min(speech_sec / duration, 1.0)
    return TranscriptionResult(
        speech_presence_ratio=speech_ratio,
        silence_ratio=1.0 - speech_ratio,
        has_exclamation="!" in (verbose.get("text") or ""),
    )


class LiteLLMTranscriber:
    def __init__(self, model: str):
        self._model = model

    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        with open(audio_path, "rb") as audio_file:
            response = litellm.transcription(
                model=self._model, file=audio_file, response_format="verbose_json"
            )
        verbose = response if isinstance(response, dict) else response.model_dump()
        return transcription_result_from_verbose(verbose)
