"""Audio capture end to end: two pendants, one with a microphone, one without.

Firmware half, for each pendant: the daemon's own ``load_config`` defaults,
``resolve_audio`` (a real ffmpeg ALSA open of ``VISIO_AUDIO_DEVICE``), then
``run_recording_loop`` with the real subprocess runner, ffmpeg remux, upload
guard and the real Supabase storage and status adapters signed in as the owner.

* ``with-mic``: ALSA is given a ``visio_mic`` PCM (the ALSA ``file`` plugin over
  ``null``), so the device really opens. ``sim-bin/rpicam-vid`` then writes what
  ``rpicam-vid --codec libav --libav-format mpegts --libav-audio`` would: CBR
  H.264 plus mono 48 kHz AAC at the requested bitrate, with the microphone's
  samples taken from a synthetic speech track (ffmpeg's ``flite`` voice).
* ``mic-drops-out``: as ``with-mic``, but the microphone disappears after the
  second capture (``SIM_MIC_GONE_FILE``): rpicam-vid exits non-zero and the
  loop carries on video only, so the day mixes segments with and without
  audio, and so does its reel.
* ``no-mic``: ALSA is given the repo's real ``firmware/alsa/visio-mic.conf``.
  There is no ``adau7002`` card on this host, exactly as on a pendant whose
  microphone is not wired, so the probe fails and the pendant records video
  only.

Pipeline half: ``run_nightly`` with the real ``SupabaseStore``, ffmpeg media
probe, ffmpeg runner and the real ``LiteLLMTranscriber`` (``whisper-1``). No
OpenAI key is configured here, so LiteLLM is pointed through
``OPENAI_API_BASE`` at ``local_whisper_server.py``, which answers OpenAI's
transcription API with a real Whisper model (faster-whisper ``base.en``). The
vision LLM is stubbed (no Anthropic key): it scores a segment by its time of
day, identically for both pendants, so the audio term is the only difference
between their scores.

Run from integration/ with SUPABASE_URL, SUPABASE_ANON_KEY,
SUPABASE_SERVICE_ROLE_KEY, OPENAI_API_BASE and OPENAI_API_KEY (any value)
exported, against a stack with this branch's migrations applied:

    uv run --locked --extra dev python \\
        ../docs/superpowers/evidence/audio-capture/e2e_audio_day.py --out <dir>
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import threading
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from pipeline.adapters.ffmpeg_media import FfmpegMediaProbe, SubprocessRunner
from pipeline.adapters.litellm_clients import LiteLLMTranscriber
from pipeline.adapters.supabase_store import SupabaseStore
from pipeline.orchestrator import DeviceRecord, OrchestratorDeps, run_nightly
from pipeline.scoring.audio import TranscriptionResult
from supabase import create_client
from visio_recorder import daemon
from visio_recorder.daemon import LoopDeps, resolve_audio, run_recording_loop
from visio_recorder.led import LedPattern
from visio_recorder.muxer import SubprocessCommandRunner
from visio_recorder.recording_loop import ShutilDiskStatsReader
from visio_recorder.supabase_clients import SupabaseStatus, SupabaseStorage
from visio_recorder.upload_limit import FfprobeDurationProbe, UploadLimit

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
SIM_BIN = REPO / "docs/superpowers/evidence/segment-upload-limit/sim-bin"
REAL_ALSA_CONF = REPO / "firmware/alsa/visio-mic.conf"
SYSTEM_ALSA_CONF = "/usr/share/alsa/alsa.conf"
DAY = date(2026, 11, 16)
SEGMENTS = int(os.environ.get("E2E_SEGMENTS", "4"))
START = datetime(2026, 11, 16, 10, 0)
SPEECH = [
    "Good morning. We are at the market buying fresh mangoes.",
    "Look at that! The boat is full of flowers.",
    "Let us get some coffee before the rain starts.",
]


class CountingRunner(SubprocessCommandRunner):
    def __init__(
        self, stop: threading.Event, limit: int, mic_gone_after: int | None = None
    ) -> None:
        self._stop = stop
        self._limit = limit
        self._mic_gone_after = mic_gone_after
        self.captures = 0

    def run(self, args: list[str]) -> None:
        if args[0] == "ffmpeg" and "-loglevel" not in args:
            args = args[:1] + ["-loglevel", "error"] + args[1:]
        super().run(args)
        if args[0] == "rpicam-vid":
            self.captures += 1
            if self.captures == 1:
                print("  $ " + " ".join(args), flush=True)
            if self.captures == self._mic_gone_after:
                Path(os.environ["SIM_MIC_GONE_FILE"]).touch()
                print("  (microphone unplugged)", flush=True)
            if self.captures >= self._limit:
                self._stop.set()


class Clock:
    def __init__(self, segment_ms: int) -> None:
        self._next = START
        self._step = timedelta(milliseconds=segment_ms, seconds=1)

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


class RecordingTranscriber(LiteLLMTranscriber):
    def __init__(self, model: str) -> None:
        super().__init__(model)
        self.results: dict[str, TranscriptionResult] = {}

    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        result = super().transcribe(audio_path)
        self.results[f"{audio_path.parent.name}/{audio_path.stem}"] = result
        return result


class TimeOfDayVision:
    def score_frames(self, frame_paths: list[Path], prompt: str) -> str:
        stem = frame_paths[0].parent.name if frame_paths else ""
        minute = int(stem.split("_")[1][2:4]) if "_" in stem else 0
        return json.dumps(
            {
                "score": 5 + minute % 3,
                "location": "indoor" if minute % 2 else "outdoor",
                "people": True,
            }
        )


class Push:
    def send(self, to_token: str, title: str, body: str) -> None:
        pass


class TheseDevicesStore(SupabaseStore):
    def __init__(self, client: Any, device_ids: set[str]) -> None:
        super().__init__(client)
        self._ids = device_ids

    def list_devices(self) -> list[DeviceRecord]:
        return [d for d in super().list_devices() if d.device_id in self._ids]


def _run(cmd: list[str]) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def make_speech_track(path: Path) -> None:
    """About 30 s of synthetic speech with pauses, looped by the sim mic."""
    parts = []
    for i, line in enumerate(SPEECH):
        part = path.parent / f"speech_{i}.wav"
        _run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
              "-i", f"flite=text='{line}':voice=slt",
              "-af", "apad=pad_dur=5", "-ar", "48000", "-ac", "1",
              str(part)])  # fmt: skip
        parts.append(part)
    inputs = [arg for p in parts for arg in ("-i", str(p))]
    _run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex",
          f"concat=n={len(parts)}:v=0:a=1", str(path)])  # fmt: skip


def streams(path: Path) -> list[str]:
    out = _run(["ffprobe", "-v", "error", "-show_entries",
                "stream=codec_type,codec_name,sample_rate,channels,width,height",
                "-of", "compact=p=0:nk=1", str(path)])  # fmt: skip
    return [line for line in out.splitlines() if line.strip()]


def audio_coverage(path: Path) -> dict[str, float] | None:
    """Where the reel's audio packets start and end, and the largest gap."""
    pts = [
        float(x)
        for x in _run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "packet=pts_time",
                "-of",
                "csv=p=0",
                str(path),
            ]
        ).split()  # fmt: skip
        if x.strip()
    ]
    if not pts:
        return None
    gaps = [b - a for a, b in zip(pts, pts[1:], strict=False)]
    return {
        "first_s": round(pts[0], 3),
        "last_s": round(pts[-1], 3),
        "largest_gap_s": round(max(gaps, default=0.0), 3),
    }


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
    out.mkdir(parents=True)
    speech = out / "speech.wav"
    make_speech_track(speech)
    os.environ["SIM_MIC_WAV"] = str(speech)
    sim_conf = out / "sim-visio-mic.conf"
    sim_conf.write_text(
        'pcm.visio_mic {\n  type file\n  slave.pcm "null"\n'
        '  file "/dev/null"\n  format "raw"\n}\n'
    )

    url = os.environ["SUPABASE_URL"]
    anon = os.environ["SUPABASE_ANON_KEY"]
    config = daemon.load_config({"SUPABASE_URL": url, "SUPABASE_ANON_KEY": anon})
    print(
        f"daemon defaults: video={config.video} audio={config.audio} "
        f"segment_duration_ms={config.segment_duration_ms}"
    )
    admin = create_client(url, os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    run_id = uuid.uuid4().hex[:8]
    email = f"e2e-audio-{run_id}@example.test"
    password = "correct-horse-battery-staple"
    owner_id = admin.auth.admin.create_user(
        {"email": email, "password": password, "email_confirm": True}
    ).user.id
    report: dict[str, Any] = {}
    devices: dict[str, str] = {}
    try:
        owner = create_client(url, anon)
        owner.auth.sign_in_with_password({"email": email, "password": password})
        storage = SupabaseStorage(owner)
        status = SupabaseStatus(owner)
        os.environ["SIM_MIC_GONE_FILE"] = str(out / "mic-gone")
        for label, alsa_conf, gone_after in (
            ("with-mic", sim_conf, None),
            ("mic-drops-out", sim_conf, 2),
            ("no-mic", REAL_ALSA_CONF, None),
        ):
            rows = cast(
                list[dict[str, Any]],
                admin.table("devices")
                .insert({"user_id": owner_id, "name": f"e2e {label} {run_id}"})
                .execute()
                .data,
            )
            device_id = str(rows[0]["device_id"])
            devices[label] = device_id
            os.environ["ALSA_CONFIG_PATH"] = f"{SYSTEM_ALSA_CONF}:{alsa_conf}"
            print(f"{label}: ALSA config {alsa_conf.name}")
            stop = threading.Event()
            runner = CountingRunner(stop, SEGMENTS, gone_after)
            audio = resolve_audio(runner, config.audio)
            print(f"  resolve_audio -> {audio}")
            data_dir = out / label
            data_dir.mkdir()
            deps = LoopDeps(
                command_runner=runner,
                storage_client=storage,
                status_client=status,
                led_driver=Led(),
                battery_reader=Battery(),
                disk_stats_reader=ShutilDiskStatsReader(),
                data_dir=data_dir,
                queue_dir=data_dir / "queue",
                device_id=device_id,
                segment_duration_ms=config.segment_duration_ms,
                video=config.video,
                upload_limit=UploadLimit(
                    max_bytes=config.max_upload_bytes,
                    runner=runner,
                    probe=FfprobeDurationProbe(),
                ),
                audio=audio,
            )
            run_recording_loop(deps, stop, clock=Clock(config.segment_duration_ms))
            names = sorted(
                item["name"] for item in admin.storage.from_("segments").list(device_id)
            )
            sample = data_dir / "sample.mp4"
            sample.write_bytes(
                admin.storage.from_("segments").download(f"{device_id}/{names[0]}")
            )
            report[label] = {
                "resolved_audio": str(audio),
                "uploaded": {
                    n: int(
                        admin.storage.from_("segments")
                        .info(f"{device_id}/{n}")
                        .get("size")
                        or 0
                    )
                    for n in names
                },
                "sample_streams": streams(sample),
                "left_in_queue": sorted(p.name for p in (data_dir / "queue").iterdir()),
                "device_status": admin.table("device_status")
                .select("recording_active,audio_active,segments_uploaded_today")
                .eq("device_id", device_id)
                .execute()
                .data,
            }
        os.environ.pop("ALSA_CONFIG_PATH", None)

        transcriber = RecordingTranscriber("whisper-1")
        workdir = out / "pipeline"
        result = run_nightly(
            OrchestratorDeps(
                store=TheseDevicesStore(admin, set(devices.values())),
                media=FfmpegMediaProbe(),
                transcriber=transcriber,
                vision=TimeOfDayVision(),
                push=Push(),
                runner=SubprocessRunner(),
                workdir=workdir,
            ),
            DAY,
        )
        report["pipeline"] = {
            "devices_processed": result.devices_processed,
            "devices_failed": len(result.devices_failed),
            "transcriptions": {
                key: vars(value) for key, value in sorted(transcriber.results.items())
            },
        }
        for label, device_id in devices.items():
            seg_rows = cast(
                list[dict[str, Any]],
                admin.table("segments")
                .select("s3_key,duration_sec,composite_score")
                .eq("device_id", device_id)
                .order("recorded_at")
                .execute()
                .data,
            )
            reels = cast(
                list[dict[str, Any]],
                admin.table("reels")
                .select("s3_key,duration_sec")
                .eq("device_id", device_id)
                .execute()
                .data,
            )
            entry: dict[str, Any] = {
                "segments": {
                    r["s3_key"].split("/")[-1]: round(r["composite_score"], 4)
                    for r in seg_rows
                },
                "reel_row": reels[0] if reels else None,
            }
            if reels:
                reel = out / f"reel-{label}.mp4"
                reel.write_bytes(
                    admin.storage.from_("reels").download(reels[0]["s3_key"])
                )
                entry["reel_streams"] = streams(reel)
                entry["reel_audio"] = audio_coverage(reel)
                admin.storage.from_("reels").remove([reels[0]["s3_key"]])
            report["pipeline"][label] = entry
    finally:
        for device_id in devices.values():
            names = [
                f"{device_id}/{i['name']}"
                for i in admin.storage.from_("segments").list(device_id)
            ]
            if names:
                admin.storage.from_("segments").remove(names)
        admin.auth.admin.delete_user(owner_id)
        for label in ("with-mic", "mic-drops-out", "no-mic", "pipeline"):
            shutil.rmtree(out / label, ignore_errors=True)

    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
