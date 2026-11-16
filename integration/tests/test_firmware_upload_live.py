"""Live-Supabase tests for firmware's storage upload adapter.

These pin three properties of ``visio_recorder.supabase_clients.SupabaseStorage``
against a real storage API, because each one is a header or request-shape
detail that only the real server enforces:

- a retried upload of an object that already exists succeeds (the
  ``flush_pending`` retry contract after a power cut between upload and unlink);
- segments are stored as ``video/mp4``, not storage3's default ``text/plain``;
- the file is streamed rather than read whole into memory, which matters on a
  512 MB Pi Zero 2W uploading ~170 MB segments.

Needs a running `supabase start` instance; skips automatically otherwise
(see conftest.py and integration/README.md).
"""

import tracemalloc
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from supabase import Client, create_client

from visio_recorder.recording_loop import flush_pending
from visio_recorder.supabase_clients import SupabaseStorage
from visio_recorder.upload_queue import enqueue, list_pending
from visio_recorder.uploader import upload_segment

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


def _object_mimetype(owner: _Owner, filename: str) -> str:
    listed = owner.admin.storage.from_("segments").list(owner.device_id)
    [item] = [i for i in listed if i["name"] == filename]
    return str(item["metadata"]["mimetype"])


def test_next_boot_flush_clears_a_segment_that_already_reached_storage(
    owner: _Owner, tmp_path: Path
):
    # Power cut between upload and unlink: the object is in storage, but the
    # file is still in the queue. The next boot's flush_pending must clear it
    # rather than fail on a duplicate forever.
    queue_dir = tmp_path / "queue"
    segment = tmp_path / "20260714_090000.mp4"
    segment.write_bytes(b"segment bytes")
    queued = enqueue(queue_dir, segment)
    storage = SupabaseStorage(owner.client)
    upload_segment(storage, owner.device_id, queued)

    uploaded = flush_pending(queue_dir, storage, owner.device_id)

    assert uploaded == 1
    assert list_pending(queue_dir) == []


def test_segment_is_stored_as_video_mp4(owner: _Owner, tmp_path: Path):
    segment = tmp_path / "20260714_090500.mp4"
    segment.write_bytes(b"segment bytes")

    upload_segment(SupabaseStorage(owner.client), owner.device_id, segment)

    assert _object_mimetype(owner, segment.name) == "video/mp4"


def test_segment_upload_does_not_hold_the_whole_file_in_memory(
    owner: _Owner, tmp_path: Path
):
    # 40 MiB stays under the local stack's 50 MiB file_size_limit
    # (supabase/config.toml) while being large enough that a whole-file read
    # dominates the measured peak.
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
