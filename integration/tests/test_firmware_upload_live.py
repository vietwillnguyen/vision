"""Live-Supabase tests for firmware's storage upload adapter.

These pin three properties of ``visio_recorder.supabase_clients.SupabaseStorage``
against a real storage API, because each one is a header or request-shape
detail that only the real server enforces:

- a retried upload of an object that already exists succeeds (the
  ``flush_pending`` retry contract after a power cut between upload and unlink),
  while a different file under an existing key is refused, never overwritten;
- segments are stored as ``video/mp4``, not storage3's default ``text/plain``;
- the file is streamed rather than read whole into memory, which matters on a
  512 MB Pi Zero 2W uploading segments of up to 50 MiB;
- the buckets enforce the hosted plan's 50 MiB per-object limit (storage
  answers 413), the adapter reports that distinctly, and a real oversized
  segment is split with ffmpeg and uploaded in parts instead of being retried
  forever.

Needs a running `supabase start` instance; skips automatically otherwise
(see conftest.py and integration/README.md).
"""

import os
import shutil
import subprocess
import tracemalloc
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from supabase import Client, create_client

from visio_recorder.muxer import SubprocessCommandRunner, mux_segment
from visio_recorder.recording_loop import flush_pending
from visio_recorder.segment_size import DEFAULT_MAX_UPLOAD_BYTES
from visio_recorder.supabase_clients import SupabaseStorage
from visio_recorder.upload_limit import FfprobeDurationProbe, UploadLimit
from visio_recorder.upload_queue import enqueue, list_pending
from visio_recorder.uploader import ObjectTooLargeError, upload_segment

pytestmark = pytest.mark.usefixtures("live_supabase_env")

_PASSWORD = "correct-horse-battery-staple"


@dataclass
class _Owner:
    admin: Client
    client: Client
    device_id: str


@pytest.fixture
def owner(live_supabase_env) -> Iterator[_Owner]:
    url = live_supabase_env["url"]
    admin = create_client(url, live_supabase_env["service_role_key"])
    run_id = uuid.uuid4().hex[:8]
    email = f"upload-owner-{run_id}@example.test"
    owner_id = admin.auth.admin.create_user(
        {"email": email, "password": _PASSWORD, "email_confirm": True}
    ).user.id
    device_id: str | None = None
    try:
        inserted = cast(
            list[dict[str, Any]],
            admin.table("devices")
            .insert({"user_id": owner_id, "name": f"upload test device {run_id}"})
            .execute()
            .data,
        )
        device_id = str(inserted[0]["device_id"])
        client = create_client(url, live_supabase_env["anon_key"])
        client.auth.sign_in_with_password({"email": email, "password": _PASSWORD})
        yield _Owner(admin=admin, client=client, device_id=device_id)
    finally:
        if device_id is not None:
            listed = admin.storage.from_("segments").list(device_id)
            names = [f"{device_id}/{item['name']}" for item in listed]
            if names:
                admin.storage.from_("segments").remove(names)
        admin.auth.admin.delete_user(owner_id)


def _limit(max_bytes: int = DEFAULT_MAX_UPLOAD_BYTES) -> UploadLimit:
    return UploadLimit(
        max_bytes=max_bytes,
        runner=SubprocessCommandRunner(),
        probe=FfprobeDurationProbe(),
    )


def _object_metadata(owner: _Owner, filename: str) -> dict[str, Any]:
    listed = owner.admin.storage.from_("segments").list(owner.device_id)
    [item] = [i for i in listed if i["name"] == filename]
    return cast(dict[str, Any], item["metadata"])


def _object_mimetype(owner: _Owner, filename: str) -> str:
    return str(_object_metadata(owner, filename)["mimetype"])


def test_next_boot_flush_clears_a_segment_that_already_reached_storage(
    owner: _Owner, tmp_path: Path
):
    # Power cut between upload and unlink: the object is in storage, but the
    # file is still in the queue. The next boot's flush_pending must clear it
    # rather than fail on a duplicate forever.
    # Segment-sized, so a regression that re-sends the body and hits storage's
    # early duplicate rejection (which stalls the gateway connection) shows up.
    queue_dir = tmp_path / "queue"
    segment = tmp_path / "20260714_090000.mp4"
    segment.write_bytes(os.urandom(8 * 1024 * 1024))
    queued = enqueue(queue_dir, segment)
    storage = SupabaseStorage(owner.client)
    upload_segment(storage, owner.device_id, queued)

    uploaded = flush_pending(queue_dir, storage, owner.device_id, _limit())

    assert uploaded == 1
    assert list_pending(queue_dir) == []


