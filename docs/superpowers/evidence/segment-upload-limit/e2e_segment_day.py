"""A recorded day, end to end, against storage that enforces the 50 MiB limit.

Firmware half: the daemon's own ``load_config`` defaults, ``run_recording_loop``
with the real subprocess runner, ffmpeg mux, upload-limit guard and the real
Supabase storage and status adapters signed in as a device owner. Only the
camera is simulated: ``sim-bin/rpicam-vid`` is put first on ``PATH`` and writes
H.264 at exactly the bitrate the firmware asks for (x264 held at a constant
bitrate with filler, the worst case the target allows). The clock is simulated
too, so a day's sessions are recorded faster than real time; each capture is
stamped 1 s after the previous one ended, the restart gap a real
``rpicam-vid`` per segment leaves.

The day (2026-11-16): three recording sessions ("boots") of SESSION_SEGMENTS
default-length segments, flag presses placed mid-segment, on a segment's last
second, in the restart gap and on a segment's first second, then the oversize
guard:

* a 30 s capture at 20 Mbps (~75 MB) through ``on_segment_complete`` with the
  default limit: split locally before upload;
* the same capture with ``VISIO_MAX_UPLOAD_BYTES`` raised to 100 MiB (a plan
  upgrade the server has not had): storage answers 413 and the guard splits it;
* a 60 MB file that is not video, queued for the next boot: it cannot be split,
  so it is moved to ``rejected/``, counted in ``device_status.segments_rejected``
  and not retried by a second flush.

Pipeline half: ``run_nightly`` with the real ``SupabaseStore``, ffmpeg media
probe and ffmpeg runner. The paid LLM calls are stubbed because no API keys
are configured here: the transcriber is never called for these silent segments,
and the vision stub returns a per-segment score and alternating location.

Run from integration/ with SUPABASE_URL, SUPABASE_ANON_KEY and
SUPABASE_SERVICE_ROLE_KEY exported, against a stack with this branch's
migrations applied:

    uv run --locked --extra dev python \\
        ../docs/superpowers/evidence/segment-upload-limit/e2e_segment_day.py \\
        --out <workdir>
"""

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from pipeline.adapters.ffmpeg_media import FfmpegMediaProbe, SubprocessRunner
from pipeline.adapters.supabase_store import SupabaseStore
from pipeline.orchestrator import DeviceRecord, OrchestratorDeps, run_nightly
from pipeline.scoring.audio import TranscriptionResult
from supabase import create_client
from visio_recorder import daemon
from visio_recorder.capture import VideoSettings, record_segment
from visio_recorder.daemon import LoopDeps, run_recording_loop
from visio_recorder.flag_button import write_flag_marker
from visio_recorder.led import LedPattern
from visio_recorder.muxer import SubprocessCommandRunner
from visio_recorder.recording_loop import (
    ShutilDiskStatsReader,
    build_device_status,
    flush_pending,
    on_segment_complete,
)
from visio_recorder.supabase_clients import SupabaseStatus, SupabaseStorage
from visio_recorder.upload_limit import FfprobeDurationProbe, UploadLimit

SIM_BIN = Path(__file__).resolve().parent / "sim-bin"
DAY = date(2026, 11, 16)
SESSION_SEGMENTS = int(os.environ.get("E2E_SESSION_SEGMENTS", "16"))
SESSIONS = [datetime(2026, 11, 16, 8, 30), datetime(2026, 11, 16, 12, 15),
            datetime(2026, 11, 16, 18, 40)]  # fmt: skip
RESTART_GAP = timedelta(seconds=1)


class CountingRunner(SubprocessCommandRunner):
    """The real runner; stops the loop after ``limit`` captures."""

    def __init__(self, stop: threading.Event, limit: int) -> None:
        self._stop = stop
        self._limit = limit
        self.captures = 0

    def run(self, args: list[str]) -> None:
        if args[0] == "ffmpeg" and "-loglevel" not in args:
            args = args[:1] + ["-loglevel", "error"] + args[1:]
        super().run(args)
        if args[0] == "rpicam-vid":
            self.captures += 1
            if self.captures == 1:
                print("  $ " + " ".join(args), flush=True)
            if self.captures >= self._limit:
                self._stop.set()


