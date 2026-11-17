import json
import subprocess

import pytest

from tests.fakes import FakeCommandRunner, FakeLedDriver
from visio_recorder.led import LedPattern
from visio_recorder.onboarding import (
    NmcliConnectionActivator,
    NmcliWifiConfigurator,
    WifiConfigurationError,
    activate_connection,
    reactivate_connection_if_configured,
    run_onboarding,
)
from visio_recorder.wifi_onboard import validate_credentials

VALID_PAYLOAD = json.dumps(
    {
        "ssid": "HomeNet",
        "password": "hunter2secret",
        "user_access_token": "at-123",
        "user_refresh_token": "rt-456",
    }
)


class FakeScanner:
    def __init__(self, results):
        self._results = list(results)

    def scan(self):
        return self._results.pop(0) if self._results else None


class FakeWifi:
    """Records what onboarding asks NetworkManager for, without nmcli."""

    def __init__(self, error: Exception | None = None, exists: bool = False):
        self.configured: list[tuple[str, str]] = []
        self._error = error
        self._exists = exists

    def configure(self, ssid: str, password: str) -> None:
        validate_credentials(ssid, password)
        if self._error is not None:
            raise self._error
        self.configured.append((ssid, password))

    def exists(self) -> bool:
        return self._exists


class ScriptedNmcli:
    """An nmcli stand-in: records argv, answers ``connection show``."""

    def __init__(self, names: list[str] | None = None, fail_on: str | None = None):
        self.calls: list[list[str]] = []
        self._names = names or []
        self._fail_on = fail_on

    def run(self, args: list[str]) -> str:
        self.calls.append(args)
        if self._fail_on is not None and self._fail_on in args:
            raise subprocess.CalledProcessError(4, args, output="", stderr="")
        if args[:4] == ["nmcli", "-g", "NAME", "connection"]:
            return "".join(f"{name}\n" for name in self._names)
        return ""


def test_run_onboarding_configures_wifi_and_writes_session_on_first_good_scan(
    tmp_path,
):
    scanner = FakeScanner([None, "not json", VALID_PAYLOAD])
    wifi = FakeWifi()
    session = tmp_path / "session.json"

    payload = run_onboarding(scanner, wifi, session, max_attempts=5)

    assert payload is not None and payload.ssid == "HomeNet"
    assert wifi.configured == [("HomeNet", "hunter2secret")]
    assert json.loads(session.read_text()) == {
        "access_token": "at-123",
        "refresh_token": "rt-456",
    }


def test_run_onboarding_treats_invalid_credentials_as_failed_attempt(tmp_path):
    payload_with_short_password = json.dumps(
        {
            "ssid": "HomeNet",
            "password": "short",
            "user_access_token": "at-123",
            "user_refresh_token": "rt-456",
        }
    )
    scanner = FakeScanner([payload_with_short_password])
    wifi = FakeWifi()
    session = tmp_path / "session.json"

    payload = run_onboarding(scanner, wifi, session, max_attempts=1)

    assert payload is None
    assert wifi.configured == []
    assert not session.exists()


def test_run_onboarding_gives_up_after_max_attempts(tmp_path):
    scanner = FakeScanner([None, "garbage"])

    payload = run_onboarding(scanner, FakeWifi(), tmp_path / "s", max_attempts=2)

    assert payload is None
    assert not (tmp_path / "s").exists()


def test_a_networkmanager_refusal_stops_onboarding_without_a_session(tmp_path):
    # A polkit denial does not get better on the next QR scan, and writing the
    # session anyway would make every restart skip onboarding while offline.
    scanner = FakeScanner([VALID_PAYLOAD, VALID_PAYLOAD])
    wifi = FakeWifi(error=WifiConfigurationError("nmcli connection add exited 4"))
    session = tmp_path / "session.json"

    payload = run_onboarding(scanner, wifi, session, max_attempts=2)

    assert payload is None
    assert not session.exists()


def test_configure_adds_a_system_connection_when_none_exists():
    nmcli = ScriptedNmcli(names=["lo", "Wired connection 1"])

    NmcliWifiConfigurator(nmcli).configure("HomeNet", "hunter2secret")

    assert nmcli.calls == [
        ["nmcli", "-g", "NAME", "connection", "show"],
        [
            "nmcli", "connection", "add", "type", "wifi", "con-name", "visio",
            "ifname", "wlan0", "ssid", "HomeNet",
            "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", "hunter2secret",
            "connection.autoconnect", "yes",
        ],
    ]  # fmt: skip


