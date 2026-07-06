from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class TranscriptionResult:
    speech_presence_ratio: float
    silence_ratio: float
    has_exclamation: bool


class TranscriptionClient(Protocol):
    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        ...


def compute_audio_activity(result: TranscriptionResult) -> float:
    score = 0.6 * result.speech_presence_ratio + 0.4 * (1 - result.silence_ratio)
    if result.has_exclamation:
        score += 0.2
    return min(score, 1.0)
