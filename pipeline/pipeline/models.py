from dataclasses import dataclass
from datetime import datetime

DEFAULT_LOCATION = "indoor"


@dataclass
class ScoreWeights:
    scene_weight: float = 0.4
    # 0, not the spec's 0.3, because the firmware records no audio at all:
    # capture.py runs rpicam-vid with no ALSA source, so every segment is
    # silent video and Whisper's output on it is at best a constant and at
    # worst hallucinated speech varying randomly between segments. Restore
    # this to 0.3 (and the matching column default in
    # supabase/migrations/20260807120000_zero_audio_weight_default.sql) once
    # vision-audio-capture lands a microphone.
    audio_weight: float = 0.0
    motion_weight: float = 0.2


@dataclass
class Segment:
    id: str
    device_id: str
    recorded_at: datetime
    duration_sec: int
    s3_key: str
    location: str
    composite_score: float = 0.0
    manually_flagged: bool = False
