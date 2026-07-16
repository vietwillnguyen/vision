"""Environment configuration for the nightly pipeline entrypoint."""

from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_VISION_MODEL = "anthropic/claude-haiku-4-5-20251001"
DEFAULT_TRANSCRIPTION_MODEL = "whisper-1"
DEFAULT_TARGET_DURATION_SEC = 90


@dataclass
class Config:
    supabase_url: str
    supabase_service_role_key: str
    vision_model: str = DEFAULT_VISION_MODEL
    transcription_model: str = DEFAULT_TRANSCRIPTION_MODEL
    target_duration_sec: int = DEFAULT_TARGET_DURATION_SEC
    vintage: bool = False


def load_config(env: Mapping[str, str]) -> Config:
    missing = [
        name
        for name in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")
        if not env.get(name)
    ]
    if missing:
        raise ValueError(f"missing required environment variables: {', '.join(missing)}")
    return Config(
        supabase_url=env["SUPABASE_URL"],
        supabase_service_role_key=env["SUPABASE_SERVICE_ROLE_KEY"],
        vision_model=env.get("VISIO_VISION_MODEL", DEFAULT_VISION_MODEL),
        transcription_model=env.get(
            "VISIO_TRANSCRIPTION_MODEL", DEFAULT_TRANSCRIPTION_MODEL
        ),
        target_duration_sec=int(
            env.get("VISIO_TARGET_DURATION_SEC", DEFAULT_TARGET_DURATION_SEC)
        ),
        vintage=env.get("VISIO_VINTAGE", "") in ("1", "true", "yes"),
    )
