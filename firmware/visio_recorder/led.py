from enum import Enum
from typing import Protocol


class LedState(Enum):
    RECORDING = "recording"
    UPLOADING = "uploading"
    LOW_BATTERY = "low_battery"
    CRITICAL = "critical"
    HALTED = "halted"


class LedPattern(Enum):
    SOLID = "solid"
    PULSING = "pulsing"
    FLASHING = "flashing"


class LedDriver(Protocol):
    def set(self, color: tuple[int, int, int], pattern: LedPattern) -> None: ...


GREEN = (0, 255, 0)
BLUE = (0, 0, 255)
YELLOW = (255, 255, 0)
RED = (255, 0, 0)

_STATE_TO_COLOR_PATTERN: dict[LedState, tuple[tuple[int, int, int], LedPattern]] = {
    LedState.RECORDING: (GREEN, LedPattern.SOLID),
    LedState.UPLOADING: (BLUE, LedPattern.PULSING),
    LedState.LOW_BATTERY: (YELLOW, LedPattern.PULSING),
    LedState.CRITICAL: (RED, LedPattern.FLASHING),
    # Solid, unlike Critical's flash: Critical is "still recording, needs you",
    # Halted is "stopped recording, about to power off" (pendant spec, LED
    # state machine). The SPI driver renders both as solid red until the LED
    # controller animates patterns.
    LedState.HALTED: (RED, LedPattern.SOLID),
}


def apply_led_state(driver: LedDriver, state: LedState) -> None:
    color, pattern = _STATE_TO_COLOR_PATTERN[state]
    driver.set(color, pattern)
