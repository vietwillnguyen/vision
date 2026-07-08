from visio_recorder.led import LedDriver, LedPattern, LedState, apply_led_state


class FakeLedDriver(LedDriver):
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[int, int, int], LedPattern]] = []

    def set(self, color: tuple[int, int, int], pattern: LedPattern) -> None:
        self.calls.append((color, pattern))


def test_recording_state_is_solid_green():
    driver = FakeLedDriver()
    apply_led_state(driver, LedState.RECORDING)
    assert driver.calls == [((0, 255, 0), LedPattern.SOLID)]


def test_uploading_state_is_pulsing_blue():
    driver = FakeLedDriver()
    apply_led_state(driver, LedState.UPLOADING)
    assert driver.calls == [((0, 0, 255), LedPattern.PULSING)]


def test_low_battery_state_is_pulsing_yellow():
    driver = FakeLedDriver()
    apply_led_state(driver, LedState.LOW_BATTERY)
    assert driver.calls == [((255, 255, 0), LedPattern.PULSING)]


def test_critical_state_is_flashing_red():
    driver = FakeLedDriver()
    apply_led_state(driver, LedState.CRITICAL)
    assert driver.calls == [((255, 0, 0), LedPattern.FLASHING)]
