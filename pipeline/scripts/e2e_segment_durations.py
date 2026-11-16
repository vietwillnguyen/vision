"""End-to-end check of real segment durations through the nightly pipeline.

Run against a local ``supabase start`` stack from the ``pipeline/`` directory:

    eval "$(supabase status -o env)"
    SUPABASE_URL=$API_URL SUPABASE_SERVICE_ROLE_KEY=$SERVICE_ROLE_KEY \\
        uv run --locked python scripts/e2e_segment_durations.py --out <dir>

It builds a device day the way the pendant produces one - raw H.264 from a
lavfi source, muxed with the exact command in firmware's ``muxer.py`` (so the
segments are silent, like the pendant's) - uploads it to the real
``segments`` bucket, runs ``run_nightly`` with the real Supabase store,
ffmpeg media probe and ffmpeg runner, then reports what landed in the
database and in the reel file.

Only the paid LLM calls and the push are stubbed: the transcriber returns
silence, the vision client returns a fixed score with a per-segment location,
and the push client records instead of sending.

The device day:

Segments are 320x180 to keep uploads small; resolution plays no part in
duration handling.

* ``090000`` - a full 300 s segment.
* ``090500`` - a 40 s segment, as a stop press would leave it.
* ``090600`` - a full 300 s segment recorded after the restart.
* ``100000`` - a full 300 s segment later in the day.
* ``FLAG_..._090520`` - a flag press inside the 40 s segment.
* ``FLAG_..._090700`` - a flag press inside the restarted segment.
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import uuid
from datetime import date
from pathlib import Path
from typing import Any, cast

from supabase import create_client

from pipeline.adapters.ffmpeg_media import FfmpegMediaProbe, SubprocessRunner
from pipeline.adapters.supabase_store import SupabaseStore
from pipeline.orchestrator import DeviceRecord, OrchestratorDeps, run_nightly
from pipeline.scoring.audio import TranscriptionResult

DAY = date(2026, 7, 14)
FRAMERATE = 30
# Each source flips between bright and dark every second, so the 1 fps motion
# probe clears MOTION_GATING_THRESHOLD and the stub vision's location is used;
# the chroma differs per segment so their clips are told apart in the reel.
FLICKER = (
    "nullsrc=size=320x180:rate={rate},"
    "geq=lum='if(mod(floor(T),2),220,30)':cb={cb}:cr={cr}"
)
SEGMENTS = [
    # (stem, seconds, chroma (cb, cr), location the stub vision reports)
    ("20260714_090000", 300, (90, 90), "indoor"),
    ("20260714_090500", 40, (200, 60), "outdoor"),
    ("20260714_090600", 300, (60, 200), "indoor"),
    ("20260714_100000", 300, (200, 200), "outdoor"),
]
MARKERS = ["FLAG_20260714_090520.marker", "FLAG_20260714_090700.marker"]


def _run(command: list[str]) -> str:
    return subprocess.run(
        command, check=True, capture_output=True, text=True
    ).stdout.strip()


def make_segment(
    media_dir: Path, stem: str, seconds: int, chroma: tuple[int, int]
) -> Path:
    mp4 = media_dir / f"{stem}.mp4"
    if mp4.exists():
        return mp4
    h264 = media_dir / f"{stem}.h264"
    _run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi",
            "-i", FLICKER.format(rate=FRAMERATE, cb=chroma[0], cr=chroma[1]),
            "-t", str(seconds),
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-f", "h264", str(h264),
        ]
    )  # fmt: skip
    # Identical to firmware/visio_recorder/muxer.py:mux_segment.
    _run(
        [
            "ffmpeg", "-y", "-framerate", str(FRAMERATE),
            "-i", str(h264), "-c", "copy", str(mp4),
        ]
    )  # fmt: skip
    h264.unlink()
    return mp4


def probe(path: Path) -> dict[str, Any]:
    duration = _run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", str(path),
        ]
    )  # fmt: skip
    frames = _run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v", "-count_frames",
            "-show_entries", "stream=nb_read_frames",
            "-of", "default=nw=1:nk=1", str(path),
        ]
    )  # fmt: skip
    return {
        "bytes": path.stat().st_size,
        "duration_sec": float(duration) if duration not in ("", "N/A") else None,
        "frames": int(frames) if frames.isdigit() else 0,
    }


class RecordingMediaProbe(FfmpegMediaProbe):
    """The real probe, also recording how many frames vision is handed."""

    def __init__(self) -> None:
        self.vision_frames: dict[str, int] = {}

    def extract_frames(self, video_path: Path, *args: Any, **kwargs: Any) -> list[Path]:
        frames = super().extract_frames(video_path, *args, **kwargs)
        self.vision_frames[video_path.stem] = len(frames)
        return frames


class SilentTranscriber:
    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        return TranscriptionResult(
            speech_presence_ratio=0.0, silence_ratio=1.0, has_exclamation=False
        )


class StubVision:
    def score_frames(self, frame_paths: list[Path], prompt: str) -> str:
        # With no frames there is nothing to locate; a real model can only
        # guess, so the stub falls back to the pipeline's default location.
        stem = frame_paths[0].parent.name.removesuffix("_frames") if frame_paths else ""
        location = next((s[3] for s in SEGMENTS if s[0] == stem), "indoor")
        return json.dumps({"score": 6, "location": location, "people": False})


class RecordingPush:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, to_token: str, title: str, body: str) -> None:
        self.sent.append((to_token, title, body))


class OneDeviceStore(SupabaseStore):
    """Restricts the run to the harness device, so leftover rows from other
    local runs are not processed alongside it."""

    def __init__(self, client: Any, device_id: str) -> None:
        super().__init__(client)
        self._device_id = device_id

    def list_devices(self) -> list[DeviceRecord]:
        return [d for d in super().list_devices() if d.device_id == self._device_id]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--media-cache", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    out: Path = args.out
    if out.exists():
        shutil.rmtree(out)
    workdir = out / "workdir"
    workdir.mkdir(parents=True)
    media_dir = args.media_cache or out / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    client = create_client(
        os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    )
    run_id = uuid.uuid4().hex[:8]
    user_id = client.auth.admin.create_user(
        {
            "email": f"durations-{run_id}@example.test",
            "password": "correct-horse-battery-staple",
            "email_confirm": True,
        }
    ).user.id
    report: dict[str, Any] = {}
    uploaded: list[str] = []
    try:
        rows = cast(
            list[dict[str, Any]],
            client.table("devices")
            .insert({"user_id": user_id, "name": f"durations e2e {run_id}"})
            .execute()
            .data,
        )
        device_id = str(rows[0]["device_id"])
        bucket = client.storage.from_("segments")

        report["source_segments"] = {}
        for stem, seconds, chroma, _ in SEGMENTS:
            mp4 = make_segment(media_dir, stem, seconds, chroma)
            report["source_segments"][stem] = probe(mp4)
            key = f"{device_id}/{mp4.name}"
            bucket.upload(key, mp4.read_bytes(), {"content-type": "video/mp4"})
            uploaded.append(key)
        for marker in MARKERS:
            key = f"{device_id}/{marker}"
            bucket.upload(key, b"", {"content-type": "application/octet-stream"})
            uploaded.append(key)

        media = RecordingMediaProbe()
        deps = OrchestratorDeps(
            store=OneDeviceStore(client, device_id),
            media=media,
            transcriber=SilentTranscriber(),
            vision=StubVision(),
            push=RecordingPush(),
            runner=SubprocessRunner(),
            workdir=workdir,
        )
        result = run_nightly(deps, DAY)
        report["run"] = {
            "devices_processed": result.devices_processed,
            "devices_failed": len(result.devices_failed),
        }
        report["vision_frames_per_segment"] = media.vision_frames

        segment_rows = (
            client.table("segments")
            .select("recorded_at, duration_sec, manually_flagged, composite_score")
            .eq("device_id", device_id)
            .order("recorded_at")
            .execute()
            .data
        )
        report["segments_rows"] = segment_rows
        reel_rows = cast(
            list[dict[str, Any]],
            client.table("reels")
            .select("s3_key, duration_sec")
            .eq("device_id", device_id)
            .execute()
            .data,
        )
        report["reels_row"] = reel_rows[0] if reel_rows else None
        report["dlq_rows"] = (
            client.table("pipeline_dlq")
            .select("key, kind, attempts")
            .eq("device_id", device_id)
            .execute()
            .data
        )

        device_workdir = workdir / device_id
        report["clips"] = {
            clip.name: probe(clip) for clip in sorted(device_workdir.glob("clip_*.mp4"))
        }
        if report["reels_row"]:
            reel_local = out / "reel.mp4"
            reel_local.write_bytes(
                client.storage.from_("reels").download(report["reels_row"]["s3_key"])
            )
            report["reel_file"] = probe(reel_local)
            reel_local.unlink()
            client.storage.from_("reels").remove([report["reels_row"]["s3_key"]])
    finally:
        if uploaded:
            client.storage.from_("segments").remove(uploaded)
        client.auth.admin.delete_user(user_id)
        shutil.rmtree(workdir, ignore_errors=True)

    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
