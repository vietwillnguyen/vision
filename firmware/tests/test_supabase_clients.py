import json
from pathlib import Path

from visio_recorder.supabase_clients import build_supabase_clients


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


class FakeStorageBucket:
    def __init__(self, bucket: str, uploads: list) -> None:
        self._bucket = bucket
        self._uploads = uploads

    def upload(self, path: str, file, file_options=None) -> None:
        # Record what supabase-py's storage3 would turn into the request: the
        # file handle (read here, while the adapter still holds it open) and
        # the headers it would send.
        self._uploads.append(
            {
                "bucket": self._bucket,
                "path": path,
                "file": file,
                "data": file.read() if hasattr(file, "read") else file,
                "file_options": file_options,
            }
        )


class FakeStorage:
    def __init__(self, uploads: list) -> None:
        self._uploads = uploads

    def from_(self, bucket: str) -> FakeStorageBucket:
        return FakeStorageBucket(bucket, self._uploads)


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
        self.storage = FakeStorage(self.uploads)
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

    [upload] = fake.uploads
    assert upload["bucket"] == "segments"
    assert upload["path"] == "device-abc/20260709_080000.mp4"
    assert upload["data"] == b"vid"


def _upload_one(tmp_path: Path, filename: str) -> dict:
    fake = FakeSupabase()
    clients = build_supabase_clients(
        "http://x",
        "anon",
        _write_session(tmp_path),
        client_factory=lambda url, key: fake,
    )
    local = tmp_path / filename
    local.write_bytes(b"payload")
    clients.storage.upload("segments", f"device-abc/{filename}", local)
    [upload] = fake.uploads
    return upload


def test_storage_adapter_upload_is_an_idempotent_upsert(tmp_path):
    # flush_pending retries a queued file on every boot. If the first attempt
    # reached storage but the device lost power before unlinking it, the retry
    # targets an object that already exists; storage3's default x-upsert=false
    # turns that into a duplicate error forever.
    upload = _upload_one(tmp_path, "20260709_080000.mp4")

    assert upload["file_options"]["upsert"] == "true"


def test_storage_adapter_labels_segments_as_video_mp4(tmp_path):
    # storage3's default content-type is text/plain;charset=UTF-8.
    upload = _upload_one(tmp_path, "20260709_080000.mp4")

    assert upload["file_options"]["content-type"] == "video/mp4"


def test_storage_adapter_labels_flag_markers_as_binary(tmp_path):
    upload = _upload_one(tmp_path, "FLAG_20260709_080000.marker")

    assert upload["file_options"]["content-type"] == "application/octet-stream"


def test_storage_adapter_streams_the_file_instead_of_reading_it_whole(tmp_path):
    # A ~170 MB segment read into one bytes object is a large share of a
    # 512 MB Pi Zero 2W; handing storage3 the open file lets httpx stream it.
    upload = _upload_one(tmp_path, "20260709_080000.mp4")

    assert not isinstance(upload["file"], bytes)
    assert hasattr(upload["file"], "read")


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
