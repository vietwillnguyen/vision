"""The parts of the real hardware drivers that decide something.

Most of ``drivers.py`` forwards a call to a vendor library and is validated on
the assembled device rather than emulated here. ``PiJuiceBatteryReader`` is an
exception: which of the HAT's four power-input states counts as "externally
powered" is a decision, and getting it wrong either halts a charging pendant or
fails to halt a dying one. Both are worth more than the cost of a fake.
"""

import pytest

from tests.fakes import FakePiJuiceStatus, fake_pijuice
from visio_recorder.battery import BatteryTelemetryReader, read_battery_sample
from visio_recorder.drivers import PiJuiceBatteryReader


def reader(**status) -> PiJuiceBatteryReader:
    return PiJuiceBatteryReader(fake_pijuice(**status))


def test_the_pijuice_reader_satisfies_the_telemetry_protocol():
    # What makes the voltage floor and the charging exemption reachable at
    # all: a source that does not satisfy this is read for percentage alone.
    assert isinstance(reader(), BatteryTelemetryReader)


def test_charge_and_voltage_come_straight_off_the_hat():
    sample = read_battery_sample(reader(charge_pct=42, voltage_mv=3812))

    assert sample.pct == 42
    assert sample.voltage_mv == 3812


def test_a_supply_on_the_main_input_is_external_power():
    assert reader(power_input="PRESENT").is_externally_powered() is True


def test_a_supply_on_the_5v_io_input_is_external_power():
    # The pendant can be charged through either; PiJuice's own system task
    # checks both before deciding a device is on battery.
    assert reader(power_input_5v_io="PRESENT").is_externally_powered() is True


@pytest.mark.parametrize("power_input", ["NOT_PRESENT", "BAD", "WEAK"])
def test_a_marginal_or_absent_supply_is_not_external_power(power_input):
    # BAD and WEAK are exactly the frayed cable or underpowered charger that
    # would otherwise persuade a dying pendant it is safe to keep recording.
    # PiJuice's own pijuice_sys.py treats both as no power for this reason.
    assert reader(power_input=power_input).is_externally_powered() is False


def test_no_battery_fitted_counts_as_external_power():
    # With no cell there is nothing to protect and nothing that can go flat,
    # so halting would only turn a working mains-powered device off.
    assert reader(battery="NOT_PRESENT").is_externally_powered() is True


def test_a_discharging_pendant_reports_no_external_power():
    sample = read_battery_sample(reader(battery="NORMAL", charge_pct=55))

    assert sample.external_power is False
    assert sample.pct == 55


def test_a_charging_pendant_reports_external_power():
    sample = read_battery_sample(
        reader(battery="CHARGING_FROM_IN", power_input="PRESENT", charge_pct=55)
    )

    assert sample.external_power is True


def test_the_reader_reads_the_status_word_once_per_question():
    # Each telemetry method is its own I2C transfer; nothing is cached, so a
    # charger connected between two samples is seen on the second.
    status = FakePiJuiceStatus(power_input="NOT_PRESENT")
    pijuice = fake_pijuice(status=status)
    battery = PiJuiceBatteryReader(pijuice)

    assert battery.is_externally_powered() is False
    status.power_input = "PRESENT"
    assert battery.is_externally_powered() is True
