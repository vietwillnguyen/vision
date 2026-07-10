import json
from dataclasses import dataclass
from pathlib import Path

from supabase import create_client


class SupabaseStorage:
    def __init__(self, client):
        self._client = client

    def upload(self, bucket: str, object_path: str, local_path: Path) -> None:
        with open(local_path, "rb") as f:
            self._client.storage.from_(bucket).upload(object_path, f.read())


class SupabaseStatus:
    def __init__(self, client):
        self._client = client

    def upsert_device_status(self, status: dict) -> None:
        self._client.table("device_status").upsert(status).execute()


class SupabaseRegistration:
    def __init__(self, client, user_id: str):
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
    url: str, key: str, session_path: Path, client_factory=create_client
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
