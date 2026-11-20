import json

from tests.fakes import FakeCommandRunner, FakeLedDriver
from visio_recorder.led import LedPattern
from visio_recorder.onboarding import (
    NmcliConnectionActivator,
    activate_connection,
    reactivate_connection_if_configured,
    run_onboarding,
)
from visio_recorder.pairing import read_pending_pairing

VALID_PAYLOAD = json.dumps(
    {"v": 2, "ssid": "HomeNet", "password": "hunter2secret", "pairing_code": "code-1"}
)

# What an app from before the pairing change shows: the owner's own session.
V1_PAYLOAD = json.dumps(
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
        self.scans = 0

    def scan(self):
        self.scans += 1
        return self._results.pop(0) if self._results else None


def test_run_onboarding_writes_keyfile_and_pending_code_on_first_good_scan(tmp_path):
    scanner = FakeScanner([None, "not json", VALID_PAYLOAD])
    keyfile = tmp_path / "visio.nmconnection"
    pending = tmp_path / "pairing.json"

    payload = run_onboarding(scanner, keyfile, pending, max_attempts=5)

    assert payload is not None and payload.ssid == "HomeNet"
    assert "psk=hunter2secret" in keyfile.read_text()
    assert read_pending_pairing(pending) == "code-1"
    # The pendant's login comes from pairing, never from the QR.
    assert not (tmp_path / "session.json").exists()


def test_run_onboarding_keeps_scanning_past_a_qr_from_an_older_app(tmp_path):
    scanner = FakeScanner([V1_PAYLOAD, VALID_PAYLOAD])
    keyfile = tmp_path / "visio.nmconnection"
    pending = tmp_path / "pairing.json"

    payload = run_onboarding(scanner, keyfile, pending, max_attempts=5)

    assert payload is not None
    assert scanner.scans == 2
    assert read_pending_pairing(pending) == "code-1"
    assert not (tmp_path / "session.json").exists()


def test_run_onboarding_treats_invalid_credentials_as_failed_attempt(tmp_path):
    payload_with_short_password = json.dumps(
        {"v": 2, "ssid": "HomeNet", "password": "short", "pairing_code": "code-1"}
    )
    scanner = FakeScanner([payload_with_short_password])
    keyfile = tmp_path / "visio.nmconnection"
    pending = tmp_path / "pairing.json"

    payload = run_onboarding(scanner, keyfile, pending, max_attempts=1)

    assert payload is None
    assert not keyfile.exists()
    assert not pending.exists()


def test_run_onboarding_gives_up_after_max_attempts(tmp_path):
    scanner = FakeScanner([None, "garbage"])

    payload = run_onboarding(scanner, tmp_path / "k", tmp_path / "p", max_attempts=2)

    assert payload is None
    assert not (tmp_path / "k").exists()
    assert not (tmp_path / "p").exists()


def test_nmcli_activator_runs_reload_then_up_in_order():
    runner = FakeCommandRunner()
    activator = NmcliConnectionActivator(runner)

    activator.activate("visio")

    assert runner.calls == [
        ["nmcli", "connection", "reload"],
        ["nmcli", "connection", "up", "visio"],
    ]


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


def test_reactivate_retries_reload_and_up_when_keyfile_exists(tmp_path):
    # Restart boots must retry activation: a failed first-boot reload leaves
    # the profile unloaded, and NM does not pick up dropped-in keyfiles alone.
    keyfile = tmp_path / "visio.nmconnection"
    keyfile.touch()
    runner = FakeCommandRunner()

    reactivate_connection_if_configured(
        NmcliConnectionActivator(runner), "visio", keyfile
    )

    assert runner.calls == [
        ["nmcli", "connection", "reload"],
        ["nmcli", "connection", "up", "visio"],
    ]


def test_reactivate_does_nothing_when_keyfile_is_missing(tmp_path):
    runner = FakeCommandRunner()

    reactivate_connection_if_configured(
        NmcliConnectionActivator(runner), "visio", tmp_path / "missing.nmconnection"
    )

    assert runner.calls == []


def test_reactivate_swallows_activation_failure(tmp_path):
    keyfile = tmp_path / "visio.nmconnection"
    keyfile.touch()

    # Best-effort: a restart boot may already be online via autoconnect, so a
    # failed retry must not raise (or crash the daemon before flush/recording).
    reactivate_connection_if_configured(RaisingActivator(), "visio", keyfile)