class SessionClock:
    """Stamps each capture one restart gap after the previous one ended."""

    def __init__(self, start: datetime, segment_ms: int) -> None:
        self._next = start
        self._step = timedelta(milliseconds=segment_ms) + RESTART_GAP

    def __call__(self) -> datetime:
        value = self._next
        self._next += self._step
        return value


class Battery:
    def get_charge_pct(self) -> int:
        return 80


class Led:
    def set(self, color: tuple[int, int, int], pattern: LedPattern) -> None:
        pass


class NeverCalledTranscriber:
    def __init__(self) -> None:
        self.calls = 0

    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        self.calls += 1
        return TranscriptionResult(
            speech_presence_ratio=0.0, silence_ratio=1.0, has_exclamation=False
        )


class StubVision:
    def __init__(self) -> None:
        self.calls = 0

    def score_frames(self, frame_paths: list[Path], prompt: str) -> str:
        self.calls += 1
        stem = frame_paths[0].parent.name if frame_paths else ""
        digest = hashlib.sha256(stem.encode()).digest()
        return json.dumps(
            {
                "score": 1 + digest[0] % 10,
                "location": "indoor" if digest[1] % 2 else "outdoor",
                "people": False,
            }
        )


class RecordingPush:
    def send(self, to_token: str, title: str, body: str) -> None:
        pass


class OneDeviceStore(SupabaseStore):
    def __init__(self, client: Any, device_id: str) -> None:
        super().__init__(client)
        self._device_id = device_id

    def list_devices(self) -> list[DeviceRecord]:
        return [d for d in super().list_devices() if d.device_id == self._device_id]


