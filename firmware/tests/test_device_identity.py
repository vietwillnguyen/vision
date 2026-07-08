import json
import uuid

import pytest

from visio_recorder.device_identity import (
    DeviceRegistrationClient,
    load_or_create_device_id,
    register_device,
)


def test_load_or_create_device_id_generates_a_valid_uuid_when_missing(tmp_path):
    state_path = tmp_path / "device_id.json"

    device_id = load_or_create_device_id(state_path)

    assert uuid.UUID(device_id)
    assert state_path.exists()


def test_load_or_create_device_id_returns_the_same_id_on_subsequent_calls(tmp_path):
    state_path = tmp_path / "device_id.json"

    first = load_or_create_device_id(state_path)
    second = load_or_create_device_id(state_path)

    assert first == second


def test_load_or_create_device_id_reads_a_pre_existing_file(tmp_path):
    state_path = tmp_path / "device_id.json"
    state_path.write_text(
        json.dumps({"device_id": "11111111-1111-1111-1111-111111111111"})
    )

    assert (
        load_or_create_device_id(state_path) == "11111111-1111-1111-1111-111111111111"
    )


@pytest.mark.parametrize("corrupt_content", ["", '{"device_id', '{"other": 1}'])
def test_load_or_create_device_id_recovers_from_a_corrupt_file(
    tmp_path, corrupt_content
):
    state_path = tmp_path / "device_id.json"
    state_path.write_text(corrupt_content)

    device_id = load_or_create_device_id(state_path)

    assert uuid.UUID(device_id)
    assert json.loads(state_path.read_text()) == {"device_id": device_id}


def test_load_or_create_device_id_leaves_no_temp_files(tmp_path):
    state_path = tmp_path / "device_id.json"

    load_or_create_device_id(state_path)

    assert [p.name for p in tmp_path.iterdir()] == ["device_id.json"]


class FakeDeviceRegistrationClient(DeviceRegistrationClient):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def upsert_device(self, device_id: str, name: str) -> None:
        self.calls.append((device_id, name))


def test_register_device_calls_client_with_id_and_name():
    client = FakeDeviceRegistrationClient()

    register_device(client, "11111111-1111-1111-1111-111111111111", "visio-pendant")

    assert client.calls == [("11111111-1111-1111-1111-111111111111", "visio-pendant")]
