import pytest

from pipeline.scoring.audio import TranscriptionResult, compute_audio_activity


def test_full_speech_no_silence_scores_full_activity():
    result = TranscriptionResult(speech_presence_ratio=1.0, silence_ratio=0.0, has_exclamation=False)
    assert compute_audio_activity(result) == 1.0


def test_no_speech_all_silence_scores_zero():
    result = TranscriptionResult(speech_presence_ratio=0.0, silence_ratio=1.0, has_exclamation=False)
    assert compute_audio_activity(result) == 0.0


def test_exclamation_adds_bonus():
    result = TranscriptionResult(speech_presence_ratio=0.5, silence_ratio=0.5, has_exclamation=True)
    assert compute_audio_activity(result) == pytest.approx(0.7)


def test_score_is_clamped_to_one():
    result = TranscriptionResult(speech_presence_ratio=1.0, silence_ratio=0.0, has_exclamation=True)
    assert compute_audio_activity(result) == 1.0
