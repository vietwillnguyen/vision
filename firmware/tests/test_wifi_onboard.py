import json
import stat

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
    bad_payload = "not json but contains hunter2 and refresh-xyz"
    with pytest.raises(QrDecodeError) as exc_info:
        parse_onboarding_qr_payload(bad_payload)
    message = str(exc_info.value)
    assert "hunter2" not in message
    assert "refresh-xyz" not in message
    assert "not valid JSON" in message


def test_parse_qr_payload_missing_fields_raises():
    payload = json.dumps(
        {
            "ssid": "HomeNet",
            "password": "hunter2",
            "user_refresh_token": "refresh-xyz",
        }
    )
    with pytest.raises(QrDecodeError) as exc_info:
        parse_onboarding_qr_payload(payload)
    message = str(exc_info.value)
    assert "hunter2" not in message
    assert "refresh-xyz" not in message
    assert "missing required field" in message
    assert "user_access_token" in message


@pytest.mark.parametrize("field", ["ssid", "password", "user_access_token", "user_refresh_token"])
def test_parse_qr_payload_non_string_field_raises(field):
    data = json.loads(VALID_PAYLOAD)
    data[field] = 123
    with pytest.raises(QrDecodeError) as exc_info:
        parse_onboarding_qr_payload(json.dumps(data))
    message = str(exc_info.value)
    assert "hunter2" not in message
    assert "refresh-xyz" not in message
    assert field in message


def test_render_wpa_supplicant_includes_ssid_and_password():
    content = render_wpa_supplicant("HomeNet", "hunter2pass")
    assert 'ssid="HomeNet"' in content
    assert 'psk="hunter2pass"' in content
    assert "network={" in content


def test_write_wpa_supplicant_writes_file(tmp_path):
    path = tmp_path / "wpa_supplicant.conf"
    write_wpa_supplicant(path, "HomeNet", "hunter2pass")
    assert 'ssid="HomeNet"' in path.read_text()


def test_write_wpa_supplicant_sets_restrictive_permissions(tmp_path):
    path = tmp_path / "wpa_supplicant.conf"
    write_wpa_supplicant(path, "HomeNet", "hunter2pass")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_session_credentials_writes_json(tmp_path):
    path = tmp_path / "session.json"
    write_session_credentials(path, "access-abc", "refresh-xyz")
    saved = json.loads(path.read_text())
    assert saved == {"access_token": "access-abc", "refresh_token": "refresh-xyz"}


def test_write_session_credentials_sets_restrictive_permissions(tmp_path):
    path = tmp_path / "session.json"
    write_session_credentials(path, "access-abc", "refresh-xyz")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("writer", ["wpa_supplicant", "session_credentials"])
def test_writers_tighten_permissions_on_pre_existing_loose_file(tmp_path, writer):
    path = tmp_path / "target"
    path.touch(mode=0o644)
    path.chmod(0o644)
    if writer == "wpa_supplicant":
        write_wpa_supplicant(path, "HomeNet", "hunter2pass")
    else:
        write_session_credentials(path, "access-abc", "refresh-xyz")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "ssid,password",
    [
        ('Home"Net', "hunter2pass"),
        ("HomeNet", 'hunter"2pass'),
        ("Home\nNet", "hunter2pass"),
        ("HomeNet", "hunter\r2pass"),
        ("Home\nNet", "hunter\r2pass"),
        ("Home\\Net", "hunter2pass"),
        ("HomeNet", "hunter\\2pass"),
        ("HomeNet", "hunter2pass\\"),
    ],
)
def test_render_wpa_supplicant_rejects_unsafe_characters(ssid, password):
    with pytest.raises(ValueError):
        render_wpa_supplicant(ssid, password)


def test_write_wpa_supplicant_rejects_unsafe_characters(tmp_path):
    path = tmp_path / "wpa_supplicant.conf"
    with pytest.raises(ValueError):
        write_wpa_supplicant(path, 'Home"Net', "hunter2pass")


@pytest.mark.parametrize("password", ["", "short77", "a" * 64])
def test_render_wpa_supplicant_rejects_out_of_range_psk_length(password):
    with pytest.raises(ValueError) as exc_info:
        render_wpa_supplicant("HomeNet", password)
    message = str(exc_info.value)
    assert "8-63" in message
    if password:
        assert password not in message


@pytest.mark.parametrize("password", ["a" * 8, "a" * 63])
def test_render_wpa_supplicant_accepts_boundary_psk_lengths(password):
    content = render_wpa_supplicant("HomeNet", password)
    assert f'psk="{password}"' in content


def test_write_wpa_supplicant_rejects_out_of_range_psk_length(tmp_path):
    path = tmp_path / "wpa_supplicant.conf"
    with pytest.raises(ValueError):
        write_wpa_supplicant(path, "HomeNet", "short77")
    assert not path.exists()
