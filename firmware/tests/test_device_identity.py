import json

import pytest

from visio_recorder.device_identity import (
    load_paired_device_id,
    store_paired_device_id,
)

DEVICE_ID = "11111111-1111-1111-1111-111111111111"


def test_store_then_load_round_trips(tmp_path):
    state_path = tmp_path / "device.json"

    store_paired_device_id(state_path, DEVICE_ID)

    assert load_paired_device_id(state_path) == DEVICE_ID
    assert json.loads(state_path.read_text()) == {"device_id": DEVICE_ID, "pairing": 2}


def test_store_replaces_an_existing_file_and_leaves_no_temp_files(tmp_path):
    state_path = tmp_path / "device.json"
    state_path.write_text(json.dumps({"device_id": "legacy"}))

    store_paired_device_id(state_path, DEVICE_ID)

    assert load_paired_device_id(state_path) == DEVICE_ID
    assert [p.name for p in tmp_path.iterdir()] == ["device.json"]


def test_load_of_a_missing_file_is_none(tmp_path):
    assert load_paired_device_id(tmp_path / "device.json") is None


def test_load_of_a_legacy_file_without_the_marker_is_none(tmp_path):
    # Written by firmware that borrowed the owner's session: not a pairing.
    state_path = tmp_path / "device.json"
    state_path.write_text(json.dumps({"device_id": DEVICE_ID}))

    assert load_paired_device_id(state_path) is None


@pytest.mark.parametrize(
    "content",
    [
        "",
        '{"device_id',
        "[]",
        '{"pairing": 2}',
        json.dumps({"device_id": 7, "pairing": 2}),
        json.dumps({"device_id": DEVICE_ID, "pairing": 1}),
        json.dumps({"device_id": DEVICE_ID, "pairing": "2"}),
    ],
)
def test_load_of_a_corrupt_or_foreign_file_is_none(tmp_path, content):
    state_path = tmp_path / "device.json"
    state_path.write_text(content)

    assert load_paired_device_id(state_path) is None
