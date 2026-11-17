"""Simulated boot of the merged visio-recorder daemon against local Supabase.

Usage (from the repo root, with local Supabase running and firmware synced):

    bash docs/superpowers/evidence/pr26-merge-resolution/2026-11-17/simulate-boot.sh

The wrapper runs the unit's ExecStartPre (``python -m visio_recorder.preflight``)
first and only starts this script when it exits 0, exactly as systemd would.
This script then runs the real ``visio_recorder.daemon.main()`` with:

- real local Supabase auth, devices/device_status tables and segments bucket;
- real ffmpeg muxing, and a stub ``rpicam-vid`` on PATH that writes a short
  H.264 test pattern instead of reading a camera;
- the two GPIO drivers (WS2812 LED, flag button) replaced by logging fakes,
  since neither has a host equivalent.

Before boot it plants the leftovers an unclean stop produces: one file still in
the upload queue, a lone ``.h264`` and a lone ``.mp4`` in the data dir. A
watcher thread polls ``device_status`` with the service role key and logs every
``recording_active`` change, then sends SIGTERM once two live segments have
been uploaded.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from supabase import create_client

from visio_recorder import daemon

log = logging.getLogger("simulate-boot")


def _make_clip(path: Path, seconds: int, *, mp4: bool) -> None:
    fmt = ["-f", "mp4"] if mp4 else ["-f", "h264"]
    subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
            "-i", f"testsrc=size=320x240:rate=30:duration={seconds}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", *fmt, str(path),
        ],
        check=True,
    )


def _traced(name, fn):
    def wrapper(*args, **kwargs):
        log.info("BOOT STEP %s: start", name)
        result = fn(*args, **kwargs)
        shown = [p.name for p in result] if isinstance(result, list) else result
        log.info("BOOT STEP %s: done -> %s", name, shown)
        return result

    return wrapper


class LoggingLed:
    def set(self, color, pattern) -> None:
        log.info("LED -> color=%s pattern=%s", color, getattr(pattern, "name", pattern))


class LoggingFlagButton:
    def on_press(self, callback) -> None:
        log.info("flag button listener registered (GPIO 17 simulated)")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    data_dir = Path(os.environ["VISIO_DATA_DIR"])
    url = os.environ["SUPABASE_URL"]
    admin = create_client(url, os.environ["SUPABASE_SERVICE_ROLE_KEY"])

    email = f"simboot-{uuid.uuid4().hex[:8]}@example.test"
    password = "simulated-boot-pw-1"
    user_id = admin.auth.admin.create_user(
        {"email": email, "password": password, "email_confirm": True}
    ).user.id
    user = create_client(url, os.environ["SUPABASE_ANON_KEY"])
    session = user.auth.sign_in_with_password(
        {"email": email, "password": password}
    ).session
    device_id = str(uuid.uuid4())

    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "session.json").write_text(
        json.dumps(
            {"access_token": session.access_token, "refresh_token": session.refresh_token}
        )
    )
    (data_dir / "device.json").write_text(json.dumps({"device_id": device_id}))
    queue_dir = data_dir / "queue"
    queue_dir.mkdir(exist_ok=True)
    _make_clip(queue_dir / "20261116_230000.mp4", 1, mp4=True)
    _make_clip(data_dir / "20261116_231000.h264", 1, mp4=False)
    _make_clip(data_dir / "20261116_232000.mp4", 1, mp4=True)
    log.info(
        "planted leftovers: queue=%s data_dir=%s",
        sorted(p.name for p in queue_dir.iterdir()),
        sorted(p.name for p in data_dir.iterdir() if p.is_file()),
    )

    daemon.preflight.run_checks = _traced("preflight.run_checks (in main)", daemon.preflight.run_checks)
    daemon.flush_pending = _traced("flush_pending", daemon.flush_pending)
    daemon.recover_orphaned_segments = _traced(
        "recover_orphaned_segments", daemon.recover_orphaned_segments
    )
    daemon.run_recording_loop = _traced("run_recording_loop", daemon.run_recording_loop)
    daemon.Ws2812LedDriver = LoggingLed
    daemon.GpioZeroFlagButton = LoggingFlagButton

    planted = {"20261116_230000.mp4", "20261116_231000.mp4", "20261116_232000.mp4"}
    observed: list[tuple[bool, int]] = []
    done = threading.Event()

    def watch() -> None:
        last = None
        sent_stop = False
        deadline = time.monotonic() + 120
        while not done.is_set() and time.monotonic() < deadline:
            rows = (
                admin.table("device_status")
                .select("recording_active,segments_uploaded_today,segments_pending")
                .eq("device_id", device_id)
                .execute()
                .data
            )
            if rows:
                row = rows[0]
                key = (row["recording_active"], row["segments_uploaded_today"])
                if key != last:
                    log.info("device_status row: %s", row)
                    observed.append(key)
                    last = key
                live = [
                    o["name"]
                    for o in admin.storage.from_("segments").list(device_id)
                    if o["name"].endswith(".mp4") and o["name"] not in planted
                ]
                if not sent_stop and len(live) >= 2:
                    log.info("two live segments stored (%s); sending SIGTERM", live)
                    os.kill(os.getpid(), signal.SIGTERM)
                    sent_stop = True
            time.sleep(0.1)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        rc = daemon.main()
    finally:
        time.sleep(1.0)
        done.set()
        watcher.join()

    stored = sorted(o["name"] for o in admin.storage.from_("segments").list(device_id))
    final = (
        admin.table("device_status").select("*").eq("device_id", device_id).execute().data
    )
    log.info("daemon.main() returned %s", rc)
    log.info("objects in segments/%s: %s", device_id, stored)
    log.info("final device_status: %s", final)
    log.info("queue after run: %s", sorted(p.name for p in queue_dir.iterdir()))
    log.info(
        "data_dir top level after run: %s",
        sorted(p.name for p in data_dir.iterdir() if p.is_file()),
    )
    log.info("recording_active transitions observed: %s", observed)

    for name in stored:
        admin.storage.from_("segments").remove([f"{device_id}/{name}"])
    admin.auth.admin.delete_user(user_id)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
