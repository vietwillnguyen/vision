import json
import stat

import pytest

from visio_recorder.wifi_onboard import (
    OnboardingPayload,
    QrDecodeError,
    parse_onboarding_qr_payload,
    render_nm_keyfile,
    render_wpa_supplicant,
    write_nm_keyfile,
    write_session_credentials,
    write_wpa_supplicant,
)

# Distinctive values, so an assertion can prove an error message leaks neither.
PASSWORD = "hunter2pass"
PAIRING_CODE = "Zq8-one-time-pairing-code_Xy"

VALID_PAYLOAD = json.dumps(
    {"v": 2, "ssid": "HomeNet", "password": PASSWORD, "pairing_code": PAIRING_CODE}
)


def _assert_leaks_no_secret(message: str) -> None:
    assert PASSWORD not in message
    assert PAIRING_CODE not in message


def test_parse_valid_qr_payload():
    result = parse_onboarding_qr_payload(VALID_PAYLOAD)
    assert result == OnboardingPayload(
        ssid="HomeNet", password=PASSWORD, pairing_code=PAIRING_CODE
    )


def test_parse_invalid_qr_payload_raises():
    bad_payload = f"not json but contains {PASSWORD} and {PAIRING_CODE}"
    with pytest.raises(QrDecodeError) as exc_info:
        parse_onboarding_qr_payload(bad_payload)
    message = str(exc_info.value)
    _assert_leaks_no_secret(message)
    assert "not valid JSON" in message


def test_parse_rejects_a_v1_payload_from_an_older_app():
    # Accepting it would put the owner's own session back on the pendant.
    payload = json.dumps(
        {
            "ssid": "HomeNet",
            "password": PASSWORD,
            "user_access_token": "access-abc",
            "user_refresh_token": "refresh-xyz",
        }
    )
    with pytest.raises(QrDecodeError) as exc_info:
        parse_onboarding_qr_payload(payload)
    message = str(exc_info.value)
    _assert_leaks_no_secret(message)
    assert "refresh-xyz" not in message
    assert "older app version" in message


@pytest.mark.parametrize("version", [1, 3, "2", 2.5, True, None])
def test_parse_rejects_an_unsupported_version(version):
    data = json.loads(VALID_PAYLOAD)
    data["v"] = version
    with pytest.raises(QrDecodeError) as exc_info:
        parse_onboarding_qr_payload(json.dumps(data))
    message = str(exc_info.value)
    _assert_leaks_no_secret(message)
    assert "unsupported QR version" in message


def test_parse_rejects_a_missing_version_even_with_a_pairing_code():
    data = json.loads(VALID_PAYLOAD)
    del data["v"]
    with pytest.raises(QrDecodeError) as exc_info:
        parse_onboarding_qr_payload(json.dumps(data))
    _assert_leaks_no_secret(str(exc_info.value))


@pytest.mark.parametrize("payload", ["[]", '"text"', "2", "null"])
def test_parse_rejects_a_payload_that_is_not_an_object(payload):
    with pytest.raises(QrDecodeError):
        parse_onboarding_qr_payload(payload)


@pytest.mark.parametrize("field", ["ssid", "password", "pairing_code"])
def test_parse_qr_payload_missing_field_raises(field):
    data = json.loads(VALID_PAYLOAD)
    del data[field]
    with pytest.raises(QrDecodeError) as exc_info:
        parse_onboarding_qr_payload(json.dumps(data))
    message = str(exc_info.value)
    _assert_leaks_no_secret(message)
    assert "missing required field" in message
    assert field in message


@pytest.mark.parametrize("field", ["ssid", "password", "pairing_code"])
def test_parse_qr_payload_non_string_field_raises(field):
    data = json.loads(VALID_PAYLOAD)
    data[field] = 123
    with pytest.raises(QrDecodeError) as exc_info:
        parse_onboarding_qr_payload(json.dumps(data))
    message = str(exc_info.value)
    _assert_leaks_no_secret(message)
    assert field in message


def test_parse_qr_payload_empty_pairing_code_raises():
    data = json.loads(VALID_PAYLOAD)
    data["pairing_code"] = ""
    with pytest.raises(QrDecodeError) as exc_info:
        parse_onboarding_qr_payload(json.dumps(data))
    assert "pairing_code" in str(exc_info.value)


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


def test_render_nm_keyfile_produces_a_wifi_psk_connection():
    keyfile = render_nm_keyfile("HomeNet", "hunter2secret")
    assert "[connection]" in keyfile
    assert "type=wifi" in keyfile
    assert "ssid=HomeNet" in keyfile
    assert "key-mgmt=wpa-psk" in keyfile
    assert "psk=hunter2secret" in keyfile


def test_render_nm_keyfile_rejects_unsafe_characters():
    with pytest.raises(ValueError):
        render_nm_keyfile('Home"Net', "hunter2secret")


def test_render_nm_keyfile_rejects_out_of_range_psk_length():
    with pytest.raises(ValueError):
        render_nm_keyfile("HomeNet", "short")


def test_write_nm_keyfile_is_owner_read_write_only(tmp_path):
    path = tmp_path / "visio.nmconnection"
    write_nm_keyfile(path, "HomeNet", "hunter2secret")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
