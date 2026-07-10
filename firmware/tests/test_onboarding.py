import json
from pathlib import Path

from visio_recorder.onboarding import run_onboarding

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


def test_run_onboarding_writes_keyfile_and_session_on_first_good_scan(tmp_path):
    scanner = FakeScanner([None, "not json", VALID_PAYLOAD])
    keyfile = tmp_path / "visio.nmconnection"
    session = tmp_path / "session.json"

    payload = run_onboarding(scanner, keyfile, session, max_attempts=5)

    assert payload is not None and payload.ssid == "HomeNet"
    assert "psk=hunter2secret" in keyfile.read_text()
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
    keyfile = tmp_path / "visio.nmconnection"
    session = tmp_path / "session.json"

    payload = run_onboarding(scanner, keyfile, session, max_attempts=1)

    assert payload is None
    assert not keyfile.exists()
    assert not session.exists()


def test_run_onboarding_gives_up_after_max_attempts(tmp_path):
    scanner = FakeScanner([None, "garbage"])

    payload = run_onboarding(
        scanner, tmp_path / "k", tmp_path / "s", max_attempts=2
    )

    assert payload is None
    assert not (tmp_path / "k").exists()
