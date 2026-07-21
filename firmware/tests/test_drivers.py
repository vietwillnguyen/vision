"""Unit tests for the one class in drivers.py with no hardware dependency.

Every other class here (PiJuiceBatteryReader, Ws2812LedDriver,
GpioZeroFlagButton) wraps a real device library behind a deferred import
and is deliberately not unit tested - see drivers.py's module docstring.
UnmeteredPowerReader is different: it touches no hardware at all, so it
gets the same test treatment as any other pure BatteryReader (compare
tests/fakes.py's FakeBatteryReader).
"""

from visio_recorder.drivers import UnmeteredPowerReader


def test_unmetered_power_reader_always_reports_full_charge():
    reader = UnmeteredPowerReader()

    assert reader.get_charge_pct() == 100
