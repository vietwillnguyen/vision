import json
import os
import uuid
from pathlib import Path
from typing import Protocol


def _read_stored_device_id(state_path: Path) -> str | None:
    try:
        device_id = json.loads(state_path.read_text())["device_id"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return device_id if isinstance(device_id, str) else None


def load_or_create_device_id(state_path: Path) -> str:
    if state_path.exists():
        stored = _read_stored_device_id(state_path)
        if stored is not None:
            return stored

    device_id = str(uuid.uuid4())
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_name(state_path.name + ".tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps({"device_id": device_id}))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, state_path)
    return device_id


class DeviceRegistrationClient(Protocol):
    def upsert_device(self, device_id: str, name: str) -> None: ...


def register_device(
    client: DeviceRegistrationClient, device_id: str, name: str
) -> None:
    client.upsert_device(device_id, name)
