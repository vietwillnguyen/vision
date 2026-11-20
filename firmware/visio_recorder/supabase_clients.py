import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from postgrest.exceptions import APIError
from storage3.exceptions import StorageApiError

from visio_recorder.session_store import (
    LoginRefusedError,
    SessionKeeper,
    SessionRevokedError,
    create_pendant_client,
    read_saved_session,
)
from visio_recorder.uploader import ObjectAlreadyStoredError, ObjectTooLargeError

__all__ = [
    "SessionRevokedError",
    "SupabaseClients",
    "SupabaseStatus",
    "SupabaseStorage",
    "build_supabase_clients",
    "restore_supabase_client",
    "supabase_clients_for",
]

_logger = logging.getLogger(__name__)

# Content types by suffix for everything firmware uploads. Explicit rather than
# `mimetypes`, whose answer depends on the host's /etc/mime.types. Anything
# unlisted (flag markers) is stored as opaque bytes.
_CONTENT_TYPES = {".mp4": "video/mp4"}
_DEFAULT_CONTENT_TYPE = "application/octet-stream"


# PostgREST's code for a request whose JWT has expired.
_POSTGREST_JWT_EXPIRED = "PGRST303"


def _is_expired_token_refusal(err: Exception) -> bool:
    """Whether storage or PostgREST refused the request's expired token."""
    if isinstance(err, StorageApiError):
        return str(err.status) == "403" and '"exp" claim' in str(err.message)
    return isinstance(err, APIError) and err.code == _POSTGREST_JWT_EXPIRED


def _signed(session: SessionKeeper | None, request: Callable[[], None]) -> None:
    """Run ``request`` with a fresh login, refreshing it first when due.

    ``session`` judges freshness by the token's lifetime; a refusal for an
    expired token anyway (a lifetime that ran out sooner than it said) marks
    it stale, and the request is sent once more after a refresh.
    """
    if session is None:
        request()
        return
    session.ensure_fresh()
    try:
        request()
        return
    except (StorageApiError, APIError) as err:
        if not _is_expired_token_refusal(err):
            raise
        _logger.warning("the request's token had expired; refreshing the login")
    session.mark_stale()
    session.ensure_fresh()
    request()


# `client` below is supabase-py's Client in production and a fake in tests.
# Annotating it as Client would reject the fakes these adapters are tested with,
# and supabase-py ships no Protocol for its fluent query/storage builders - so
# the untyped boundary is stated explicitly as Any rather than left implicit.
# `session` is the process's one SessionKeeper; without one (tests, a
# stranger's client) the client's token is used as it is.
class SupabaseStorage:
    def __init__(self, client: Any, session: SessionKeeper | None = None) -> None:
        self._client = client
        self._session = session

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

        A login that is due and cannot be refreshed raises
        ``LoginUnavailableError`` before anything is sent; see
        ``SessionKeeper``.
        """
        _signed(self._session, lambda: self._upload(bucket, object_path, local_path))

    def _upload(self, bucket: str, object_path: str, local_path: Path) -> None:
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
    def __init__(self, client: Any, session: SessionKeeper | None = None) -> None:
        self._client = client
        self._session = session

    def upsert_device_status(self, status: dict[str, Any]) -> None:
        _signed(
            self._session,
            lambda: self._client.table("device_status").upsert(status).execute(),
        )


@dataclass
class SupabaseClients:
    storage: SupabaseStorage
    status: SupabaseStatus
    session: SessionKeeper


def restore_supabase_client(
    url: str,
    key: str,
    session_path: Path,
    client_factory: Callable[[str, str], Any] = create_pendant_client,
) -> SessionKeeper:
    """The pendant's login, signed back in from ``session.json``."""
    saved = read_saved_session(session_path)
    if saved is None:
        raise SessionRevokedError("the pendant's session.json is missing or unreadable")
    # The keeper is registered before the restore, whose refresh rotates the
    # refresh token: that first rotation has to be saved too.
    session = SessionKeeper(client_factory(url, key), session_path)
    try:
        session.restore(saved["refresh_token"])
    except LoginRefusedError as err:
        raise SessionRevokedError(f"the pendant's {err}") from err
    return session


def supabase_clients_for(session: SessionKeeper) -> SupabaseClients:
    return SupabaseClients(
        storage=SupabaseStorage(session.client, session),
        status=SupabaseStatus(session.client, session),
        session=session,
    )


def build_supabase_clients(
    url: str,
    key: str,
    session_path: Path,
    client_factory: Callable[[str, str], Any] = create_pendant_client,
) -> SupabaseClients:
    return supabase_clients_for(
        restore_supabase_client(url, key, session_path, client_factory)
    )
