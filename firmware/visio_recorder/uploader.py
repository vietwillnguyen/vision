from pathlib import Path
from typing import Protocol


class StorageClient(Protocol):
    def upload(self, bucket: str, object_path: str, local_path: Path) -> None: ...


def upload_segment(client: StorageClient, device_id: str, local_path: Path) -> str:
    object_path = f"{device_id}/{local_path.name}"
    client.upload("segments", object_path, local_path)
    return object_path


class ObjectTooLargeError(Exception):
    """Storage refused the object because it is over its size limit (413)."""


class ObjectAlreadyStoredError(Exception):
    """Storage holds this key already, under a login this one cannot read (409).

    The pendant reads back only what its own login uploaded, so after a
    re-pair an object stored by the previous login looks absent to the
    idempotency check and the upload is answered as a duplicate.
    """


class LoginUnavailableError(Exception):
    """The pendant cannot sign a request right now, so none was sent.

    Raised before a request when the login could not be refreshed (offline)
    or was revoked. Nothing about the file is wrong, so it stays queued and
    the attempt is not an upload failure: it never counts towards the
    Critical LED. See ``visio_recorder.session_store.SessionKeeper``.
    """
