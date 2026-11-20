import threading

from tests.fakes import FakeLedDriver
from visio_recorder.led import (
    FLAG_ACK_BLINK_SEC,
    LedController,
    LedPattern,
    LedState,
    apply_led_state,
)

GREEN = ((0, 255, 0), LedPattern.SOLID)
BLUE = ((0, 0, 255), LedPattern.PULSING)
RED = ((255, 0, 0), LedPattern.FLASHING)
MAGENTA = ((255, 0, 255), LedPattern.SOLID)


def test_recording_state_is_solid_green():
    driver = FakeLedDriver()
    apply_led_state(driver, LedState.RECORDING)
    assert driver.calls == [GREEN]


def test_uploading_state_is_pulsing_blue():
    driver = FakeLedDriver()
    apply_led_state(driver, LedState.UPLOADING)
    assert driver.calls == [BLUE]


def test_low_battery_state_is_pulsing_yellow():
    driver = FakeLedDriver()
    apply_led_state(driver, LedState.LOW_BATTERY)
    assert driver.calls == [((255, 255, 0), LedPattern.PULSING)]


def test_critical_state_is_flashing_red():
    driver = FakeLedDriver()
    apply_led_state(driver, LedState.CRITICAL)
    assert driver.calls == [RED]


def test_halted_state_is_solid_red():
    driver = FakeLedDriver()
    apply_led_state(driver, LedState.HALTED)
    assert driver.calls == [((255, 0, 0), LedPattern.SOLID)]


def test_flag_ack_is_solid_magenta():
    # Magenta is the last unused primary pair and the complement of the green
    # the wearer sees most of the time, so the blink reads against its usual
    # background and stays clear of the white and dim white the stop/start
    # states take.
    driver = FakeLedDriver()
    apply_led_state(driver, LedState.FLAG_ACK)
    assert driver.calls == [MAGENTA]


class GatedSleep:
    """Holds the blink open until the test releases it; records the delays."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.entered.set()
        assert self.release.wait(timeout=5), "the blink was never released"


def test_controller_writes_condition_states_straight_through():
    driver = FakeLedDriver()
    controller = LedController(driver)
    try:
        apply_led_state(controller, LedState.RECORDING)
        apply_led_state(controller, LedState.UPLOADING)
    finally:
        controller.close()
    assert driver.calls == [GREEN, BLUE]


def test_ack_blinks_magenta_and_then_restores_the_state():
    driver = FakeLedDriver()
    sleep = GatedSleep()
    controller = LedController(driver, sleep=sleep)
    apply_led_state(controller, LedState.RECORDING)

    controller.ack_flag()

    assert sleep.entered.wait(timeout=5), "the blink never started"
    assert driver.calls == [GREEN, MAGENTA]
    sleep.release.set()
    controller.close()
    assert driver.calls == [GREEN, MAGENTA, GREEN]
    assert sleep.delays == [FLAG_ACK_BLINK_SEC]


def test_ack_returns_before_the_blink_is_over():
    # The press runs on gpiozero's callback thread, which may neither sleep
    # nor touch the driver, so the blink has to belong to the controller.
    driver = FakeLedDriver()
    sleep = GatedSleep()
    controller = LedController(driver, sleep=sleep)
    apply_led_state(controller, LedState.RECORDING)

    controller.ack_flag()  # returns while the blink is still held open

    assert sleep.entered.wait(timeout=5)
    assert not sleep.release.is_set()
    sleep.release.set()
    controller.close()


def test_ack_restores_a_latched_critical_red():
    # The blink is deliberately applied over Critical - a long outage is
    # exactly when a press most needs acknowledging - so the latch has to come
    # back on its own when the blink ends.
    driver = FakeLedDriver()
    sleep = GatedSleep()
    controller = LedController(driver, sleep=sleep)
    apply_led_state(controller, LedState.CRITICAL)

    controller.ack_flag()

    assert sleep.entered.wait(timeout=5)
    sleep.release.set()
    controller.close()
    assert driver.calls == [RED, MAGENTA, RED]


def test_a_state_change_during_a_blink_wins_and_is_not_overwritten():
    driver = FakeLedDriver()
    sleep = GatedSleep()
    controller = LedController(driver, sleep=sleep)
    apply_led_state(controller, LedState.RECORDING)
    controller.ack_flag()
    assert sleep.entered.wait(timeout=5)

    apply_led_state(controller, LedState.CRITICAL)
    sleep.release.set()
    controller.close()

    # No restore after the red: the blink lost the race, and re-applying the
    # green it started from would paint over a latched Critical.
    assert driver.calls == [GREEN, MAGENTA, RED]


def test_ack_before_any_state_has_been_set_writes_nothing():
    # There would be nothing to return to, so magenta would simply stay on.
    driver = FakeLedDriver()
    controller = LedController(driver, sleep=lambda _: None)

    controller.ack_flag()
    controller.close()

    assert driver.calls == []


def test_ack_after_close_is_dropped():
    driver = FakeLedDriver()
    controller = LedController(driver, sleep=lambda _: None)
    apply_led_state(controller, LedState.CRITICAL)
    controller.close()

    controller.ack_flag()

    assert driver.calls == [RED]


def test_close_is_idempotent():
    controller = LedController(FakeLedDriver(), sleep=lambda _: None)
    controller.close()
    controller.close()


def test_ack_while_halted_writes_nothing():
    # Halted is terminal: the pendant is about to power off and nothing is
    # recording, so there is nothing for a flag to acknowledge, and the red
    # must stay the last frame on the wire.
    driver = FakeLedDriver()
    controller = LedController(driver, sleep=lambda _: None)
    apply_led_state(controller, LedState.HALTED)

    controller.ack_flag()
    controller.close()

    assert driver.calls == [((255, 0, 0), LedPattern.SOLID)]
