import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Protocol

from visio_recorder.led import LedDriver, LedState, apply_led_state
from visio_recorder.muxer import CommandRunner, SubprocessCommandRunner
from visio_recorder.wifi_onboard import (
    OnboardingPayload,
    QrDecodeError,
    parse_onboarding_qr_payload,
    write_nm_keyfile,
    write_session_credentials,
)

_logger = logging.getLogger(__name__)


class QrScanner(Protocol):
    def scan(self) -> Optional[str]: ...


class RpicamZbarScanner:
    def scan(self) -> Optional[str]:
        with tempfile.TemporaryDirectory() as tmp:
            still = Path(tmp) / "qr.jpg"
            capture = subprocess.run(
                ["rpicam-still", "-n", "-t", "1000", "-o", str(still)]
            )
            if capture.returncode != 0:
                return None
            decode = subprocess.run(
                ["zbarimg", "--raw", "-q", str(still)],
                capture_output=True,
                text=True,
            )
            payload = decode.stdout.strip()
            return payload or None


def run_onboarding(
    scanner: QrScanner,
    nm_keyfile_path: Path,
    session_path: Path,
    max_attempts: int,
) -> Optional[OnboardingPayload]:
    for _ in range(max_attempts):
        raw = scanner.scan()
        if raw is None:
            continue
        try:
            payload = parse_onboarding_qr_payload(raw)
        except QrDecodeError:
            continue
        try:
            write_nm_keyfile(nm_keyfile_path, payload.ssid, payload.password)
            write_session_credentials(
                session_path, payload.user_access_token, payload.user_refresh_token
            )
        except ValueError:
            continue
        return payload
    return None


class ConnectionActivator(Protocol):
    def activate(self, connection_id: str) -> None: ...


class NmcliConnectionActivator:
    """Makes NetworkManager pick up and bring up a freshly written keyfile.

    NetworkManager does not reliably load keyfiles dropped into
    system-connections while running, so onboarding must reload before it can
    bring the profile up.
    """

    def __init__(self, command_runner: CommandRunner | None = None) -> None:
        self._runner = command_runner or SubprocessCommandRunner()

    def activate(self, connection_id: str) -> None:
        self._runner.run(["nmcli", "connection", "reload"])
        self._runner.run(["nmcli", "connection", "up", connection_id])


def activate_connection(
    activator: ConnectionActivator, connection_id: str, led_driver: LedDriver
) -> bool:
    try:
        activator.activate(connection_id)
    except Exception:
        _logger.exception(
            "failed to activate NetworkManager connection %r; device stays offline",
            connection_id,
        )
        apply_led_state(led_driver, LedState.CRITICAL)
        return False
    return True
