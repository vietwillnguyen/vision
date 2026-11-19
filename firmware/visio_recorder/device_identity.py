"""The pendant's device id, as adopted from ``claim_pairing``.

The id is the owner's ``devices`` row that the pairing code named; the pendant
never invents one. The ``"pairing": 2`` marker distinguishes a file written by
a finished pairing from the one firmware wrote when it borrowed the owner's
session, so a pendant onboarded the old way is treated as unpaired.
"""

import json
from pathlib import Path

from visio_recorder.private_files import write_private_text_atomic

PAIRING_MARKER = 2


def _read(state_path: Path) -> dict[str, object] | None:
    try:
        data = json.loads(state_path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_paired_device_id(state_path: Path) -> str | None:
    """The paired device id, or None for a missing, corrupt or legacy file."""
    data = _read(state_path)
    if data is None:
        return None
    device_id = data.get("device_id")
    marker = data.get("pairing")
    if not isinstance(device_id, str) or not device_id:
        return None
    if type(marker) is not int or marker != PAIRING_MARKER:
        return None
    return device_id


def store_paired_device_id(state_path: Path, device_id: str) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    write_private_text_atomic(
        state_path, json.dumps({"device_id": device_id, "pairing": PAIRING_MARKER})
    )


def drop_pairing_marker(state_path: Path) -> None:
    """Keep the device id for diagnosis but stop treating it as paired."""
    device_id = load_paired_device_id(state_path)
    if device_id is not None:
        write_private_text_atomic(state_path, json.dumps({"device_id": device_id}))
