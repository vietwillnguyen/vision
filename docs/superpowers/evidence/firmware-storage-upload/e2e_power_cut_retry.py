"""End-to-end reproduction of the firmware upload defects against local Supabase.

Simulates the device with real tools wherever the Pi is not needed:
ffmpeg stands in for rpicam-vid (it writes a raw H.264 elementary stream),
then firmware's own mux, queue, upload, and flush_pending code runs unchanged
against a live `supabase start` stack.

Scenario: a segment is uploaded, and power is cut before the queue file is
unlinked. The next boot runs flush_pending. Then the stored object is inspected
(content type, playability) and the upload's peak Python memory is measured.

Run from integration/ with SUPABASE_URL, SUPABASE_ANON_KEY and
SUPABASE_SERVICE_ROLE_KEY exported:

    uv run --locked --extra dev python \
        ../docs/superpowers/evidence/firmware-storage-upload/e2e_power_cut_retry.py
"""

import os
import subprocess
import tempfile
import tracemalloc
import uuid
from pathlib import Path

from supabase import create_client
from visio_recorder.muxer import SubprocessCommandRunner, mux_segment
from visio_recorder.recording_loop import flush_pending
from visio_recorder.supabase_clients import SupabaseStorage
from visio_recorder.upload_queue import enqueue, list_pending
from visio_recorder.uploader import upload_segment


def main() -> None:
    url = os.environ["SUPABASE_URL"]
    admin = create_client(url, os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    run_id = uuid.uuid4().hex[:8]
    email = f"e2e-upload-{run_id}@example.test"
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
            h264 = data_dir / "20260714_090000.h264"
            # rpicam-vid stand-in: 60 s of 720p30 raw H.264, high bitrate so the
            # file is large enough for the memory measurement to mean something.
            subprocess.run(
                [
                    "ffmpeg",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=size=1280x720:rate=30:duration=60",
                    "-c:v",
                    "libx264",
                    "-b:v",
                    "5M",
                    "-pix_fmt",
                    "yuv420p",
                    "-f",
                    "h264",
                    str(h264),
                ],
                check=True,
            )
            mp4 = mux_segment(SubprocessCommandRunner(), h264, 30)
            h264.unlink()
            queued = enqueue(queue_dir, mp4)
            size = queued.stat().st_size
            print(f"segment: {queued.name} {size} bytes")

            tracemalloc.start()
            upload_segment(storage, device_id, queued)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            print(
                f"first upload: ok, peak python memory {peak} bytes "
                f"({peak / size:.2f}x file size)"
            )
            print("power cut before mark_uploaded: file stays queued")

            uploaded = flush_pending(queue_dir, storage, device_id)
            print(f"next boot flush_pending uploaded: {uploaded}")
            print(f"queue after flush: {[p.name for p in list_pending(queue_dir)]}")

            listed = admin.storage.from_("segments").list(device_id)
            meta = next(i for i in listed if i["name"] == queued.name)["metadata"]
            print(f"stored mimetype: {meta['mimetype']}  size: {meta['size']}")

            downloaded = data_dir / "downloaded.mp4"
            downloaded.write_bytes(
                admin.storage.from_("segments").download(f"{device_id}/{queued.name}")
            )
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=nw=1",
                    str(downloaded),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            print(f"downloaded object ffprobe: {probe.stdout.strip()}")
    finally:
        if device_id is not None:
            listed = admin.storage.from_("segments").list(device_id)
            names = [f"{device_id}/{i['name']}" for i in listed]
            if names:
                admin.storage.from_("segments").remove(names)
        admin.auth.admin.delete_user(owner_id)


if __name__ == "__main__":
    main()
