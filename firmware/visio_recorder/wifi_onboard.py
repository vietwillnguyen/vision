import json
import os
from dataclasses import dataclass
from pathlib import Path

_UNSAFE_CHARS = ('"', "\n", "\r", "\\")
_PSK_MIN_LENGTH = 8
_PSK_MAX_LENGTH = 63


class QrDecodeError(ValueError):
    pass


@dataclass
class OnboardingPayload:
    ssid: str
    password: str
    user_access_token: str
    user_refresh_token: str


def parse_onboarding_qr_payload(payload: str) -> OnboardingPayload:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise QrDecodeError(
            f"invalid onboarding QR payload: not valid JSON (length={len(payload)})"
        ) from exc

    fields = {}
    for field in ("ssid", "password", "user_access_token", "user_refresh_token"):
        try:
            value = data[field]
        except (KeyError, TypeError) as exc:
            raise QrDecodeError(
                f"invalid onboarding QR payload: missing required field {field!r} "
                f"(length={len(payload)})"
            ) from exc
        if not isinstance(value, str):
            raise QrDecodeError(
                f"invalid onboarding QR payload: field {field!r} is not a string "
                f"(length={len(payload)})"
            )
        fields[field] = value
    return OnboardingPayload(**fields)


def render_wpa_supplicant(ssid: str, password: str) -> str:
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


def write_session_credentials(path: Path, access_token: str, refresh_token: str) -> None:
    _write_private_text(
        path, json.dumps({"access_token": access_token, "refresh_token": refresh_token})
    )