def test_configure_modifies_the_existing_connection_on_re_onboarding():
    # Also the migration path: the root-era keyfile NetworkManager already
    # loaded has id "visio", so re-onboarding rewrites it rather than adding a
    # second profile of the same name.
    nmcli = ScriptedNmcli(names=["visio"])

    NmcliWifiConfigurator(nmcli).configure("OtherNet", "newsecret99")

    assert nmcli.calls[1] == [
        "nmcli", "connection", "modify", "visio",
        "wifi.ssid", "OtherNet",
        "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", "newsecret99",
        "connection.autoconnect", "yes",
    ]  # fmt: skip


def test_configure_rejects_invalid_credentials_before_calling_nmcli():
    nmcli = ScriptedNmcli()

    with pytest.raises(ValueError):
        NmcliWifiConfigurator(nmcli).configure("HomeNet", "short")

    assert nmcli.calls == []


def test_a_failed_nmcli_call_never_carries_the_password_in_its_error():
    # CalledProcessError renders the full argv, psk included, and the daemon
    # logs onboarding failures with a traceback into journalctl.
    nmcli = ScriptedNmcli(fail_on="add")

    with pytest.raises(WifiConfigurationError) as excinfo:
        NmcliWifiConfigurator(nmcli).configure("HomeNet", "hunter2secret")

    error = excinfo.value
    # Neither the message nor a chained CalledProcessError may reach a
    # traceback: logging.exception renders both.
    assert "hunter2secret" not in str(error)
    assert error.__cause__ is None and error.__suppress_context__
    assert "exited 4" in str(error)


def test_exists_matches_the_profile_name_exactly():
    assert NmcliWifiConfigurator(ScriptedNmcli(names=["visio"])).exists()
    assert not NmcliWifiConfigurator(ScriptedNmcli(names=["visio-old"])).exists()


def test_nmcli_activator_only_brings_the_connection_up():
    # No `nmcli connection reload`: NetworkManager's D-Bus policy refuses
    # ReloadConnections to every non-root caller, and a profile created
    # through nmcli is already loaded.
    runner = FakeCommandRunner()
    activator = NmcliConnectionActivator(runner)

    activator.activate("visio")

    assert runner.calls == [["nmcli", "connection", "up", "visio"]]


def test_activate_connection_returns_true_and_leaves_led_alone_on_success():
    led = FakeLedDriver()

    ok = activate_connection(
        NmcliConnectionActivator(FakeCommandRunner()), "visio", led
    )

    assert ok is True
    assert led.calls == []


class RaisingActivator:
    def activate(self, connection_id: str) -> None:
        raise RuntimeError("nmcli failed")


def test_activate_connection_surfaces_failure_via_critical_led():
    led = FakeLedDriver()

    ok = activate_connection(RaisingActivator(), "visio", led)

    assert ok is False
    assert led.calls == [((255, 0, 0), LedPattern.FLASHING)]


def test_reactivate_retries_up_when_the_connection_exists():
    # Restart boots retry activation: the first boot can race NetworkManager.
    runner = FakeCommandRunner()

    reactivate_connection_if_configured(
        NmcliConnectionActivator(runner), "visio", FakeWifi(exists=True)
    )

    assert runner.calls == [["nmcli", "connection", "up", "visio"]]


def test_reactivate_does_nothing_when_the_connection_is_missing():
    runner = FakeCommandRunner()

    reactivate_connection_if_configured(
        NmcliConnectionActivator(runner), "visio", FakeWifi(exists=False)
    )

    assert runner.calls == []


def test_reactivate_swallows_activation_failure():
    # Best-effort: a restart boot may already be online via autoconnect, so a
    # failed retry must not raise (or crash the daemon before flush/recording).
    reactivate_connection_if_configured(
        RaisingActivator(), "visio", FakeWifi(exists=True)
    )


def test_reactivate_swallows_a_failed_existence_check():
    class Unreachable:
        def exists(self) -> bool:
            raise WifiConfigurationError("nmcli connection show exited 8")

    runner = FakeCommandRunner()

    reactivate_connection_if_configured(
        NmcliConnectionActivator(runner), "visio", Unreachable()
    )

    assert runner.calls == []
