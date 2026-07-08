from visio_recorder.battery import BatteryReader, read_battery_status


class FakeBatteryReader(BatteryReader):
    def __init__(self, pct: int) -> None:
        self._pct = pct

    def get_charge_pct(self) -> int:
        return self._pct


def test_battery_above_thresholds_is_normal():
    status = read_battery_status(FakeBatteryReader(85))
    assert status.pct == 85
    assert status.should_halt is False
    assert status.is_low is False


def test_battery_below_warn_threshold_is_low():
    status = read_battery_status(FakeBatteryReader(15))
    assert status.is_low is True
    assert status.should_halt is False


def test_battery_below_halt_threshold_should_halt():
    status = read_battery_status(FakeBatteryReader(5))
    assert status.should_halt is True
