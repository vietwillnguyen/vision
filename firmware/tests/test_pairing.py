import json
import stat

import pytest

from visio_recorder.device_identity import store_paired_device_id
from visio_recorder.pairing import (
    PairingState,
    clear_local_pairing,
    pairing_state,
    read_pending_pairing,
    write_pending_pairing,
)
from visio_recorder.wifi_onboard import write_session_credentials

DEVICE_ID = "11111111-1111-1111-1111-111111111111"
CODE = "one-time-pairing-code"


def _session(data_dir):
    write_session_credentials(data_dir / "session.json", "access", "refresh")


# The state table from the spec's "On-disk state".


def test_an_empty_data_dir_is_unpaired(tmp_path):
    assert pairing_state(tmp_path) is PairingState.UNPAIRED


def test_a_pending_pairing_file_is_pending(tmp_path):
    write_pending_pairing(tmp_path / "pairing.json", CODE)

    assert pairing_state(tmp_path) is PairingState.PENDING


def test_a_pending_pairing_file_wins_over_everything(tmp_path):
    store_paired_device_id(tmp_path / "device.json", DEVICE_ID)
    _session(tmp_path)
    write_pending_pairing(tmp_path / "pairing.json", CODE)

    assert pairing_state(tmp_path) is PairingState.PENDING


def test_a_marked_device_file_and_a_session_is_paired(tmp_path):
    store_paired_device_id(tmp_path / "device.json", DEVICE_ID)
    _session(tmp_path)

    assert pairing_state(tmp_path) is PairingState.PAIRED


def test_a_marked_device_file_without_a_session_is_unpaired(tmp_path):
    store_paired_device_id(tmp_path / "device.json", DEVICE_ID)

    assert pairing_state(tmp_path) is PairingState.UNPAIRED


def test_a_session_without_a_device_file_is_unpaired(tmp_path):
    _session(tmp_path)

    assert pairing_state(tmp_path) is PairingState.UNPAIRED


def test_a_legacy_device_file_and_the_owners_session_is_unpaired(tmp_path):
    # A pendant onboarded under the old contract: new firmware must rescan.
    (tmp_path / "device.json").write_text(json.dumps({"device_id": DEVICE_ID}))
    _session(tmp_path)

    assert pairing_state(tmp_path) is PairingState.UNPAIRED


def test_a_corrupt_device_file_is_unpaired(tmp_path):
    (tmp_path / "device.json").write_text('{"device_id')
    _session(tmp_path)

    assert pairing_state(tmp_path) is PairingState.UNPAIRED


def test_a_corrupt_pending_pairing_file_is_unpaired(tmp_path):
    # Nothing to claim with, so the only way forward is a fresh scan.
    (tmp_path / "pairing.json").write_text("{not json")

    assert pairing_state(tmp_path) is PairingState.UNPAIRED


# The pending pairing file.


def test_write_pending_pairing_round_trips_private_and_atomic(tmp_path):
    path = tmp_path / "pairing.json"

    write_pending_pairing(path, CODE)

    assert read_pending_pairing(path) == CODE
    assert json.loads(path.read_text()) == {"pairing_code": CODE}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["pairing.json"]


def test_read_pending_pairing_of_a_missing_file_is_none(tmp_path):
    assert read_pending_pairing(tmp_path / "pairing.json") is None


@pytest.mark.parametrize(
    "content",
    ["", "{not json", "[]", "{}", '{"pairing_code": ""}', '{"pairing_code": 5}'],
)
def test_read_pending_pairing_of_a_corrupt_file_is_none(tmp_path, content):
    path = tmp_path / "pairing.json"
    path.write_text(content)

    assert read_pending_pairing(path) is None


# Clearing the local pairing.


def test_clear_local_pairing_returns_a_paired_pendant_to_unpaired(tmp_path):
    store_paired_device_id(tmp_path / "device.json", DEVICE_ID)
    _session(tmp_path)
    write_pending_pairing(tmp_path / "pairing.json", CODE)
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir()
    (queue_dir / "seg.mp4").write_bytes(b"footage")

    clear_local_pairing(tmp_path)

    assert not (tmp_path / "session.json").exists()
    assert not (tmp_path / "pairing.json").exists()
    assert json.loads((tmp_path / "device.json").read_text()) == {
        "device_id": DEVICE_ID
    }
    assert pairing_state(tmp_path) is PairingState.UNPAIRED
    assert (queue_dir / "seg.mp4").read_bytes() == b"footage"


def test_clear_local_pairing_of_an_empty_data_dir_is_a_no_op(tmp_path):
    clear_local_pairing(tmp_path)

    assert list(tmp_path.iterdir()) == []
