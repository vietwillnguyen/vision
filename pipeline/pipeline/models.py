from dataclasses import dataclass
from datetime import datetime

DEFAULT_LOCATION = "indoor"


@dataclass
class ScoreWeights:
    scene_weight: float = 0.4
    # The spec's 0.3. The firmware records an I2S microphone track when one
    # opens and video only otherwise; a segment with no audio stream is scored
    # as silence without a transcription call (audio activity 0), so this
    # weight only counts for segments that carry real audio. Must agree with
    # the column default in
    # supabase/migrations/20261117120000_restore_audio_weight_default.sql.
    audio_weight: float = 0.3
    motion_weight: float = 0.2


@dataclass
class Segment:
    id: str
    device_id: str
    recorded_at: datetime
    # Measured from the file, never the firmware's nominal segment length.
    duration_sec: float
    s3_key: str
    location: str
    composite_score: float = 0.0
    manually_flagged: bool = False
    # Whether the file carries an audio stream (a microphone was recording).
    has_audio: bool = False
