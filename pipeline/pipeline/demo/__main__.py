"""Demo seed entrypoint: ``python -m pipeline.demo (--clips DIR | --placeholder)``.

Needs ``SUPABASE_URL`` and ``SUPABASE_SERVICE_ROLE_KEY``, like the nightly
pipeline, and the demo password in ``VISIO_DEMO_PASSWORD`` (or ``--password``).
That password ships in the public web build by design: the account is
read-only in the database, so knowing it grants nothing but a look.

Scoring is stubbed unless ``--scoring real`` is passed; see
``pipeline.demo.scoring``.
"""

import argparse
import json
import logging
import os
import sys
import tempfile
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from supabase import create_client

from pipeline.adapters.ffmpeg_media import SubprocessRunner
from pipeline.demo.footage import load_source_clips, plan_day, render_day
from pipeline.demo.placeholder import generate_placeholder_clips
from pipeline.demo.scoring import ScoringMode, scoring_clients
from pipeline.demo.seed import DEFAULT_DEMO_EMAIL, DEFAULT_DEVICE_NAME, seed_demo

DEFAULT_START = time(9, 0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.demo",
        description="Seed the read-only demo account with a device day and its reel.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--clips",
        type=Path,
        help="Directory of video clips, with an optional demo.toml for start "
        "times and flag presses.",
    )
    source.add_argument(
        "--placeholder",
        action="store_true",
        help="Generate clearly synthetic placeholder clips with FFmpeg instead.",
    )
    parser.add_argument(
        "--day",
        type=date.fromisoformat,
        help="Day to record the footage on, YYYY-MM-DD (default: yesterday, UTC).",
    )
    parser.add_argument(
        "--scoring",
        type=ScoringMode,
        choices=list(ScoringMode),
        default=ScoringMode.STUB,
        help="stub (default, free) or real (Claude Haiku + Whisper via LiteLLM, "
        "needs ANTHROPIC_API_KEY and, for clips with sound, OPENAI_API_KEY).",
    )
    parser.add_argument("--email", default=DEFAULT_DEMO_EMAIL)
    parser.add_argument(
        "--password",
        default=os.environ.get("VISIO_DEMO_PASSWORD"),
        help="Demo account password (default: $VISIO_DEMO_PASSWORD).",
    )
    parser.add_argument("--device-name", default=DEFAULT_DEVICE_NAME)
    parser.add_argument(
        "--keep-media",
        type=Path,
        help="Keep the source clips and rendered segments here instead of a temp dir.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    if not args.password:
        print("error: set VISIO_DEMO_PASSWORD or pass --password", file=sys.stderr)
        return 2
    missing = [
        n
        for n in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")
        if not os.environ.get(n)
    ]
    if missing:
        print(f"error: missing {', '.join(missing)}", file=sys.stderr)
        return 2
    day: date = args.day or datetime.now(UTC).date() - timedelta(days=1)

    with tempfile.TemporaryDirectory(prefix="visio-demo-") as tmp:
        media = args.keep_media or Path(tmp)
        runner = SubprocessRunner()
        clips_dir = (
            generate_placeholder_clips(runner, media / "clips")
            if args.placeholder
            else args.clips
        )
        sources, manifest = load_source_clips(clips_dir)
        plan = plan_day(
            sources,
            day,
            default_start=manifest.start or DEFAULT_START,
            gap_sec=manifest.gap_sec,
        )
        # Fail on a missing key before anything is rendered or deleted.
        transcriber, vision = scoring_clients(
            args.scoring,
            os.environ,
            needs_audio=any(s.has_audio for s in plan.segments),
            day_stems=[s.filename.removesuffix(".mp4") for s in plan.segments],
        )
        segment_paths = render_day(runner, plan, media / "segments")
        workdir = Path(tmp) / "pipeline"
        workdir.mkdir()

        client = create_client(
            os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        )
        result = seed_demo(
            client,
            email=args.email,
            password=args.password,
            day=day,
            plan=plan,
            segment_paths=segment_paths,
            transcriber=transcriber,
            vision=vision,
            workdir=workdir,
            device_name=args.device_name,
        )

    print(
        json.dumps(
            {**asdict(result), "scoring": str(args.scoring)}, indent=2, default=str
        )
    )
    print(
        "\nApp build config (app/.env, public by design):\n"
        f"EXPO_PUBLIC_DEMO_EMAIL={args.email}\n"
        "EXPO_PUBLIC_DEMO_PASSWORD=<the password you seeded with>"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
