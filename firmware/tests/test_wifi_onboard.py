import json

import pytest

from visio_recorder.wifi_onboard import (
    QrDecodeError,
    parse_onboarding_qr_payload,
    render_wpa_supplicant,
    write_session_credentials,
    write_wpa_supplicant,
)

VALID_PAYLOAD = json.dumps(
    {
        "ssid": "HomeNet",
        "password": "hunter2",
        "user_access_token": "access-abc",
        "user_refresh_token": "refresh-xyz",
    }
)


def test_parse_valid_qr_payload():
    result = parse_onboarding_qr_payload(VALID_PAYLOAD)
    assert result.ssid == "HomeNet"
    assert result.password == "hunter2"
    assert result.user_access_token == "access-abc"
    assert result.user_refresh_token == "refresh-xyz"


def test_parse_invalid_qr_payload_raises():
    with pytest.raises(QrDecodeError):
        parse_onboarding_qr_payload("not json")


def test_parse_qr_payload_missing_fields_raises():
    with pytest.raises(QrDecodeError):
        parse_onboarding_qr_payload(json.dumps({"ssid": "HomeNet", "password": "hunter2"}))


def test_render_wpa_supplicant_includes_ssid_and_password():
    content = render_wpa_supplicant("HomeNet", "hunter2")
    assert 'ssid="HomeNet"' in content
    assert 'psk="hunter2"' in content
    assert "network={" in content


def test_write_wpa_supplicant_writes_file(tmp_path):
    path = tmp_path / "wpa_supplicant.conf"
    write_wpa_supplicant(path, "HomeNet", "hunter2")
    assert 'ssid="HomeNet"' in path.read_text()


def test_write_session_credentials_writes_json(tmp_path):
    path = tmp_path / "session.json"
    write_session_credentials(path, "access-abc", "refresh-xyz")
    saved = json.loads(path.read_text())
    assert saved == {"access_token": "access-abc", "refresh_token": "refresh-xyz"}
