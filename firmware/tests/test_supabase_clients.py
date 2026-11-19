import json
from pathlib import Path

import pytest
from storage3.exceptions import StorageApiError
from supabase_auth.errors import AuthApiError, AuthRetryableError

from visio_recorder.supabase_clients import (
    SessionRevokedError,
    SupabaseStorage,
    build_supabase_clients,
)
from visio_recorder.uploader import ObjectAlreadyStoredError, ObjectTooLargeError


class FakeUser:
    def __init__(self, user_id: str) -> None:
        self.id = user_id


class FakeAuthResponse:
    def __init__(self, user_id: str) -> None:
        self.user = FakeUser(user_id)


class FakeSession:
    def __init__(self, access_token: str, refresh_token: str) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token


class FakeAuth:
    """supabase-py's auth client, as far as the daemon uses it.

    ``refresh_on_set`` makes ``set_session`` behave as it does for an expired
    access token: it refreshes and notifies TOKEN_REFRESHED before returning.
    """

    def __init__(self, user_id: str) -> None:
        self._user_id = user_id
        self.session_set_with: tuple[str, str] | None = None
        self.refresh_on_set: FakeSession | None = None
        self.set_session_error: Exception | None = None
        self._subscribers: list = []

    def on_auth_state_change(self, callback) -> None:
        self._subscribers.append(callback)

    def emit(self, event: str, session: FakeSession | None) -> None:
        for callback in self._subscribers:
            callback(event, session)

    def set_session(self, access_token: str, refresh_token: str) -> FakeAuthResponse:
        self.session_set_with = (access_token, refresh_token)
        if self.set_session_error is not None:
            raise self.set_session_error
        if self.refresh_on_set is not None:
            self.emit("TOKEN_REFRESHED", self.refresh_on_set)
        return FakeAuthResponse(self._user_id)


class FakeStorageBucket:
    # Mirrors the local storage API: x-upsert=false rejects an existing key
    # with a 409 Duplicate (status as a string, as storage3 reports it).
    def __init__(self, bucket: str, uploads: list, objects: dict) -> None:
        self._bucket = bucket
        self._uploads = uploads
        self._objects = objects
        self.info_has_size = True
        self.info_hides_existing = False
        self.max_size: int | None = None

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
        if self.max_size is not None and len(data) > self.max_size:
            # What storage-api answers over a file size limit; storage3
            # surfaces the body's numeric statusCode as the error's status.
            raise StorageApiError(
                "The object exceeded the maximum allowed size",
                "Payload too large",
                413,
            )
        key = (self._bucket, path)
        if key in self._objects:
            raise StorageApiError("The resource already exists", "Duplicate", "409")
        self._objects[key] = data

    def info(self, path: str) -> dict:
        # Like the storage API: a missing object is a 404 not_found, and so
        # is one this login's select policy does not let it see.
        if (self._bucket, path) not in self._objects or self.info_hides_existing:
            raise StorageApiError("Object not found", "not_found", 404)
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
        self.info_hides_existing = False
        self.max_size: int | None = None

    def from_(self, bucket: str) -> FakeStorageBucket:
        api = FakeStorageBucket(bucket, self._uploads, self.objects)
        api.info_has_size = self.info_has_size
        api.info_hides_existing = self.info_hides_existing
        api.max_size = self.max_size
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


def _stored_session(path: Path) -> dict:
    return json.loads(path.read_text())


def test_a_token_refresh_during_the_run_is_written_back_to_session_json(tmp_path):
    # Refresh tokens rotate and each one works once, so a pendant that only
    # ever keeps the token it was onboarded with cannot log in after a reboot
    # once it has refreshed twice in a run (reproduced against the local
    # stack: "Invalid Refresh Token: Already Used").
    fake = FakeSupabase()
    session_path = _write_session(tmp_path)
    build_supabase_clients(
        "http://x", "anon", session_path, client_factory=lambda url, key: fake
    )

    fake.auth.emit("TOKEN_REFRESHED", FakeSession("at-2", "rt-2"))

    assert _stored_session(session_path) == {
        "access_token": "at-2",
        "refresh_token": "rt-2",
    }


def test_the_refresh_set_session_does_at_boot_is_written_back_too(tmp_path):
    # An expired access token makes set_session refresh before it returns, so
    # the listener has to be registered before the session is set.
    fake = FakeSupabase()
    fake.auth.refresh_on_set = FakeSession("at-boot", "rt-boot")
    session_path = _write_session(tmp_path)
    build_supabase_clients(
        "http://x", "anon", session_path, client_factory=lambda url, key: fake
    )

    assert _stored_session(session_path)["refresh_token"] == "rt-boot"


def test_events_without_a_session_leave_session_json_alone(tmp_path):
    # A sign-out notification carries no session; erasing the file would turn
    # a transient server-side revoke into a permanent loss of the login.
    fake = FakeSupabase()
    session_path = _write_session(tmp_path)
    before = session_path.read_text()
    build_supabase_clients(
        "http://x", "anon", session_path, client_factory=lambda url, key: fake
    )

    fake.auth.emit("SIGNED_OUT", None)

    assert session_path.read_text() == before


