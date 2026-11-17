"""Record one segment with the daemon's default config and upload it.

Everything is firmware's own code - ``load_config`` defaults, the recording
loop, the real subprocess runner, ffmpeg mux and the real Supabase storage and
status adapters signed in as a device owner - except the camera: the
``sim-bin/rpicam-vid`` stand-in next to this script is put first on ``PATH``,
and writes H.264 at the bitrate the real encoder targets for the arguments the
firmware passes (see its docstring).

The local stack must enforce the hosted project's 50 MiB per-file limit
(``supabase/config.toml`` ``[storage] file_size_limit = "50MiB"``).

Run from integration/ with SUPABASE_URL, SUPABASE_ANON_KEY and
SUPABASE_SERVICE_ROLE_KEY exported:

    uv run --locked --extra dev python \
        ../docs/superpowers/evidence/segment-upload-limit/e2e_default_segment.py
"""

import dataclasses
import logging
import os
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path

from supabase import create_client
from visio_recorder import daemon
from visio_recorder.daemon import LoopDeps, run_recording_loop
from visio_recorder.led import LedPattern
from visio_recorder.muxer import SubprocessCommandRunner
from visio_recorder.recording_loop import ShutilDiskStatsReader, flush_pending
from visio_recorder.supabase_clients import SupabaseStatus, SupabaseStorage

SIM_BIN = Path(__file__).resolve().parent / "sim-bin"


class StopAfterOneCapture(SubprocessCommandRunner):
    def __init__(self, stop: threading.Event) -> None:
        self._stop = stop

    def run(self, args: list[str]) -> None:
        if args[0] == "ffmpeg":
            args = args[:1] + ["-loglevel", "error"] + args[1:]
        print("$ " + " ".join(args), flush=True)
        super().run(args)
        if args[0] == "rpicam-vid":
            self._stop.set()


class Battery:
    def get_charge_pct(self) -> int:
        return 80


class Led:
    def set(self, color: tuple[int, int, int], pattern: LedPattern) -> None:
        pass


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    os.environ["PATH"] = f"{SIM_BIN}{os.pathsep}{os.environ['PATH']}"
    url = os.environ["SUPABASE_URL"]
    config = daemon.load_config(
        {"SUPABASE_URL": url, "SUPABASE_ANON_KEY": os.environ["SUPABASE_ANON_KEY"]}
    )
    tunables = {
        f.name: getattr(config, f.name)
        for f in dataclasses.fields(config)
        if f.name not in ("supabase_url", "supabase_anon_key", "data_dir")
    }
    print(f"daemon defaults: {tunables}")
    admin = create_client(url, os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    run_id = uuid.uuid4().hex[:8]
    email = f"e2e-limit-{run_id}@example.test"
    password = "correct-horse-battery-staple"
    owner_id = admin.auth.admin.create_user(
        {"email": email, "password": password, "email_confirm": True}
    ).user.id
    device_id = None
    try:
        rows = (
            admin.table("devices")
            .insert({"user_id": owner_id, "name": f"e2e {run_id}"})
            .execute()
            .data
        )
        device_id = str(rows[0]["device_id"])  # type: ignore[index,call-overload]
        owner = create_client(url, os.environ["SUPABASE_ANON_KEY"])
        owner.auth.sign_in_with_password({"email": email, "password": password})

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            queue_dir = data_dir / "queue"
            stop = threading.Event()
            storage = SupabaseStorage(owner)
            fields = {f.name for f in dataclasses.fields(LoopDeps)}
            runner = StopAfterOneCapture(stop)
            # Firmware with the upload-limit guard takes it as a dependency;
            # the same script runs against firmware from before it existed.
            limit_args: list[object] = []
            if "upload_limit" in fields:
                from visio_recorder.upload_limit import (
                    FfprobeDurationProbe,
                    UploadLimit,
                )

                limit = UploadLimit(
                    max_bytes=config.max_upload_bytes,  # type: ignore[attr-defined]
                    runner=runner,
                    probe=FfprobeDurationProbe(),
                )
                tunables["upload_limit"] = limit
                limit_args = [limit]
            deps = LoopDeps(
                command_runner=runner,
                storage_client=storage,
                status_client=SupabaseStatus(owner),
                led_driver=Led(),
                battery_reader=Battery(),
                disk_stats_reader=ShutilDiskStatsReader(),
                data_dir=data_dir,
                queue_dir=queue_dir,
                device_id=device_id,
                **{k: v for k, v in tunables.items() if k in fields},
            )
            run_recording_loop(deps, stop, clock=lambda: datetime(2026, 11, 16, 9, 0))
            # The next boot: flush_pending is the firmware's only retry.
            print("next boot: flush_pending")
            flushed = flush_pending(queue_dir, storage, device_id, *limit_args)
            print(f"  uploaded {flushed} files")
            queued = sorted(queue_dir.glob("*")) if queue_dir.exists() else []
            print("left in upload queue:")
            for path in queued:
                print(f"  {path.name} {path.stat().st_size} bytes")
            print(f"  ({len(queued)} files)")
        listed = admin.storage.from_("segments").list(device_id)
        print("segments bucket:")
        for item in listed:
            print(f"  {item['name']} {item['metadata']['size']} bytes")
        print(f"  ({len(listed)} objects)")
        status = (
            admin.table("device_status")
            .select("segments_pending,segments_uploaded_today,recording_active")
            .eq("device_id", device_id)
            .execute()
            .data
        )
        print(f"device_status: {status}")
        if listed:
            admin.storage.from_("segments").remove(
                [f"{device_id}/{item['name']}" for item in listed]
            )
    finally:
        admin.auth.admin.delete_user(owner_id)


if __name__ == "__main__":
    main()
