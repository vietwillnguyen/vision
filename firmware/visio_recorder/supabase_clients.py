import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from storage3.exceptions import StorageApiError
from supabase import create_client

# Content types by suffix for everything firmware uploads. Explicit rather than
# `mimetypes`, whose answer depends on the host's /etc/mime.types. Anything
# unlisted (flag markers) is stored as opaque bytes.
_CONTENT_TYPES = {".mp4": "video/mp4"}
_DEFAULT_CONTENT_TYPE = "application/octet-stream"


# `client` below is supabase-py's Client in production and a fake in tests.
# Annotating it as Client would reject the fakes these adapters are tested with,
# and supabase-py ships no Protocol for its fluent query/storage builders - so
# the untyped boundary is stated explicitly as Any rather than left implicit.
class SupabaseStorage:
    def __init__(self, client: Any) -> None:
        self._client = client

    def upload(self, bucket: str, object_path: str, local_path: Path) -> None:
        """Upload ``local_path`` to ``bucket/object_path``, idempotently.

        ``flush_pending`` retries a queued file on every boot, so an object that
        reached storage before a power cut is uploaded again. storage3 sends
        ``x-upsert: false``, and that is kept: object keys are second-resolution
        naive local timestamps, which can repeat across a DST fall-back or a Pi
        without an RTC restarting before NTP sync, so an overwrite could destroy
        an earlier, different segment. Instead a duplicate is treated as already
        uploaded only when the stored object has the local file's byte size - a
        cheap identity check for H.264 segments, and exact for flag markers,
        which are empty files whose name is their whole content. A different
        size, or a failed lookup, re-raises so the file stays queued. Storage
        can reject a large duplicate before reading its whole body, which
        leaves the gateway's upstream connection unusable: the next storage
        request sent on it (the size lookup) stalls until the client timeout,
        and the one after that is served normally. So a failed lookup is tried
        once more before giving up.

        storage3's default ``content-type: text/plain`` mislabels every
        segment, so it is set explicitly. Passing the open file rather than
        ``f.read()`` lets httpx stream a ~170 MB segment in chunks instead of
        holding it (and multipart copies of it) in a 512 MB Pi Zero 2W's memory.
        """
        content_type = _CONTENT_TYPES.get(local_path.suffix, _DEFAULT_CONTENT_TYPE)
        bucket_api = self._client.storage.from_(bucket)
        try:
            with open(local_path, "rb") as f:
                bucket_api.upload(object_path, f, {"content-type": content_type})
        except StorageApiError as err:
            if not _is_duplicate(err):
                raise
            try:
                try:
                    stored_size = _stored_size(bucket_api, object_path)
                except Exception:
                    stored_size = _stored_size(bucket_api, object_path)
            except Exception:
                raise err from None
            if stored_size != local_path.stat().st_size:
                raise


def _is_duplicate(err: StorageApiError) -> bool:
    return str(err.status) == "409" or err.code == "Duplicate"


def _stored_size(bucket_api: Any, object_path: str) -> int | None:
    size = bucket_api.info(object_path).get("size")
    if size is not None:
        return int(size)
    folder, _, name = object_path.rpartition("/")
    for item in bucket_api.list(folder, {"search": name, "limit": 100}):
        if item.get("name") == name:
            return int(item["metadata"]["size"])
    return None


class SupabaseStatus:
    def __init__(self, client: Any) -> None:
        self._client = client

    def upsert_device_status(self, status: dict[str, Any]) -> None:
        self._client.table("device_status").upsert(status).execute()


class SupabaseRegistration:
    def __init__(self, client: Any, user_id: str) -> None:
        self._client = client
        self._user_id = user_id

    def upsert_device(self, device_id: str, name: str) -> None:
        self._client.table("devices").upsert(
            {"device_id": device_id, "user_id": self._user_id, "name": name}
        ).execute()


@dataclass
class SupabaseClients:
    storage: SupabaseStorage
    status: SupabaseStatus
    registration: SupabaseRegistration


def build_supabase_clients(
    url: str,
    key: str,
    session_path: Path,
    client_factory: Callable[[str, str], Any] = create_client,
) -> SupabaseClients:
    client = client_factory(url, key)
    session = json.loads(session_path.read_text())
    auth_response = client.auth.set_session(
        session["access_token"], session["refresh_token"]
    )
    user_id = auth_response.user.id
    return SupabaseClients(
        storage=SupabaseStorage(client),
        status=SupabaseStatus(client),
        registration=SupabaseRegistration(client, user_id),
    )
