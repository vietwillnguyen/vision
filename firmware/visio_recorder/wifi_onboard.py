import json
from dataclasses import dataclass
from pathlib import Path


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
        return OnboardingPayload(
            ssid=data["ssid"],
            password=data["password"],
            user_access_token=data["user_access_token"],
            user_refresh_token=data["user_refresh_token"],
        )
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise QrDecodeError(f"invalid onboarding QR payload: {payload!r}") from exc


def render_wpa_supplicant(ssid: str, password: str) -> str:
    return (
        "country=US\n"
        "ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\n"
        "update_config=1\n\n"
        "network={\n"
        f'    ssid="{ssid}"\n'
        f'    psk="{password}"\n'
        "}\n"
    )


def write_wpa_supplicant(path: Path, ssid: str, password: str) -> None:
    path.write_text(render_wpa_supplicant(ssid, password))


def write_session_credentials(path: Path, access_token: str, refresh_token: str) -> None:
    path.write_text(json.dumps({"access_token": access_token, "refresh_token": refresh_token}))
