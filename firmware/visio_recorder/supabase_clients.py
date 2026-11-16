import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
        """Upload ``local_path`` to ``bucket/object_path``.

        storage3's defaults are wrong for this device on every header it sends:
        ``x-upsert: false`` makes the next-boot ``flush_pending`` retry of an
        object that already reached storage fail as a duplicate forever, and
        ``content-type: text/plain`` mislabels every segment. Passing the open
        file rather than ``f.read()`` lets httpx stream a ~170 MB segment in
        chunks instead of holding it (and multipart copies of it) in a 512 MB
        Pi Zero 2W's memory. Object keys are capture timestamps, so an upsert
        only ever overwrites an object with the same file.
        """
        content_type = _CONTENT_TYPES.get(local_path.suffix, _DEFAULT_CONTENT_TYPE)
        with open(local_path, "rb") as f:
            self._client.storage.from_(bucket).upload(
                object_path,
                f,
                {"content-type": content_type, "upsert": "true"},
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