def _ffprobe(path: Path, entries: str) -> str:
    return subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", entries,
         "-of", "default=nw=1:nk=1", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s %(name)s %(message)s"
    )
    os.environ["PATH"] = f"{SIM_BIN}{os.pathsep}{os.environ['PATH']}"
    out: Path = args.out
    shutil.rmtree(out, ignore_errors=True)
    data_dir = out / "device"
    queue_dir = data_dir / "queue"
    workdir = out / "pipeline"
    workdir.mkdir(parents=True)
    data_dir.mkdir(parents=True)  # main() creates it before anything else

    url = os.environ["SUPABASE_URL"]
    anon = os.environ["SUPABASE_ANON_KEY"]
    config = daemon.load_config({"SUPABASE_URL": url, "SUPABASE_ANON_KEY": anon})
    print(
        f"daemon defaults: video={config.video} "
        f"segment_duration_ms={config.segment_duration_ms} "
        f"max_upload_bytes={config.max_upload_bytes}"
    )
    admin = create_client(url, os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    run_id = uuid.uuid4().hex[:8]
    email = f"e2e-day-{run_id}@example.test"
    password = "correct-horse-battery-staple"
    owner_id = admin.auth.admin.create_user(
        {"email": email, "password": password, "email_confirm": True}
    ).user.id
    report: dict[str, Any] = {}
    device_id = ""
    try:
        rows = cast(
            list[dict[str, Any]],
            admin.table("devices")
            .insert({"user_id": owner_id, "name": f"e2e day {run_id}"})
            .execute()
            .data,
        )
        device_id = str(rows[0]["device_id"])
        owner = create_client(url, anon)
        owner.auth.sign_in_with_password({"email": email, "password": password})
        storage = SupabaseStorage(owner)
        status = SupabaseStatus(owner)
        probe = FfprobeDurationProbe()

        def limit_for(max_bytes: int, runner: SubprocessCommandRunner) -> UploadLimit:
            return UploadLimit(max_bytes=max_bytes, runner=runner, probe=probe)

        # -- recording sessions -------------------------------------------
        captured: list[str] = []
        for start in SESSIONS:
            stop = threading.Event()
            runner = CountingRunner(stop, SESSION_SEGMENTS)
            deps = LoopDeps(
                command_runner=runner,
                storage_client=storage,
                status_client=status,
                led_driver=Led(),
                battery_reader=Battery(),
                disk_stats_reader=ShutilDiskStatsReader(),
                data_dir=data_dir,
                queue_dir=queue_dir,
                device_id=device_id,
                segment_duration_ms=config.segment_duration_ms,
                video=config.video,
                upload_limit=limit_for(config.max_upload_bytes, runner),
            )
            print(f"session from {start:%H:%M:%S}: {SESSION_SEGMENTS} segments")
            clock = SessionClock(start, config.segment_duration_ms)
            stamps: list[datetime] = []

            def recording_clock(
                clock: Callable[[], datetime] = clock,
                stamps: list[datetime] = stamps,
            ) -> datetime:
                stamp = clock()
                stamps.append(stamp)
                return stamp

            run_recording_loop(deps, stop, clock=recording_clock)
            captured += [f"{s:%Y%m%d_%H%M%S}.mp4" for s in stamps]

        # -- flag presses near segment boundaries -------------------------
        seg_sec = config.segment_duration_ms / 1000
        s1 = SESSIONS[0]
        second_start = s1 + timedelta(seconds=seg_sec) + RESTART_GAP
        third_start = second_start + timedelta(seconds=seg_sec) + RESTART_GAP
        presses = {
            "mid_segment": s1 + timedelta(seconds=seg_sec / 2),
            "last_second_of_segment": second_start + timedelta(seconds=seg_sec - 1),
            "restart_gap": second_start + timedelta(seconds=seg_sec),
            "first_second_of_segment": third_start,
        }
        for pressed_at in presses.values():
            write_flag_marker(queue_dir, pressed_at)
        report["flag_presses"] = {k: f"{v:%H:%M:%S}" for k, v in presses.items()}

        # -- oversize guard -----------------------------------------------
        guard: dict[str, Any] = {}
        hot = VideoSettings(bitrate_bps=20_000_000)
        for label, stamp, max_bytes in (
            ("local_split", datetime(2026, 11, 16, 20, 0, 0), config.max_upload_bytes),
            ("storage_413_resplit", datetime(2026, 11, 16, 20, 5, 0), 100 * 1024**2),
        ):
            runner = CountingRunner(threading.Event(), 10**9)
            h264 = record_segment(runner, data_dir, stamp, 30_000, hot)
            h264_size = h264.stat().st_size
            result = on_segment_complete(
                h264_path=h264,
                queue_dir=queue_dir,
                command_runner=runner,
                storage_client=storage,
                status_client=status,
                led_driver=Led(),
                battery_reader=Battery(),
                device_id=device_id,
                segments_uploaded_today=0,
                framerate=hot.framerate,
                disk_stats_reader=ShutilDiskStatsReader(),
                data_dir=data_dir,
                upload_limit=limit_for(max_bytes, runner),
            )
            guard[label] = {
                "capture_bytes": h264_size,
                "max_upload_bytes": max_bytes,
                "uploaded_files": result.segments_uploaded_today,
                "upload_ok": result.upload_ok,
            }
        junk = queue_dir / "20261116_210000.mp4"
        junk.write_bytes(os.urandom(60_000_000))
        runner = SubprocessCommandRunner()
        first = flush_pending(
            queue_dir, storage, device_id, limit_for(config.max_upload_bytes, runner)
        )
        second = flush_pending(
            queue_dir, storage, device_id, limit_for(config.max_upload_bytes, runner)
        )
        # What the next boot's loop reports before its first capture.
        status.upsert_device_status(
            asdict(
                build_device_status(
                    device_id=device_id,
                    battery_pct=80,
                    disk_stats_reader=ShutilDiskStatsReader(),
                    data_dir=data_dir,
                    queue_dir=queue_dir,
                    segments_uploaded_today=0,
                    recording_active=True,
                )
            )
        )
        guard["unsplittable"] = {
            "flush_uploaded_first_boot": first,
            "flush_uploaded_second_boot": second,
            "rejected_dir": sorted(p.name for p in (data_dir / "rejected").iterdir()),
        }
        report["oversize_guard"] = guard
        report["left_in_queue"] = sorted(p.name for p in queue_dir.iterdir())
        report["device_status"] = (
            admin.table("device_status")
            .select("segments_pending,segments_uploaded_today,segments_rejected")
            .eq("device_id", device_id)
            .execute()
            .data
        )

        # -- what storage holds -------------------------------------------
        objects = SupabaseStore(admin)._list_bucket_names(device_id)
        sizes = {}
        for name in objects:
            info = admin.storage.from_("segments").info(f"{device_id}/{name}")
            sizes[name] = int(info.get("size") or 0)
        segment_sizes = [v for k, v in sizes.items() if k.endswith(".mp4")]
        report["storage"] = {
            "objects": len(objects),
            "segments": len(segment_sizes),
            "markers": sum(1 for k in objects if k.endswith(".marker")),
            "captured_by_loop": len(captured),
            "every_loop_capture_uploaded": all(c in sizes for c in captured),
            "largest_segment_bytes": max(segment_sizes),
            "smallest_full_segment_bytes": min(sizes[c] for c in captured),
            "limit_bytes": 50 * 1024**2,
            "guard_parts": sorted(
                f"{k} {v}" for k, v in sizes.items() if k.startswith("20261116_20")
            ),
        }
        sample = data_dir / "sample.mp4"
        sample.write_bytes(
            admin.storage.from_("segments").download(f"{device_id}/{captured[0]}")
        )
        report["sample_segment"] = {
            "name": captured[0],
            "bytes": sample.stat().st_size,
            "duration_sec": float(_ffprobe(sample, "format=duration")),
            "bit_rate": int(_ffprobe(sample, "format=bit_rate")),
            "video": _ffprobe(sample, "stream=codec_name,width,height,r_frame_rate"),
        }
        sample.unlink()

        # -- nightly pipeline ---------------------------------------------
        vision = StubVision()
        transcriber = NeverCalledTranscriber()
        result = run_nightly(
            OrchestratorDeps(
                store=OneDeviceStore(admin, device_id),
                media=FfmpegMediaProbe(),
                transcriber=transcriber,
                vision=vision,
                push=RecordingPush(),
                runner=SubprocessRunner(),
                workdir=workdir,
            ),
            DAY,
        )
        report["pipeline"] = {
            "devices_processed": result.devices_processed,
            "devices_failed": len(result.devices_failed),
            "vision_calls": vision.calls,
            "transcriber_calls": transcriber.calls,
        }
        seg_rows = cast(
            list[dict[str, Any]],
            admin.table("segments")
            .select("s3_key,recorded_at,duration_sec,manually_flagged")
            .eq("device_id", device_id)
            .order("recorded_at")
            .execute()
            .data,
        )
        report["segments_rows"] = {
            "count": len(seg_rows),
            "durations": sorted({r["duration_sec"] for r in seg_rows}),
            "flagged": [
                r["s3_key"].split("/")[-1] for r in seg_rows if r["manually_flagged"]
            ],
        }
        report["dlq_rows"] = (
            admin.table("pipeline_dlq")
            .select("key,kind,attempts")
            .eq("device_id", device_id)
            .execute()
            .data
        )
        reels = cast(
            list[dict[str, Any]],
            admin.table("reels")
            .select("s3_key,duration_sec")
            .eq("device_id", device_id)
            .execute()
            .data,
        )
        report["reel_row"] = reels[0] if reels else None
        if reels:
            reel = out / "reel.mp4"
            reel.write_bytes(admin.storage.from_("reels").download(reels[0]["s3_key"]))
            report["reel_file"] = {
                "bytes": reel.stat().st_size,
                "duration_sec": float(_ffprobe(reel, "format=duration")),
                "video": _ffprobe(reel, "stream=codec_name,width,height"),
            }
            admin.storage.from_("reels").remove([reels[0]["s3_key"]])
    finally:
        if device_id:
            names = SupabaseStore(admin)._list_bucket_names(device_id)
            for i in range(0, len(names), 100):
                admin.storage.from_("segments").remove(
                    [f"{device_id}/{n}" for n in names[i : i + 100]]
                )
        admin.auth.admin.delete_user(owner_id)
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(data_dir, ignore_errors=True)

    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
