import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from storage3.exceptions import StorageApiError
from supabase import create_client
from supabase_auth.errors import AuthApiError

from visio_recorder.uploader import ObjectAlreadyStoredError, ObjectTooLargeError
from visio_recorder.wifi_onboard import write_session_credentials

_logger = logging.getLogger(__name__)

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
        an earlier, different segment.

        Instead the stored object is looked up before uploading. An existing
        object with the local file's byte size counts as already uploaded - a
        cheap identity check for H.264 segments, and exact for flag markers,
        which are empty files whose name is their whole content - so a retried
        segment is never sent over WiFi again just to be rejected. An
        existing object of another size raises ``FileExistsError``, and a failed
        lookup or upload raises too, so the file stays queued for the next retry.
        Checking first also avoids the duplicate path entirely: storage answers
        a large duplicate before reading its body, which leaves the gateway
        connection stalled for the next request.

        storage3's default ``content-type: text/plain`` mislabels every
        segment, so it is set explicitly. Passing the open file rather than
        ``f.read()`` lets httpx stream a segment (up to the 50 MiB upload
        limit) in chunks instead of holding it, and multipart copies of it, in
        a 512 MB Pi Zero 2W's memory.

        A 413 from storage (object over the bucket's size limit) is raised as
        ``ObjectTooLargeError`` so the caller splits the file instead of
        retrying it unchanged forever; see ``visio_recorder.upload_limit``.
        A 409 for a key the lookup could not see (stored under an earlier
        pairing) is raised as ``ObjectAlreadyStoredError`` so the caller sets
        the file aside instead of retrying it forever.
        """
        bucket_api = self._client.storage.from_(bucket)
        stored_size = _stored_size(bucket_api, object_path)
        if stored_size is not None:
            local_size = local_path.stat().st_size
            if stored_size == local_size:
                return
            raise FileExistsError(
                f"{bucket}/{object_path} already stores {stored_size} bytes; "
                f"refusing to replace it with {local_path} ({local_size} bytes)"
            )
        content_type = _CONTENT_TYPES.get(local_path.suffix, _DEFAULT_CONTENT_TYPE)
        try:
            with open(local_path, "rb") as f:
                bucket_api.upload(object_path, f, {"content-type": content_type})
        except StorageApiError as err:
            if str(err.status) == "413":
                raise ObjectTooLargeError(
                    f"{bucket}/{object_path} ({local_path.stat().st_size} bytes) "
                    f"is over the storage size limit: {err.message}"
                ) from err
            if str(err.status) == "409":
                raise ObjectAlreadyStoredError(
                    f"{bucket}/{object_path} is already stored but not readable "
                    f"by this login: {err.message}"
                ) from err
            raise


def _stored_size(bucket_api: Any, object_path: str) -> int | None:
    """Byte size of the stored object, or None when there is no such object."""
    try:
        info = bucket_api.info(object_path)
    except StorageApiError as err:
        if str(err.status) == "404" or err.code == "not_found":
            return None
        raise
    size = info.get("size")
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


@dataclass
class SupabaseClients:
    storage: SupabaseStorage
    status: SupabaseStatus


class SessionRevokedError(Exception):
    """The pendant's login no longer exists: removed in the app, or re-paired."""


def _session_persister(session_path: Path) -> Callable[[str, Any], None]:
    """An auth listener that keeps ``session.json`` on the newest tokens.

    Supabase rotates the refresh token on every refresh and accepts only the
    newest one (plus its immediate predecessor), so a pendant that kept only
    the token it was onboarded with lost its login on the first reboot after
    two refreshes - about two hours of running. Events without a session (a
    sign-out) leave the file alone, so a transient revoke is not turned into
    a deleted login. The lock serialises supabase-py's auto-refresh timer
    thread with any other refresh.
    """
    lock = threading.Lock()

    def persist(event: str, session: Any) -> None:
        if session is None:
            return
        with lock:
            write_session_credentials(
                session_path, session.access_token, session.refresh_token
            )
        _logger.info("session %s; saved the rotated tokens", event.lower())

    return persist


def build_supabase_clients(
    url: str,
    key: str,
    session_path: Path,
    client_factory: Callable[[str, str], Any] = create_client,
) -> SupabaseClients:
    client = client_factory(url, key)
    session = json.loads(session_path.read_text())
    # Registered before set_session, which refreshes an expired access token
    # before it returns: that first rotation has to be saved too.
    client.auth.on_auth_state_change(_session_persister(session_path))
    try:
        client.auth.set_session(session["access_token"], session["refresh_token"])
    except AuthApiError as err:
        # A definite 4xx means the device user, its session or its refresh
        # token is gone. A 429, a 5xx or a network error (AuthRetryableError)
        # is not a verdict on the login and must never unpair the pendant.
        if 400 <= err.status < 500 and err.status != 429:
            raise SessionRevokedError(
                f"the pendant's login was refused ({err.status}: {err.message})"
            ) from err
        raise
    return SupabaseClients(
        storage=SupabaseStorage(client),
        status=SupabaseStatus(client),
    )
