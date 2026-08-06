import json
from pathlib import Path

import pytest
from storage3._sync.file_api import DEFAULT_FILE_OPTIONS

from visio_recorder.recording_loop import flush_pending
from visio_recorder.supabase_clients import build_supabase_clients
from visio_recorder.upload_queue import enqueue, list_pending


class FakeUser:
    def __init__(self, user_id: str) -> None:
        self.id = user_id


class FakeAuthResponse:
    def __init__(self, user_id: str) -> None:
        self.user = FakeUser(user_id)


class FakeAuth:
    def __init__(self, user_id: str) -> None:
        self._user_id = user_id
        self.session_set_with: tuple[str, str] | None = None

    def set_session(self, access_token: str, refresh_token: str) -> FakeAuthResponse:
        self.session_set_with = (access_token, refresh_token)
        return FakeAuthResponse(self._user_id)


class DuplicateObjectError(RuntimeError):
    """What Supabase Storage answers a non-upsert POST over an existing key."""


class FakeStorageBucket:
    # Models the one storage3/Supabase behaviour the retry path depends on:
    # a POST over a key that already exists is rejected unless the request
    # carries x-upsert, and storage3 merges file_options over
    # DEFAULT_FILE_OPTIONS, so an unset key keeps its default rather than
    # being absent.
    def __init__(self, bucket: str, uploads: list, objects: dict) -> None:
        self._bucket = bucket
        self._uploads = uploads
        self._objects = objects

    def upload(self, path: str, data: bytes, file_options: dict | None = None) -> None:
        headers = {**DEFAULT_FILE_OPTIONS, **(file_options or {})}
        key = (self._bucket, path)
        if key in self._objects and headers["x-upsert"] != "true":
            raise DuplicateObjectError(f"The resource already exists: {path}")
        self._objects[key] = data
        self._uploads.append((self._bucket, path, data, headers))


class FakeStorage:
    def __init__(self, uploads: list, objects: dict) -> None:
        self._uploads = uploads
        self._objects = objects

    def from_(self, bucket: str) -> FakeStorageBucket:
        return FakeStorageBucket(bucket, self._uploads, self._objects)


class FakeTable:
    def __init__(self, name: str, upserts: dict) -> None:
        self._name = name
        self._upserts = upserts

    def upsert(self, row: dict) -> "FakeTable":
        self._upserts.setdefault(self._name, []).append(row)
        return self

    def execute(self) -> None:
        return None


class FakeSupabase:
    # Records auth.set_session, storage uploads, and table upserts, mimicking
    # the supabase-py client surface used by the adapters.
    def __init__(self, user_id: str = "user-123") -> None:
        self.auth = FakeAuth(user_id)
        self.uploads: list = []
        self.objects: dict = {}
        self.storage = FakeStorage(self.uploads, self.objects)
        self.upserts: dict = {}

    @property
    def session_set_with(self):
        return self.auth.session_set_with

    def table(self, name: str) -> FakeTable:
        return FakeTable(name, self.upserts)


def _write_session(tmp_path: Path) -> Path:
    session = tmp_path / "session.json"
    session.write_text(json.dumps({"access_token": "at", "refresh_token": "rt"}))
    return session


def test_build_sets_the_session_before_any_client_use(tmp_path):
    fake = FakeSupabase()
    build_supabase_clients(
        "http://x",
        "anon",
        _write_session(tmp_path),
        client_factory=lambda url, key: fake,
    )
    assert fake.session_set_with == ("at", "rt")


def test_storage_adapter_uploads_to_bucket_and_path(tmp_path):
    fake = FakeSupabase()
    clients = build_supabase_clients(
        "http://x",
        "anon",
        _write_session(tmp_path),
        client_factory=lambda url, key: fake,
    )
    local = tmp_path / "20260709_080000.mp4"
    local.write_bytes(b"vid")

    clients.storage.upload("segments", "device-abc/20260709_080000.mp4", local)

    bucket, path, data, _headers = fake.uploads[0]
    assert (bucket, path, data) == (
        "segments",
        "device-abc/20260709_080000.mp4",
        b"vid",
    )
    assert len(fake.uploads) == 1