def test_a_different_file_under_an_existing_key_is_refused_not_overwritten(
    owner: _Owner, tmp_path: Path
):
    # Keys are naive local timestamps, so a DST fall-back or a clock that steps
    # back can name a new segment after one already uploaded. That must fail
    # and stay queued, never replace the earlier recording.
    earlier = tmp_path / "earlier" / "20260714_092000.mp4"
    earlier.parent.mkdir()
    earlier.write_bytes(b"earlier segment")
    later = tmp_path / "later" / "20260714_092000.mp4"
    later.parent.mkdir()
    later.write_bytes(b"later, different segment")
    storage = SupabaseStorage(owner.client)
    upload_segment(storage, owner.device_id, earlier)

    with pytest.raises(FileExistsError):
        upload_segment(storage, owner.device_id, later)

    assert _object_metadata(owner, earlier.name)["size"] == len(b"earlier segment")


def test_segment_is_stored_as_video_mp4(owner: _Owner, tmp_path: Path):
    segment = tmp_path / "20260714_090500.mp4"
    segment.write_bytes(b"segment bytes")

    upload_segment(SupabaseStorage(owner.client), owner.device_id, segment)

    assert _object_mimetype(owner, segment.name) == "video/mp4"


def test_segment_upload_does_not_hold_the_whole_file_in_memory(
    owner: _Owner, tmp_path: Path
):
    # 40 MiB stays under the buckets' 50 MiB file_size_limit (migration
    # 20261116120000_cap_bucket_file_size.sql) while being large enough that a
    # whole-file read dominates the measured peak.
    size = 40 * 1024 * 1024
    segment = tmp_path / "20260714_091000.mp4"
    with open(segment, "wb") as f:
        f.truncate(size)

    tracemalloc.start()
    try:
        upload_segment(SupabaseStorage(owner.client), owner.device_id, segment)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < size // 4, f"upload peaked at {peak} bytes for a {size} byte file"


def test_storage_rejects_an_object_over_the_hosted_50_mib_limit(
    owner: _Owner, tmp_path: Path
):
    # supabase/config.toml's [storage] file_size_limit alone is not enforced
    # by the local storage container; the bucket-level cap is, on every stack.
    segment = tmp_path / "20260714_093000.mp4"
    with open(segment, "wb") as f:
        f.truncate(DEFAULT_MAX_UPLOAD_BYTES + 1)

    with pytest.raises(ObjectTooLargeError):
        upload_segment(SupabaseStorage(owner.client), owner.device_id, segment)

    assert owner.admin.storage.from_("segments").list(owner.device_id) == []


def test_storage_accepts_an_object_of_exactly_the_limit(owner: _Owner, tmp_path: Path):
    segment = tmp_path / "20260714_093500.mp4"
    with open(segment, "wb") as f:
        f.truncate(DEFAULT_MAX_UPLOAD_BYTES)

    upload_segment(SupabaseStorage(owner.client), owner.device_id, segment)

    assert _object_metadata(owner, segment.name)["size"] == DEFAULT_MAX_UPLOAD_BYTES


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)
def test_flush_splits_a_real_oversized_segment_and_uploads_every_part(
    owner: _Owner, tmp_path: Path
):
    # A real H.264 capture over the limit, muxed with firmware's own command,
    # as an older five-minute firmware would have left it queued. Storage would
    # answer 413 forever; the guard splits it losslessly and uploads the parts.
    h264 = tmp_path / "20260714_094000.h264"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi",
            "-i", "testsrc2=size=640x360:rate=30,noise=alls=20:allf=t",
            "-t", "24", "-c:v", "libx264", "-preset", "ultrafast", "-g", "60",
            "-b:v", "20M", "-minrate", "20M", "-maxrate", "20M", "-bufsize", "20M",
            "-x264-params", "nal-hrd=cbr", "-pix_fmt", "yuv420p",
            "-f", "h264", str(h264),
        ],
        check=True,
    )  # fmt: skip
    mp4 = mux_segment(SubprocessCommandRunner(), h264, 30)
    queue_dir = tmp_path / "queue"
    queued = enqueue(queue_dir, mp4)
    size = queued.stat().st_size
    assert size > DEFAULT_MAX_UPLOAD_BYTES

    uploaded = flush_pending(
        queue_dir, SupabaseStorage(owner.client), owner.device_id, _limit()
    )

    listed = owner.admin.storage.from_("segments").list(owner.device_id)
    sizes = {item["name"]: item["metadata"]["size"] for item in listed}
    assert uploaded == len(sizes) >= 2
    assert sorted(sizes)[0] == "20260714_094000.mp4"
    assert all(v <= DEFAULT_MAX_UPLOAD_BYTES for v in sizes.values())
    assert sum(sizes.values()) == pytest.approx(size, rel=0.01)
    assert list_pending(queue_dir) == []
