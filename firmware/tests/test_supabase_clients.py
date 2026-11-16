import json
from pathlib import Path

import pytest
from storage3.exceptions import StorageApiError

from visio_recorder.supabase_clients import SupabaseStorage, build_supabase_clients


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
    # Mirrors the local storage API: x-upsert=false rejects an existing key
    # with a 409 Duplicate (status as a string, as storage3 reports it).
    def __init__(self, bucket: str, uploads: list, objects: dict) -> None:
        self._bucket = bucket
        self._uploads = uploads
        self._objects = objects
        self.info_has_size = True
        self.lookup_hangs_after_duplicate = False
        self._hung_lookup_pending = False

    def upload(self, path: str, file, file_options=None) -> None:
        # Record what supabase-py's storage3 would turn into the request: the
        # file handle (read here, while the adapter still holds it open) and
        # the headers it would send.
        data = file.read() if hasattr(file, "read") else file
        self._uploads.append(
            {
                "bucket": self._bucket,
                "path": path,
                "file": file,
                "data": data,
                "file_options": file_options,
            }
        )
        key = (self._bucket, path)
        if key in self._objects:
            self._hung_lookup_pending = self.lookup_hangs_after_duplicate
            raise StorageApiError("The resource already exists", "Duplicate", "409")
        self._objects[key] = data

    def info(self, path: str) -> dict:
        # Local storage behind kong answers a large duplicate before reading
        # its body, and the next storage request then stalls until httpx's
        # ReadTimeout; the request after that is served normally.
        if self._hung_lookup_pending:
            self._hung_lookup_pending = False
            raise TimeoutError("timed out")
        info: dict = {"name": path}
        if self.info_has_size:
            info["size"] = len(self._objects[(self._bucket, path)])
        return info

    def list(self, folder: str, options: dict | None = None) -> list[dict]:
        # Like the storage API: names sorted ascending, filtered by a name
        # prefix search, and capped at one page (storage3's default limit 100).
        options = options or {}
        prefix = f"{folder}/"
        search = options.get("search", "")
        names = sorted(
            path[len(prefix) :]
            for (bucket, path) in self._objects
            if bucket == self._bucket
            and path.startswith(prefix)
            and path[len(prefix) :].startswith(search)
        )
        return [
            {
                "name": name,
                "metadata": {"size": len(self._objects[(self._bucket, prefix + name)])},
            }
            for name in names[: options.get("limit", 100)]
        ]


class FakeStorage:
    def __init__(self, uploads: list) -> None:
        self._uploads = uploads
        self.objects: dict = {}
        self.info_has_size = True
        self.lookup_hangs_after_duplicate = False

    def from_(self, bucket: str) -> FakeStorageBucket:
        api = FakeStorageBucket(bucket, self._uploads, self.objects)
        api.info_has_size = self.info_has_size
        api.lookup_hangs_after_duplicate = self.lookup_hangs_after_duplicate
        return api


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


def _storage_with_existing(
    tmp_path: Path, existing: bytes, *, info_has_size: bool = True
) -> tuple[FakeSupabase, SupabaseStorage]:
    fake = FakeSupabase()
    fake.storage.info_has_size = info_has_size
    fake.storage.objects[("segments", "device-abc/20260709_080000.mp4")] = existing
    clients = build_supabase_clients(
        "http://x",
        "anon",
        _write_session(tmp_path),
        client_factory=lambda url, key: fake,
    )
    return fake, clients.storage


def test_storage_adapter_never_requests_an_overwrite(tmp_path):
    # Keys are naive local timestamps that can repeat (DST fall-back, a Pi
    # without an RTC booting before NTP sync), so an upsert could replace an
    # earlier, different segment.
    upload = _upload_one(tmp_path, "20260709_080000.mp4")

    assert "upsert" not in upload["file_options"]


def test_storage_adapter_retry_survives_a_stalled_first_size_lookup(tmp_path):
    # Storage can answer 409 before reading a large body, and the size lookup
    # sent next then times out. One stalled lookup must not leave an
    # already-uploaded segment queued forever.
    fake, storage = _storage_with_existing(tmp_path, b"payload")
    fake.storage.lookup_hangs_after_duplicate = True
    local = tmp_path / "20260709_080000.mp4"
    local.write_bytes(b"payload")

    storage.upload("segments", "device-abc/20260709_080000.mp4", local)


@pytest.mark.parametrize("info_has_size", [True, False])
def test_storage_adapter_retry_of_an_already_stored_file_succeeds(
    tmp_path, info_has_size
):
    # flush_pending retries a queued file on every boot. If the first attempt
    # reached storage but the device lost power before unlinking it, the retry
    # targets an object that already exists and must count as uploaded.
    fake, storage = _storage_with_existing(
        tmp_path, b"payload", info_has_size=info_has_size
    )
    local = tmp_path / "20260709_080000.mp4"
    local.write_bytes(b"payload")

    storage.upload("segments", "device-abc/20260709_080000.mp4", local)

    assert fake.storage.objects[("segments", "device-abc/20260709_080000.mp4")] == (
        b"payload"
    )


def test_storage_adapter_retry_finds_the_stored_size_past_the_first_list_page(
    tmp_path,
):
    # A device folder passes 100 objects within about a day; the list fallback
    # must target the one key rather than read the first page of the folder.
    fake, storage = _storage_with_existing(tmp_path, b"payload", info_has_size=False)
    for i in range(150):
        fake.storage.objects[("segments", f"device-abc/20260101_{i:06d}.mp4")] = b""
    local = tmp_path / "20260709_080000.mp4"
    local.write_bytes(b"payload")

    storage.upload("segments", "device-abc/20260709_080000.mp4", local)


def test_storage_adapter_refuses_a_different_file_under_an_existing_key(tmp_path):
    fake, storage = _storage_with_existing(tmp_path, b"earlier segment")
    local = tmp_path / "20260709_080000.mp4"
    local.write_bytes(b"later, different segment")

    with pytest.raises(StorageApiError):
        storage.upload("segments", "device-abc/20260709_080000.mp4", local)

    assert fake.storage.objects[("segments", "device-abc/20260709_080000.mp4")] == (
        b"earlier segment"
    )


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
