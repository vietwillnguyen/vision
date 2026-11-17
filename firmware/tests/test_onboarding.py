import json
import os
import subprocess
import sys
from pathlib import Path

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
    """An nmcli stand-in: records argv and stdin, answers the read-backs."""

    def __init__(
        self,
        names: list[str] | None = None,
        fail_on: str | None = None,
        stored_psk: str | None = None,
    ):
        self.calls: list[list[str]] = []
        self.stdins: list[str] = []
        self._names = names or []
        self._fail_on = fail_on
        self._stored_psk = stored_psk

    def run(self, args: list[str], *, stdin: str = "") -> str:
        self.calls.append(args)
        self.stdins.append(stdin)
        if self._fail_on is not None and self._fail_on in args:
            raise subprocess.CalledProcessError(4, args, output="", stderr="")
        if args[:4] == ["nmcli", "-g", "NAME", "connection"]:
            return "".join(f"{name}\n" for name in self._names)
        if "--show-secrets" in args:
            # Default to the key the editor was just handed, so only a test
            # that asks for a mismatch sees one.
            if self._stored_psk is not None:
                return f"{self._stored_psk}\n"
            lines = "".join(self.stdins).splitlines()
            for index, line in enumerate(lines[:-1]):
                if line == "set 802-11-wireless-security.psk":
                    return f"{lines[index + 1]}\n"
            return "\n"
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


class NmcliStub:
    """The real ``nmcli`` replaced by a recording executable on ``PATH``.

    Exercises :class:`SubprocessNmcliRunner` for real, so a test can see what
    onboarding put in the process list and what it kept out of it.
    """

    def __init__(self, record_dir: Path):
        self._dir = record_dir

    def seed(self, name: str, **properties: str) -> None:
        self._dir.joinpath("profiles.json").write_text(json.dumps({name: properties}))

    @property
    def transcript(self) -> list[dict]:
        log = self._dir / "transcript.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def argv(self) -> list[list[str]]:
        return [call["argv"] for call in self.transcript]

    def stdin(self) -> list[str]:
        return [call["stdin"] for call in self.transcript]

    def stored(self, name: str = "visio") -> dict[str, str]:
        store = self._dir / "profiles.json"
        return json.loads(store.read_text()).get(name, {}) if store.exists() else {}


