import json
import uuid
from pathlib import Path
from typing import Protocol


def load_or_create_device_id(state_path: Path) -> str:
    if state_path.exists():
        return json.loads(state_path.read_text())["device_id"]

    device_id = str(uuid.uuid4())
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"device_id": device_id}))
    return device_id


class DeviceRegistrationClient(Protocol):
    def upsert_device(self, device_id: str, name: str) -> None:
        ...


def register_device(client: DeviceRegistrationClient, device_id: str, name: str) -> None:
    client.upsert_device(device_id, name)
