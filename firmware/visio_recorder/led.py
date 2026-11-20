import queue
import threading
import time
from collections.abc import Callable
from enum import Enum
from typing import Protocol


class LedState(Enum):
    RECORDING = "recording"
    UPLOADING = "uploading"
    LOW_BATTERY = "low_battery"
    CRITICAL = "critical"
    HALTED = "halted"
    # Momentary, not a condition: see LedController.ack_flag.
    FLAG_ACK = "flag_ack"


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
MAGENTA = (255, 0, 255)

# How long a flag acknowledgement holds the pixel. Long enough to register
# through a pendant housing, short enough that it cannot be mistaken for a
# state; accepted presses are at least 2 s apart, so blinks never stack.
FLAG_ACK_BLINK_SEC = 0.15

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
    LedState.FLAG_ACK: (MAGENTA, LedPattern.SOLID),
}


def apply_led_state(driver: LedDriver, state: LedState) -> None:
    color, pattern = _STATE_TO_COLOR_PATTERN[state]
    driver.set(color, pattern)


class LedController:
    """The single owner of the status LED, and the only thing that blinks it.

    Implements ``LedDriver`` by writing through to the real driver, so every
    caller that drives the LED from a condition - the upload worker, the
    startup sequence, the battery halt - keeps calling ``apply_led_state``
    unchanged. What it adds is ``ack_flag``.

    A flag acknowledgement is not a condition and has no rung in
    ``next_led_state``'s precedence: it is a momentary overlay that shows
    magenta and then puts back whatever state the conditions last called for.
    It has to be rendered by something that is neither the caller nor a
    condition owner, because the press arrives on gpiozero's callback thread,
    which must not sleep (a later press would be delayed) and must not touch
    the driver (that would break the single-owner invariant the upload
    worker's ``UploadHealth`` relies on). So the blink runs here, on this
    object's own thread, and ``ack_flag`` only queues it.

    The wait is deliberately held *outside* the lock. A real state change
    landing mid-blink therefore reaches the driver at once and wins: the
    restore is skipped, because the state it would write is stale and would
    paint over, say, a Critical red that was latched while the blink was up.
    Writes themselves are serialised by the lock, so two threads never
    interleave frames on the wire.
    """

    def __init__(
        self,
        driver: LedDriver,
        *,
        blink_sec: float = FLAG_ACK_BLINK_SEC,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._driver = driver
        self._blink_sec = blink_sec
        self._sleep = sleep
        self._lock = threading.Lock()
        self._state: tuple[tuple[int, int, int], LedPattern] | None = None
        # Bumped by every condition write, so a blink can tell whether the
        # state it started from is still the one on the wire.
        self._generation = 0
        self._acks: queue.Queue[bool | None] = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, name="led-controller", daemon=True
        )
        self._thread.start()

    def set(self, color: tuple[int, int, int], pattern: LedPattern) -> None:
        with self._lock:
            self._state = (color, pattern)
            self._generation += 1
            self._driver.set(color, pattern)

    def ack_flag(self) -> None:
        """Ask for an acknowledgement blink; returns without rendering it."""
        self._acks.put(True)

    def close(self) -> None:
        """Finish any blink in flight, then stop rendering them."""
        if not self._thread.is_alive():
            return
        self._acks.put(None)
        self._thread.join()

    def _run(self) -> None:
        while True:
            if self._acks.get() is None:
                return
            self._blink()

    def _blink(self) -> None:
        with self._lock:
            if self._state is None:
                # Nothing has set a state yet, so there is nothing to return
                # to and magenta would simply stay on.
                return
            if self._state == _STATE_TO_COLOR_PATTERN[LedState.HALTED]:
                # Halted is terminal and must be the last thing the wearer
                # sees before the pendant powers off; the acknowledgement is
                # an overlay on Recording, and nothing is recording now.
                return
            generation = self._generation
            color, pattern = _STATE_TO_COLOR_PATTERN[LedState.FLAG_ACK]
            self._driver.set(color, pattern)
        self._sleep(self._blink_sec)
        with self._lock:
            if self._generation != generation or self._state is None:
                return
            self._driver.set(*self._state)
