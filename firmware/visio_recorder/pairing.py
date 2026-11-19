"""Pairing the pendant with its own login.

The app's QR carries a one-time pairing code. Onboarding saves it to
``pairing.json``; once Wi-Fi is up the pendant signs in anonymously and claims
the code, which links its new login to the owner's device and returns that
device's id. docs/superpowers/specs/2026-11-19-visio-device-pairing-design.md
"""

import json
from enum import Enum
from pathlib import Path

from visio_recorder.device_identity import drop_pairing_marker, load_paired_device_id
from visio_recorder.wifi_onboard import _write_private_text_atomic

PAIRING_FILE = "pairing.json"
SESSION_FILE = "session.json"
DEVICE_FILE = "device.json"


class PairingState(Enum):
    # Nothing to resume: delete any session and scan for a QR.
    UNPAIRED = "unpaired"
    # A good scan saved a code that has not been claimed yet.
    PENDING = "pending"
    # A finished pairing: the normal boot.
    PAIRED = "paired"


def write_pending_pairing(path: Path, pairing_code: str) -> None:
    _write_private_text_atomic(path, json.dumps({"pairing_code": pairing_code}))


def read_pending_pairing(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    code = data.get("pairing_code")
    return code if isinstance(code, str) and code else None


def pairing_state(data_dir: Path) -> PairingState:
    """Which of the three boot paths this data directory is on.

    A pending code wins over everything, so a power cut after the claim but
    before ``pairing.json`` was deleted resumes the claim (which is
    idempotent). A ``pairing.json`` with no readable code cannot be resumed,
    so it counts as unpaired and the pendant scans again.
    """
    if read_pending_pairing(data_dir / PAIRING_FILE) is not None:
        return PairingState.PENDING
    if (
        load_paired_device_id(data_dir / DEVICE_FILE) is not None
        and (data_dir / SESSION_FILE).exists()
    ):
        return PairingState.PAIRED
    return PairingState.UNPAIRED


def clear_local_pairing(data_dir: Path) -> None:
    """Forget this pendant's login and pairing; the queue is left alone.

    Queued footage uploads to whichever account the pendant is next paired
    to, which adds no exposure: whoever holds the pendant holds the SD card.
    """
    (data_dir / SESSION_FILE).unlink(missing_ok=True)
    (data_dir / PAIRING_FILE).unlink(missing_ok=True)
    drop_pairing_marker(data_dir / DEVICE_FILE)