def test_a_written_back_session_stays_private_and_leaves_no_temp_file(tmp_path):
    fake = FakeSupabase()
    session_path = _write_session(tmp_path)
    build_supabase_clients(
        "http://x", "anon", session_path, client_factory=lambda url, key: fake
    )

    fake.auth.emit("TOKEN_REFRESHED", FakeSession("at-2", "rt-2"))

    assert session_path.stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == ["session.json"]


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


def test_storage_adapter_reports_an_object_over_the_size_limit_distinctly(tmp_path):
    # A 413 must not look like a network failure: the caller splits the file
    # instead of leaving it queued to be rejected again on every boot.
    fake = FakeSupabase()
    fake.storage.max_size = 2
    clients = build_supabase_clients(
        "http://x",
        "anon",
        _write_session(tmp_path),
        client_factory=lambda url, key: fake,
    )
    local = tmp_path / "20260709_080000.mp4"
    local.write_bytes(b"vid")

    with pytest.raises(ObjectTooLargeError, match="20260709_080000.mp4"):
        clients.storage.upload("segments", "device-abc/20260709_080000.mp4", local)


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


def test_storage_adapter_retry_does_not_resend_an_already_stored_file(tmp_path):
    # The size check runs before the upload, so a retried ~170 MB segment is
    # not sent over WiFi again only to be rejected as a duplicate.
    fake, storage = _storage_with_existing(tmp_path, b"payload")
    local = tmp_path / "20260709_080000.mp4"
    local.write_bytes(b"payload")

    storage.upload("segments", "device-abc/20260709_080000.mp4", local)

    assert fake.uploads == []


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

    with pytest.raises(FileExistsError):
        storage.upload("segments", "device-abc/20260709_080000.mp4", local)

    assert fake.uploads == []
    assert fake.storage.objects[("segments", "device-abc/20260709_080000.mp4")] == (
        b"earlier segment"
    )


def test_storage_adapter_reports_a_duplicate_it_cannot_see_distinctly(tmp_path):
    # The pendant reads back only what its own login uploaded, so an object
    # stored under an earlier pairing looks absent and the upload gets a 409.
    fake = FakeSupabase()
    clients = build_supabase_clients(
        "http://x",
        "anon",
        _write_session(tmp_path),
        client_factory=lambda url, key: fake,
    )
    fake.storage.objects[("segments", "device-abc/20260709_080000.mp4")] = b"old"
    fake.storage.info_hides_existing = True
    local = tmp_path / "20260709_080000.mp4"
    local.write_bytes(b"payload")

    with pytest.raises(ObjectAlreadyStoredError, match="20260709_080000.mp4"):
        clients.storage.upload("segments", "device-abc/20260709_080000.mp4", local)


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


@pytest.mark.parametrize(
    "error",
    [
        AuthApiError("User from sub claim in JWT does not exist", 403, None),
        AuthApiError("Invalid Refresh Token: Already Used", 400, None),
        AuthApiError("Session not found", 401, None),
    ],
)
def test_a_definite_session_rejection_means_the_login_was_revoked(tmp_path, error):
    # Remove device in the app deletes the pendant's user, and a re-pair
    # replaces it; either way this login is gone for good.
    fake = FakeSupabase()
    fake.auth.set_session_error = error

    with pytest.raises(SessionRevokedError):
        build_supabase_clients(
            "http://x",
            "anon",
            _write_session(tmp_path),
            client_factory=lambda url, key: fake,
        )


@pytest.mark.parametrize(
    "error",
    [
        AuthApiError("Request rate limit reached", 429, "over_request_rate_limit"),
        AuthApiError("Internal Server Error", 500, None),
        AuthRetryableError("connection refused", 0),
        AuthRetryableError("Bad Gateway", 502),
    ],
)
def test_a_network_or_server_failure_is_not_a_revocation(tmp_path, error):
    # Unpairing on a flaky network would throw away a good login.
    fake = FakeSupabase()
    fake.auth.set_session_error = error

    with pytest.raises(type(error)) as exc_info:
        build_supabase_clients(
            "http://x",
            "anon",
            _write_session(tmp_path),
            client_factory=lambda url, key: fake,
        )

    assert exc_info.value is error


@pytest.mark.parametrize("content", ["", "[]", "{}", '{"access_token": "at"}'])
def test_an_unreadable_session_is_a_lost_login_not_a_crash(tmp_path, content):
    # The boot gate already treats this as unpaired; this is the last line
    # if the file changes between the gate and the restore.
    session_path = tmp_path / "session.json"
    session_path.write_text(content)
    fake = FakeSupabase()

    with pytest.raises(SessionRevokedError):
        build_supabase_clients(
            "http://x", "anon", session_path, client_factory=lambda url, key: fake
        )
