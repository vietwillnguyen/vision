"""Nightly pipeline entrypoint: ``python -m pipeline [--day YYYY-MM-DD]``.

Deployed as a GitHub Actions workflow (nightly cron plus workflow_dispatch
for the Epic 5 "trigger the pipeline manually" checklist step). This module
is the single place real adapters are wired together, mirroring the
firmware's ``daemon.py`` convention.
"""

import argparse
import logging
import os
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from supabase import create_client

from pipeline.adapters.expo_push import ExpoPushClient
from pipeline.adapters.ffmpeg_media import FfmpegMediaProbe, SubprocessRunner
from pipeline.adapters.litellm_clients import LiteLLMTranscriber, LiteLLMVisionClient
from pipeline.adapters.supabase_store import SupabaseStore
from pipeline.config import load_config
from pipeline.orchestrator import OrchestratorDeps, run_nightly


def resolve_day(day_arg: str | None, today: date) -> date:
    if day_arg is None:
        return today
    try:
        return datetime.strptime(day_arg, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"--day must be YYYY-MM-DD, got {day_arg!r}") from exc


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Run the Visio nightly reel pipeline.")
    parser.add_argument(
        "--day",
        help="Day to process as YYYY-MM-DD (default: today, UTC).",
    )
    args = parser.parse_args(argv)

    config = load_config(os.environ)
    day = resolve_day(args.day, today=datetime.now(timezone.utc).date())

    client = create_client(config.supabase_url, config.supabase_service_role_key)
    with tempfile.TemporaryDirectory(prefix="visio-pipeline-") as workdir:
        deps = OrchestratorDeps(
            store=SupabaseStore(client),
            media=FfmpegMediaProbe(),
            transcriber=LiteLLMTranscriber(config.transcription_model),
            vision=LiteLLMVisionClient(config.vision_model),
            push=ExpoPushClient(),
            runner=SubprocessRunner(),
            workdir=Path(workdir),
            target_duration_sec=config.target_duration_sec,
            vintage=config.vintage,
        )
        result = run_nightly(deps, day)

    logging.getLogger(__name__).info(
        "nightly run for %s: %d device(s) processed, %d failed",
        day.isoformat(),
        result.devices_processed,
        len(result.devices_failed),
    )
    return 1 if result.devices_failed else 0


if __name__ == "__main__":
    sys.exit(main())
