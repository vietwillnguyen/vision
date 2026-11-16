"""End-to-end reproduction of an unclean stop against local Supabase.

Hardware is simulated with real processes: an ffmpeg process writing a raw
H.264 stream stands in for rpicam-vid, and it is SIGTERMed mid-capture the way
systemd's default KillMode=control-group does on `systemctl stop`. Everything
else is firmware's own code: run_recording_loop, the real subprocess runner
and ffmpeg mux, and the real Supabase status and storage adapters signed in as
a device owner.

Then the "next boot" runs what main() runs before recording: orphan recovery
(when this firmware has it) and flush_pending.

Run from integration/ with SUPABASE_URL, SUPABASE_ANON_KEY and
SUPABASE_SERVICE_ROLE_KEY exported:

    uv run --locked --extra dev python \
        ../docs/superpowers/evidence/firmware-recording-lifecycle/e2e_unclean_stop.py
"""

import os
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from supabase import create_client
from visio_recorder import recording_loop
from visio_recorder.daemon import LoopDeps, run_recording_loop
from visio_recorder.led import LedPattern
from visio_recorder.muxer import SubprocessCommandRunner
from visio_recorder.recording_loop import ShutilDiskStatsReader, flush_pending
from visio_recorder.supabase_clients import SupabaseStatus, SupabaseStorage


class SimulatedCamera:
    """CommandRunner: rpicam-vid becomes ffmpeg writing H.264 to the -o path.

    The first capture runs to completion; the second is SIGTERMed partway,
    which makes subprocess.run(check=True) raise like the real one does.
    """

    def __init__(self) -> None:
        self.captures = 0
        self._mux = SubprocessCommandRunner()

    def run(self, args: list[str]) -> None:
        if args[0] != "rpicam-vid":
            self._mux.run(args[:1] + ["-loglevel", "error"] + args[1:])
            return
        self.captures += 1
        output = args[args.index("-o") + 1]
        seconds = int(args[args.index("-t") + 1]) / 1000
        cmd = [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-re",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=1280x720:rate=30:duration={seconds}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-f",
            "h264",
            output,
        ]
        proc = subprocess.Popen(cmd)
        if self.captures == 2:
            time.sleep(seconds / 2)
            print(f"systemd stop: SIGTERM to capture of {Path(output).name}")
            proc.send_signal(signal.SIGTERM)
        if proc.wait() != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd)


class Battery:
    def get_charge_pct(self) -> int:
        return 80


class Led:
    def set(self, color: tuple[int, int, int], pattern: LedPattern) -> None:
        pass


def main() -> None:
    url = os.environ["SUPABASE_URL"]
    admin = create_client(url, os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    run_id = uuid.uuid4().hex[:8]
    email = f"e2e-stop-{run_id}@example.test"
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
        storage = SupabaseStorage(owner)

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            queue_dir = data_dir / "queue"
            (data_dir / "session.json").write_text("{}")
            deps = LoopDeps(
                command_runner=SimulatedCamera(),
                storage_client=storage,
                status_client=SupabaseStatus(owner),
                led_driver=Led(),
                battery_reader=Battery(),
                disk_stats_reader=ShutilDiskStatsReader(),
                data_dir=data_dir,
                queue_dir=queue_dir,
                device_id=device_id,
                segment_duration_ms=6000,
                framerate=30,
            )
            minutes = iter(range(60))

            def clock() -> datetime:
                return datetime(2026, 8, 3, 12, next(minutes), 0)

            try:
                run_recording_loop(deps, threading.Event(), clock=clock)
            except subprocess.CalledProcessError as exc:
                print(f"daemon exited: CalledProcessError returncode={exc.returncode}")

            row = (
                admin.table("device_status")
                .select("recording_active,segments_uploaded_today")
                .eq("device_id", device_id)
                .execute()
                .data
            )
            print(f"device_status after the daemon exited: {row}")
            print(f"data_dir after exit: {sorted(p.name for p in data_dir.iterdir())}")

            print("--- next boot ---")
            recover = getattr(recording_loop, "recover_orphaned_segments", None)
            if recover is None:
                print("recover_orphaned_segments: not in this firmware")
            else:
                recovered = recover(data_dir, queue_dir, SimulatedCamera(), 30)
                print(f"recovered: {[p.name for p in recovered]}")
            flushed = flush_pending(queue_dir, storage, device_id)
            print(f"flush_pending uploaded: {flushed}")
            print(f"data_dir after boot: {sorted(p.name for p in data_dir.iterdir())}")

            listed = admin.storage.from_("segments").list(device_id)
            print(f"objects in storage: {sorted(i['name'] for i in listed)}")
            for item in sorted(listed, key=lambda i: i["name"]):
                local = data_dir / f"downloaded_{item['name']}"
                local.write_bytes(
                    admin.storage.from_("segments").download(
                        f"{device_id}/{item['name']}"
                    )
                )
                probe = subprocess.run(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-count_frames",
                        "-show_entries",
                        "stream=nb_read_frames",
                        "-of",
                        "default=nw=1:nk=1",
                        str(local),
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                print(f"  {item['name']}: {probe.stdout.strip()} decodable frames")
    finally:
        if device_id is not None:
            listed = admin.storage.from_("segments").list(device_id)
            names = [f"{device_id}/{i['name']}" for i in listed]
            if names:
                admin.storage.from_("segments").remove(names)
        admin.auth.admin.delete_user(owner_id)


if __name__ == "__main__":
    main()
