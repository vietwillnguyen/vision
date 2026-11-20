import logging

from tests.fakes import FakeCommandRunner, FakePiJuicePower, RaisingCommandRunner
from visio_recorder.power import (
    PIJUICE_POWER_OFF_DELAY_SEC,
    POWEROFF_COMMAND,
    PiJuicePowerController,
    SystemdPowerController,
)

# --- no battery HAT ----------------------------------------------------------


def test_the_systemd_controller_runs_systemctl_poweroff():
    runner = FakeCommandRunner()

    SystemdPowerController(runner).power_off(wake_on_charge_pct=25)

    assert runner.calls == [list(POWEROFF_COMMAND)]


def test_a_refused_poweroff_is_logged_rather_than_raised(caplog):
    # A polkit refusal is silent in production unless it is logged loudly: by
    # this point recording has stopped and the queue has drained, so there is
    # nothing left for an exception to protect.
    with caplog.at_level(logging.ERROR):
        SystemdPowerController(RaisingCommandRunner()).power_off(wake_on_charge_pct=25)

    assert "org.freedesktop.login1.power-off" in caplog.text


# --- with a PiJuice ----------------------------------------------------------


def test_the_pijuice_controller_arms_the_wake_before_cutting_power():
    power = FakePiJuicePower()
    runner = FakeCommandRunner()

    PiJuicePowerController(power, runner).power_off(wake_on_charge_pct=25)

    assert power.calls == [
        ("SetWakeUpOnCharge", 25),
        ("SetSystemPowerSwitch", 0),
        ("SetPowerOff", PIJUICE_POWER_OFF_DELAY_SEC),
    ]


def test_the_os_halt_runs_after_every_pijuice_call():
    # The halt is what protects the filesystem, and every step after it is
    # unreachable, so it has to be last.
    power = FakePiJuicePower()
    runner = FakeCommandRunner()

    PiJuicePowerController(power, runner).power_off(wake_on_charge_pct=25)

    assert runner.calls == [list(POWEROFF_COMMAND)]
    assert len(power.calls) == 3


def test_a_pijuice_call_that_raises_still_leaves_the_device_powered_off():
    # Losing the automatic wake costs a manual power cycle; skipping the
    # power-off costs the SD card.
    power = FakePiJuicePower(raise_on={"SetWakeUpOnCharge"})
    runner = FakeCommandRunner()

    PiJuicePowerController(power, runner).power_off(wake_on_charge_pct=25)

    assert runner.calls == [list(POWEROFF_COMMAND)]
    assert ("SetPowerOff", PIJUICE_POWER_OFF_DELAY_SEC) in power.calls


def test_a_pijuice_call_that_reports_an_error_is_logged_and_does_not_stop_it(caplog):
    # The vendor library returns an error dict rather than raising, so a
    # failure is invisible unless the return value is inspected.
    power = FakePiJuicePower(error_on={"SetPowerOff": "COMMUNICATION_ERROR"})
    runner = FakeCommandRunner()

    with caplog.at_level(logging.ERROR):
        PiJuicePowerController(power, runner).power_off(wake_on_charge_pct=25)

    assert "COMMUNICATION_ERROR" in caplog.text
    assert runner.calls == [list(POWEROFF_COMMAND)]
