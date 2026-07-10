import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Protocol

from visio_recorder.wifi_onboard import (
    OnboardingPayload,
    QrDecodeError,
    parse_onboarding_qr_payload,
    write_nm_keyfile,
    write_session_credentials,
)


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
        write_nm_keyfile(nm_keyfile_path, payload.ssid, payload.password)
        write_session_credentials(
            session_path, payload.user_access_token, payload.user_refresh_token
        )
        return payload
    return None