def test_storage_adapter_labels_segments_and_markers_by_their_own_type(tmp_path):
    # storage3's DEFAULT_FILE_OPTIONS content type is text/plain;charset=UTF-8
    # and file_options is merged *over* it, so leaving the key unset does not
    # mean "let the server sniff it" - it means every object is stored
    # mislabelled. `.marker` has no registered type, hence the fallback.
    fake = FakeSupabase()
    clients = build_supabase_clients(
        "http://x", "anon", _write_session(tmp_path), client_factory=lambda u, k: fake
    )
    segment = tmp_path / "20260709_080000.mp4"
    segment.write_bytes(b"vid")
    marker = tmp_path / "FLAG_20260709_080000.marker"
    marker.write_bytes(b"")

    clients.storage.upload("segments", "device-abc/20260709_080000.mp4", segment)
    clients.storage.upload("segments", "device-abc/FLAG_20260709_080000.marker", marker)

    assert fake.uploads[0][3]["content-type"] == "video/mp4"
    assert fake.uploads[1][3]["content-type"] == "application/octet-stream"


def test_a_retry_after_power_loss_between_upload_and_unlink_still_drains(tmp_path):
    # Checklist task 9 pulls the power cable. The queue's contract is that a
    # file stays queued until mark_uploaded unlinks it, so a cut in the window
    # between a successful upload and that unlink leaves the object remote and
    # the file queued. Nothing in the retry path ever removes a file it did not
    # itself upload, so a retry that errors on "already exists" does not fail
    # once - it fails on that segment forever, and the queue never drains past
    # it. Reproduces the crash, then runs the real next-boot flush.
    fake = FakeSupabase()
    clients = build_supabase_clients(
        "http://x", "anon", _write_session(tmp_path), client_factory=lambda u, k: fake
    )
    queue_dir = tmp_path / "queue"
    for name in ("20260709_080000.mp4", "20260709_080500.mp4"):
        source = tmp_path / name
        source.write_bytes(b"vid")
        enqueue(queue_dir, source)

    # The pre-crash boot: the first segment reached storage, then the power
    # went before mark_uploaded could unlink it.
    fake.objects[("segments", "device-abc/20260709_080000.mp4")] = b"vid"

    uploaded = flush_pending(queue_dir, clients.storage, "device-abc")

    assert uploaded == 2
    assert list_pending(queue_dir) == []


def test_upload_raises_when_the_object_exists_and_upsert_is_not_requested():
    # Pins the fake to the behaviour the test above depends on: without
    # x-upsert this is a hard error, not a silent overwrite. If Supabase ever
    # stopped rejecting duplicates, the test above would pass for the wrong
    # reason and this one would fail and say so.
    fake = FakeSupabase()
    fake.objects[("segments", "device-abc/20260709_080000.mp4")] = b"vid"

    with pytest.raises(DuplicateObjectError):
        fake.storage.from_("segments").upload(
            "device-abc/20260709_080000.mp4", b"vid", {"x-upsert": "false"}
        )


def test_status_adapter_upserts_device_status_row(tmp_path):
    fake = FakeSupabase()
    clients = build_supabase_clients(
        "http://x",
        "anon",
        _write_session(tmp_path),
        client_factory=lambda url, key: fake,
    )

    status = {
        "device_id": "device-abc",
        "battery_pct": 80,
        "storage_used_gb": 1.5,
        "storage_free_gb": 8.5,
        "segments_pending": 0,
        "segments_uploaded_today": 3,
        "recording_active": True,
    }
    clients.status.upsert_device_status(status)

    assert fake.upserts["device_status"] == [status]


def test_registration_adapter_upserts_device_row(tmp_path):
    fake = FakeSupabase(user_id="user-xyz")
    clients = build_supabase_clients(
        "http://x",
        "anon",
        _write_session(tmp_path),
        client_factory=lambda url, key: fake,
    )

    clients.registration.upsert_device("device-abc", "Visio Pendant")

    assert fake.upserts["devices"] == [
        {"device_id": "device-abc", "user_id": "user-xyz", "name": "Visio Pendant"}
    ]
