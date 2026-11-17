import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol

from visio_recorder.led import LedDriver, LedState, apply_led_state
from visio_recorder.muxer import CommandRunner, SubprocessCommandRunner
from visio_recorder.wifi_onboard import (
    OnboardingPayload,
    QrDecodeError,
    parse_onboarding_qr_payload,
    validate_credentials,
    write_session_credentials,
)

_logger = logging.getLogger(__name__)


class QrScanner(Protocol):
    def scan(self) -> str | None: ...


class RpicamZbarScanner:
    def scan(self) -> str | None:
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


class WifiConfigurationError(RuntimeError):
    """NetworkManager refused or failed to store the onboarded network."""


class ConnectionLookup(Protocol):
    def exists(self) -> bool: ...


class WifiConfigurator(ConnectionLookup, Protocol):
    def configure(self, ssid: str, password: str) -> None: ...


class NmcliRunner(Protocol):
    def run(self, args: list[str]) -> str: ...


class SubprocessNmcliRunner:
    """Runs nmcli and returns its stdout; stderr goes to the journal."""

    def run(self, args: list[str]) -> str:
        return subprocess.run(
            args, check=True, stdout=subprocess.PIPE, text=True, timeout=30
        ).stdout


class NmcliWifiConfigurator:
    """Stores the onboarded network as a NetworkManager system connection.

    The daemon runs as the unprivileged ``visio`` user, so it cannot write a
    keyfile into the root-only ``/etc/NetworkManager/system-connections/`` -
    and NetworkManager ignores keyfiles not owned by root anyway. It asks
    NetworkManager over D-Bus instead (``nmcli connection add``/``modify``),
    which writes the root-owned keyfile itself. The polkit rule setup-device.sh
    installs (``firmware/polkit/50-visio-recorder.rules``) is what lets the
    ``visio`` user do that without a session.

    The pre-shared key travels on nmcli's argv, the interface NetworkManager
    documents. Errors are re-raised without that argv, because
    ``CalledProcessError`` renders the full command line and the daemon logs
    onboarding failures into journalctl.
    """

    def __init__(
        self,
        runner: NmcliRunner | None = None,
        connection_id: str = "visio",
        interface: str = "wlan0",
    ) -> None:
        self._runner = runner or SubprocessNmcliRunner()
        self._connection_id = connection_id
        self._interface = interface

    def _nmcli(self, verb: str, args: list[str]) -> str:
        try:
            return self._runner.run(args)
        except subprocess.CalledProcessError as exc:
            raise WifiConfigurationError(
                f"nmcli connection {verb} exited {exc.returncode}"
            ) from None
        except (OSError, subprocess.SubprocessError) as exc:
            raise WifiConfigurationError(
                f"nmcli connection {verb} failed: {type(exc).__name__}"
            ) from None

    def exists(self) -> bool:
        names = self._nmcli("show", ["nmcli", "-g", "NAME", "connection", "show"])
        return self._connection_id in names.splitlines()

    def configure(self, ssid: str, password: str) -> None:
        validate_credentials(ssid, password)
        security = ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
        autoconnect = ["connection.autoconnect", "yes"]
        if self.exists():
            self._nmcli(
                "modify",
                ["nmcli", "connection", "modify", self._connection_id,
                 "wifi.ssid", ssid, *security, *autoconnect],
            )  # fmt: skip
            return
        self._nmcli(
            "add",
            ["nmcli", "connection", "add", "type", "wifi",
             "con-name", self._connection_id, "ifname", self._interface,
             "ssid", ssid, *security, *autoconnect],
        )  # fmt: skip


def run_onboarding(
    scanner: QrScanner,
    wifi: WifiConfigurator,
    session_path: Path,
    max_attempts: int,
) -> OnboardingPayload | None:
    """Scan until a valid QR arrives, then store its network and session.

    An invalid payload costs one attempt; a NetworkManager refusal ends
    onboarding, because rescanning cannot fix it and writing the session
    anyway would make every restart skip onboarding while still offline.
    """
    for _ in range(max_attempts):
        raw = scanner.scan()
        if raw is None:
            continue
        try:
            payload = parse_onboarding_qr_payload(raw)
        except QrDecodeError:
            continue
        try:
            wifi.configure(payload.ssid, payload.password)
        except ValueError:
            continue
        except WifiConfigurationError:
            _logger.exception("NetworkManager did not store the onboarded network")
            return None
        try:
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
    """Brings the onboarded connection up.

    No ``nmcli connection reload`` first: the profile was created through
    NetworkManager, so it is already loaded, and NetworkManager's D-Bus policy
    refuses ReloadConnections to every non-root caller.
    """

    def __init__(self, command_runner: CommandRunner | None = None) -> None:
        self._runner = command_runner or SubprocessCommandRunner()

    def activate(self, connection_id: str) -> None:
        self._runner.run(["nmcli", "connection", "up", connection_id])


def reactivate_connection_if_configured(
    activator: ConnectionActivator, connection_id: str, wifi: ConnectionLookup
) -> None:
    """Best-effort activation retry for restart boots.

    A first boot can fail activation after the connection is stored (e.g. the
    daemon races NetworkManager coming up), so restart boots retry
    ``connection up`` whenever the profile exists. Failure is logged but never
    fatal: the device may already be online via autoconnect.
    """
    try:
        if not wifi.exists():
            return
        activator.activate(connection_id)
    except Exception:
        _logger.exception(
            "best-effort reactivation of NetworkManager connection %r failed",
            connection_id,
        )


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
