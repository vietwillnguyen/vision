"""Scoring clients for the demo seed: stubbed unless real scoring is asked for.

Stub scoring is the default and the only mode tests use. It never touches the
network, even when a provider key happens to be exported in the shell: a paid
run has to be asked for by name (``--scoring real``), and then refuses to start
without the keys it needs rather than failing halfway through a seed.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path

from pipeline.adapters.litellm_clients import LiteLLMTranscriber, LiteLLMVisionClient
from pipeline.config import DEFAULT_TRANSCRIPTION_MODEL, DEFAULT_VISION_MODEL
from pipeline.scoring.audio import TranscriptionClient, TranscriptionResult
from pipeline.scoring.scene import VisionClient

LOCATIONS = ("indoor", "outdoor")


class ScoringMode(StrEnum):
    STUB = "stub"
    REAL = "real"


class StubVision:
    """A scene score per segment that is stable across runs but not uniform.

    The score comes from a hash of the segment's name (its frames directory is
    ``<stem>_frames``), so reseeding yields the same reel, and segments differ
    enough for selection to have an order to work with. Given the day's
    segment stems, locations alternate in time order, so the selector's
    no-two-same-location-clips-in-a-row rule leaves the reel room to fill.
    """

    def __init__(self, day_stems: Sequence[str] = ()) -> None:
        self._order = {stem: index for index, stem in enumerate(sorted(day_stems))}

    def score_frames(self, frame_paths: list[Path], prompt: str) -> str:
        if not frame_paths:
            return json.dumps({"score": 5, "location": "indoor", "people": False})
        stem = frame_paths[0].parent.name.removesuffix("_frames")
        digest = hashlib.sha256(stem.encode()).digest()
        position = self._order.get(stem, digest[1])
        return json.dumps(
            {
                "score": 3 + digest[0] % 7,
                "location": LOCATIONS[position % len(LOCATIONS)],
                "people": False,
            }
        )


class StubTranscriber:
    """Some speech, no exclamation: an unremarkable but not silent clip."""

    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        return TranscriptionResult(
            speech_presence_ratio=0.4, silence_ratio=0.6, has_exclamation=False
        )


def scoring_clients(
    mode: ScoringMode,
    env: Mapping[str, str],
    *,
    needs_audio: bool,
    day_stems: Sequence[str] = (),
) -> tuple[TranscriptionClient, VisionClient]:
    if mode is ScoringMode.STUB:
        return StubTranscriber(), StubVision(day_stems)

    required = ["ANTHROPIC_API_KEY"] + (["OPENAI_API_KEY"] if needs_audio else [])
    missing = [name for name in required if not env.get(name)]
    if missing:
        raise ValueError(
            f"--scoring real needs {', '.join(missing)} in the environment"
            + (" (a clip has sound, so Whisper transcribes it)" if needs_audio else "")
        )
    return (
        LiteLLMTranscriber(
            env.get("VISIO_TRANSCRIPTION_MODEL", DEFAULT_TRANSCRIPTION_MODEL)
        ),
        LiteLLMVisionClient(env.get("VISIO_VISION_MODEL", DEFAULT_VISION_MODEL)),
    )
