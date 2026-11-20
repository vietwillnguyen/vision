import json
import os
from dataclasses import dataclass
from pathlib import Path

from visio_recorder.private_files import write_private_text_atomic

_UNSAFE_CHARS = ('"', "\n", "\r", "\\")
_PSK_MIN_LENGTH = 8
_PSK_MAX_LENGTH = 63


class QrDecodeError(ValueError):
    pass


# The QR contract the app writes; docs/superpowers/specs/
# 2026-11-19-visio-device-pairing-design.md, "QR payload v2".
QR_PAYLOAD_VERSION = 2


@dataclass(frozen=True)
class OnboardingPayload:
    ssid: str
    password: str
    pairing_code: str


def parse_onboarding_qr_payload(payload: str) -> OnboardingPayload:
    """Parse the app's v2 onboarding QR: Wi-Fi credentials and a pairing code.

    A v1 payload (the owner's own session tokens) is refused outright rather
    than accepted for compatibility: accepting it would put the owner's login
    back on the pendant. Messages carry only the payload length, never a
    value, because the payload holds the Wi-Fi password and a pairing code.
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise QrDecodeError(
            f"invalid onboarding QR payload: not valid JSON (length={len(payload)})"
        ) from exc
    if not isinstance(data, dict):
        raise QrDecodeError(
            f"invalid onboarding QR payload: not a JSON object (length={len(payload)})"
        )

    version = data.get("v")
    # v1 had no version field and carried session tokens instead of a code.
    if "v" not in data and "pairing_code" not in data:
        raise QrDecodeError(
            f"QR is from an older app version; update the app (length={len(payload)})"
        )
    # bool is an int subclass, and "2" is not the JSON number 2.
    if type(version) is not int or version != QR_PAYLOAD_VERSION:
        raise QrDecodeError(
            "invalid onboarding QR payload: unsupported QR version "
            f"(expected {QR_PAYLOAD_VERSION}, length={len(payload)})"
        )

    fields = {}
    for field in ("ssid", "password", "pairing_code"):
        if field not in data:
            raise QrDecodeError(
                f"invalid onboarding QR payload: missing required field {field!r} "
                f"(length={len(payload)})"
            )
        value = data[field]
        if not isinstance(value, str):
            raise QrDecodeError(
                f"invalid onboarding QR payload: field {field!r} is not a string "
                f"(length={len(payload)})"
            )
        fields[field] = value
    if not fields["pairing_code"]:
        raise QrDecodeError(
            "invalid onboarding QR payload: field 'pairing_code' is empty "
            f"(length={len(payload)})"
        )
    return OnboardingPayload(**fields)


def _validate_credentials(ssid: str, password: str) -> None:
    for name, value in (("ssid", ssid), ("password", password)):
        if any(ch in value for ch in _UNSAFE_CHARS):
            raise ValueError(
                f"{name} contains an unsafe character (quote, newline, or backslash); "
                "rejecting rather than attempting to escape it for wpa_supplicant.conf"
            )
    if not _PSK_MIN_LENGTH <= len(password) <= _PSK_MAX_LENGTH:
        raise ValueError(
            f"password length {len(password)} is outside the 8-63 character range "
            "required for a WPA-PSK passphrase; wpa_supplicant would reject the config"
        )


def render_wpa_supplicant(ssid: str, password: str) -> str:
    _validate_credentials(ssid, password)
    return (
        "country=US\n"
        "ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\n"
        "update_config=1\n\n"
        "network={\n"
        f'    ssid="{ssid}"\n'
        f'    psk="{password}"\n'
        "}\n"
    )


def _write_private_text(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        # The mode passed to os.open only applies when the file is newly
        # created; a pre-existing file (e.g. the stock wpa_supplicant.conf
        # shipped at 0o644) keeps its old permissions, so tighten via the fd.
        os.fchmod(f.fileno(), 0o600)
        f.write(content)


def write_wpa_supplicant(path: Path, ssid: str, password: str) -> None:
    _write_private_text(path, render_wpa_supplicant(ssid, password))


def render_nm_keyfile(ssid: str, password: str) -> str:
    _validate_credentials(ssid, password)
    return (
        "[connection]\n"
        "id=visio\n"
        "type=wifi\n"
        "autoconnect=true\n\n"
        "[wifi]\n"
        "mode=infrastructure\n"
        f"ssid={ssid}\n\n"
        "[wifi-security]\n"
        "key-mgmt=wpa-psk\n"
        f"psk={password}\n\n"
        "[ipv4]\n"
        "method=auto\n\n"
        "[ipv6]\n"
        "method=auto\n"
    )


def write_nm_keyfile(path: Path, ssid: str, password: str) -> None:
    _write_private_text(path, render_nm_keyfile(ssid, password))


def write_session_credentials(
    path: Path, access_token: str, refresh_token: str
) -> None:
    """Replace ``session.json`` atomically, readable only by its owner.

    The refresh token rotates on every refresh and the old one stops working,
    so this file is rewritten during normal running, not just at pairing. A
    torn write would leave neither token and the pendant would need pairing
    again, hence the atomic replace.
    """
    content = json.dumps({"access_token": access_token, "refresh_token": refresh_token})
    write_private_text_atomic(path, content)