@pytest.fixture
def nmcli_stub(tmp_path, monkeypatch):
    record_dir = tmp_path / "nmcli"
    record_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = Path(__file__).parent / "nmcli_stub.py"
    shim = bin_dir / "nmcli"
    shim.write_text(f'#!/bin/sh\nexec {sys.executable!r} {str(stub)!r} "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("NMCLI_STUB_DIR", str(record_dir))
    return NmcliStub(record_dir)


def test_configure_keeps_the_password_off_nmclis_command_line(nmcli_stub):
    # /proc/<pid>/cmdline is world-readable, so a pre-shared key passed as an
    # nmcli argument is readable by every local user for the life of the call.
    NmcliWifiConfigurator().configure("HomeNet", "hunter2secret")

    assert nmcli_stub.argv(), "the real runner never reached the stub"
    for argv in nmcli_stub.argv():
        assert "hunter2secret" not in " ".join(argv)
    assert any("hunter2secret" in handed for handed in nmcli_stub.stdin())
    assert nmcli_stub.stored()["802-11-wireless-security.psk"] == "hunter2secret"


def test_a_failing_nmcli_still_never_sees_the_password_on_its_command_line(
    nmcli_stub, monkeypatch
):
    # The failure path through the real runner: nothing about a refused call
    # moves the key onto argv, and the error the daemon logs does not carry it.
    monkeypatch.setenv("NMCLI_STUB_FAIL_ON", "edit")

    with pytest.raises(WifiConfigurationError) as excinfo:
        NmcliWifiConfigurator().configure("HomeNet", "hunter2secret")

    assert "hunter2secret" not in str(excinfo.value)
    assert nmcli_stub.argv(), "the real runner never reached the stub"
    for argv in nmcli_stub.argv():
        assert "hunter2secret" not in " ".join(argv)
    assert "802-11-wireless-security.psk" not in nmcli_stub.stored()


def test_configure_rewrites_an_existing_profile_in_place(nmcli_stub):
    nmcli_stub.seed("visio", **{"802-11-wireless.ssid": "HomeNet"})

    NmcliWifiConfigurator().configure("OtherNet", "newsecret99")

    assert [argv[:2] for argv in nmcli_stub.argv()[1:]] == [
        ["connection", "modify"],
        ["connection", "edit"],
        ["-e", "no"],
    ]
    assert nmcli_stub.stored() == {
        "802-11-wireless.ssid": "OtherNet",
        "802-11-wireless-security.key-mgmt": "wpa-psk",
        "802-11-wireless-security.psk": "newsecret99",
        "connection.autoconnect": "yes",
    }


def test_configure_adds_a_system_connection_when_none_exists():
    nmcli = ScriptedNmcli(names=["lo", "Wired connection 1"])

    NmcliWifiConfigurator(nmcli).configure("HomeNet", "hunter2secret")

    assert nmcli.calls[:3] == [
        ["nmcli", "-g", "NAME", "connection", "show"],
        ["nmcli", "connection", "add", "type", "wifi",
         "con-name", "visio", "ifname", "wlan0", "ssid", "HomeNet",
         "wifi-sec.key-mgmt", "wpa-psk", "connection.autoconnect", "yes"],
        ["nmcli", "connection", "edit", "id", "visio"],
    ]  # fmt: skip
    assert nmcli.stdins[2] == (
        "set 802-11-wireless-security.psk\nhunter2secret\nsave\n\nquit\n"
    )


@pytest.mark.parametrize(
    ("ssid", "password"),
    [
        ("Cafe ", "hunter2secret"),
        (" Cafe", "hunter2secret"),
        ("HomeNet", " correct horse"),
        ("HomeNet", "correct horse "),
    ],
)
def test_edge_spaces_reach_networkmanager_byte_for_byte(nmcli_stub, ssid, password):
    # Legal in an SSID and in a WPA passphrase alike; the editor's
    # `set <property> <value>` form would strip them, so only the key goes
    # through the editor and only through its prompted form.
    NmcliWifiConfigurator().configure(ssid, password)

    stored = nmcli_stub.stored()
    assert stored["802-11-wireless.ssid"] == ssid
    assert stored["802-11-wireless-security.psk"] == password
    for argv in nmcli_stub.argv():
        assert password not in argv


def test_a_tab_in_the_ssid_reaches_networkmanager_on_argv(nmcli_stub):
    NmcliWifiConfigurator().configure("Home\tNet", "hunter2secret")

    assert nmcli_stub.stored()["802-11-wireless.ssid"] == "Home\tNet"


def test_a_tab_in_the_password_is_refused_before_calling_nmcli(nmcli_stub):
    # readline in the connection editor would drop it, storing a key the
    # device can never connect with.
    with pytest.raises(ValueError, match="tab.*printable-ASCII"):
        NmcliWifiConfigurator().configure("HomeNet", "hunter2\tsecret")

    assert nmcli_stub.argv() == []


def test_configure_reads_the_key_back_rather_than_trusting_the_exit_status():
    # `nmcli connection edit` prints a refused save on stdout and still exits
    # 0, so the exit status alone would report success over an unchanged
    # profile; `-e no` keeps nmcli from escaping a colon in the key it returns.
    nmcli = ScriptedNmcli(names=["visio"])

    NmcliWifiConfigurator(nmcli).configure("HomeNet", "has:a:colon")

    assert nmcli.calls[-1] == [
        "nmcli", "-e", "no", "--show-secrets", "-g",
        "802-11-wireless-security.psk",
        "connection", "show", "id", "visio",
    ]  # fmt: skip


def test_configure_fails_when_the_editor_did_not_commit_the_key():
    nmcli = ScriptedNmcli(names=["visio"], stored_psk="the-old-key")

    with pytest.raises(WifiConfigurationError) as excinfo:
        NmcliWifiConfigurator(nmcli).configure("HomeNet", "hunter2secret")

    assert "hunter2secret" not in str(excinfo.value)
    assert "the-old-key" not in str(excinfo.value)


def test_configure_modifies_the_existing_connection_on_re_onboarding():
    # Also the migration path: the root-era keyfile NetworkManager already
    # loaded has id "visio", so re-onboarding rewrites it rather than adding a
    # second profile of the same name.
    nmcli = ScriptedNmcli(names=["visio"])

    NmcliWifiConfigurator(nmcli).configure("OtherNet", "newsecret99")

    assert nmcli.calls[1:3] == [
        ["nmcli", "connection", "modify", "visio", "wifi.ssid", "OtherNet",
         "wifi-sec.key-mgmt", "wpa-psk", "connection.autoconnect", "yes"],
        ["nmcli", "connection", "edit", "id", "visio"],
    ]  # fmt: skip
    assert nmcli.stdins[2] == (
        "set 802-11-wireless-security.psk\nnewsecret99\nsave\n\nquit\n"
    )


def test_configure_rejects_invalid_credentials_before_calling_nmcli():
    nmcli = ScriptedNmcli()

    with pytest.raises(ValueError):
        NmcliWifiConfigurator(nmcli).configure("HomeNet", "short")

    assert nmcli.calls == []


def test_a_failed_nmcli_call_never_carries_the_password_in_its_error():
    # CalledProcessError renders the full argv, psk included, and the daemon
    # logs onboarding failures with a traceback into journalctl.
    nmcli = ScriptedNmcli(fail_on="edit")

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
