from pathlib import Path
from typing import Protocol


class StorageClient(Protocol):
    def upload(self, bucket: str, object_path: str, local_path: Path) -> None: ...


def upload_segment(client: StorageClient, device_id: str, local_path: Path) -> str:
    object_path = f"{device_id}/{local_path.name}"
    client.upload("segments", object_path, local_path)
    return object_path
