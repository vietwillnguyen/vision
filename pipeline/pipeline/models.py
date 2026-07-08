from dataclasses import dataclass
from datetime import datetime

DEFAULT_LOCATION = "indoor"


@dataclass
class ScoreWeights:
    scene_weight: float = 0.4
    audio_weight: float = 0.3
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
