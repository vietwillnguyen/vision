from visio_recorder.daemon import run_startup_sequence
from visio_recorder.led import LedDriver, LedPattern


class FakeBatteryReader:
    def __init__(self, pct: int) -> None:
        self._pct = pct

    def get_charge_pct(self) -> int:
        return self._pct


class FakeLedDriver(LedDriver):
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[int, int, int], LedPattern]] = []

    def set(self, color, pattern):
        self.calls.append((color, pattern))


def test_healthy_battery_proceeds_and_shows_recording_led():
    led = FakeLedDriver()
    result = run_startup_sequence(FakeBatteryReader(85), led)

    assert result.proceed is True
    assert result.battery_pct == 85
    assert led.calls == [((0, 255, 0), LedPattern.SOLID)]


def test_critical_battery_halts_and_shows_critical_led():
    led = FakeLedDriver()
    result = run_startup_sequence(FakeBatteryReader(5), led)

    assert result.proceed is False
    assert result.battery_pct == 5
    assert led.calls == [((255, 0, 0), LedPattern.FLASHING)]
