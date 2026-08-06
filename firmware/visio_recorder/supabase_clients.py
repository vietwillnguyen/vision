import json
import mimetypes
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from supabase import create_client

# `.mp4` segments resolve through mimetypes; `FLAG_*.marker` files, which ride
# the same uploader and bucket, resolve to nothing, so they need a fallback.
_DEFAULT_CONTENT_TYPE = "application/octet-stream"


# `client` below is supabase-py's Client in production and a fake in tests.
# Annotating it as Client would reject the fakes these adapters are tested with,
# and supabase-py ships no Protocol for its fluent query/storage builders - so
# the untyped boundary is stated explicitly as Any rather than left implicit.
class SupabaseStorage:
    def __init__(self, client: Any) -> None:
        self._client = client

    def upload(self, bucket: str, object_path: str, local_path: Path) -> None:
        # Both options correct a storage3 default, and both matter on a device
        # that loses power mid-upload.
        #
        # x-upsert defaults to "false", which makes a re-upload of an existing
        # object a hard error rather than a no-op. The queue's contract is that
        # a file stays queued until mark_uploaded unlinks it, so power cut
        # between a successful upload and that unlink leaves the object remote
        # and the file queued - and every retry from then on fails on an object
        # that is already exactly what we wanted to store. Permanently: nothing
        # in the retry path ever removes a file it did not upload. Upserting
        # makes the retry idempotent, which is what the queue already assumes.
        #
        # content-type has to be set alongside it rather than following from
        # it: storage3 computes its headers as {**DEFAULT_FILE_OPTIONS,
        # **file_options}, so an unset key keeps the default of
        # "text/plain;charset=UTF-8" and every segment lands mislabelled.
        file_options = {
            "x-upsert": "true",
            "content-type": mimetypes.guess_type(local_path.name)[0]
            or _DEFAULT_CONTENT_TYPE,
        }
        with open(local_path, "rb") as f:
            self._client.storage.from_(bucket).upload(
                object_path, f.read(), file_options
            )


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
