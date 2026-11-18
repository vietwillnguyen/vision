"""Real hardware drivers for visio-recorder.

These wrap third-party device libraries (``pijuice``, ``spidev``,
``gpiozero``) that are
only installed on the Raspberry Pi target via apt/system packages, not
declared in this project's pyproject.toml. Each import is deferred into
``__init__`` so this module stays importable on machines without the
hardware present (including CI). The button driver is deliberately not unit
tested: that is the real-driver convention used throughout ``daemon.py`` -
behavior is validated on the assembled device in Epic 5, not emulated here.

Two drivers are exceptions, for the same reason in both cases: they do real
work of their own rather than just forwarding a call, so that work is worth
pinning. ``Ws2812LedDriver`` accepts an injected SPI device and the tests
assert the exact frames it sends (see ``ws2812_spi``).
``PiJuiceBatteryReader`` accepts an injected PiJuice and the tests pin how it
reads the status word - which of the HAT's four power-input states count as
externally powered is a decision, not a forward, and getting it wrong would
either halt a charging pendant or fail to halt a dying one.
"""

import logging
from collections.abc import Callable
from typing import Any

from visio_recorder.ws2812_spi import SpiDevice, SpiWs2812Led

_logger = logging.getLogger(__name__)


def open_pijuice() -> Any | None:
    """The PiJuice HAT on I2C bus 1, or None when there is none to open.

    One object for the whole daemon: the battery reader and the power
    controller both need it, and opening the bus twice would be two I2C
    handles to the same device. Returns None rather than raising when the
    library or the bus is absent, so a pendant running from a USB power bank
    still starts - the battery source then reports a fixed percentage and the
    power controller falls back to ``systemctl poweroff``.
    """
    try:
        from pijuice import PiJuice

        return PiJuice(1, 0x14)
    except Exception:
        _logger.warning(
            "no PiJuice on I2C bus 1; battery telemetry and the HAT's own "
            "power-off and charge wake are unavailable on this device",
            exc_info=True,
        )
        return None


# The PiJuice reports its power inputs as one of four states. Only PRESENT is
# a supply the pendant can run on: BAD and WEAK are exactly the marginal cable
# or charger that would otherwise persuade a dying pendant it is safe, and
# PiJuice's own pijuice_sys.py treats both as no power for the same reason.
_EXTERNAL_POWER_PRESENT = "PRESENT"
_BATTERY_ABSENT = "NOT_PRESENT"


class PiJuiceBatteryReader:
    """Charge, terminal voltage and external-power state from a PiJuice HAT.

    Implements ``BatteryTelemetryReader``: voltage is what catches a cell
    whose gauge still reads half full under camera and WiFi load, and the
    external-power state is what stops a pendant on a charger shutting itself
    down.
    """

    def __init__(self, pijuice: Any | None = None) -> None:
        pijuice = open_pijuice() if pijuice is None else pijuice
        if pijuice is None:
            raise RuntimeError(
                "no PiJuice on I2C bus 1; a pendant without one needs a "
                "battery source that does not read the HAT"
            )
        self._pijuice: Any = pijuice

    def get_charge_pct(self) -> int:
        return self._pijuice.status.GetChargeLevel()["data"]

    def get_battery_voltage_mv(self) -> int:
        return self._pijuice.status.GetBatteryVoltage()["data"]

    def is_externally_powered(self) -> bool:
        """True when the pendant is running on something other than the cell.

        A battery reading NOT_PRESENT counts too: with no cell fitted there is
        nothing to protect and nothing that can go flat, so halting would only
        turn a working mains-powered device off.
        """
        status = self._pijuice.status.GetStatus()["data"]
        if status.get("battery") == _BATTERY_ABSENT:
            return True
        return _EXTERNAL_POWER_PRESENT in (
            status.get("powerInput"),
            status.get("powerInput5vIo"),
        )


class Ws2812LedDriver(SpiWs2812Led):
    """Drives the single WS2812B status LED from SPI0 MOSI (GPIO 10).

    Uses ``/dev/spidev0.0`` through py-spidev (Raspberry Pi OS package
    ``python3-spidev``), which needs ``dtparam=spi=on`` and membership of the
    ``spi`` group - no root and no ``/dev/mem``, unlike the PWM/DMA driver
    it replaces, and GPIO 18 stays free for I2S audio. Encoding and timing
    are documented in ``ws2812_spi``.
    """

    def __init__(self, spi: SpiDevice | None = None) -> None:
        if spi is None:
            import spidev

            spi = spidev.SpiDev()
        super().__init__(spi, bus=0, device=0)


class GpioZeroFlagButton:
    """Tactile flag button between GPIO 17 and ground, internal pull-up.

    Pin and pull-up per the hardware plan's wiring step. gpiozero's
    ``bounce_time`` absorbs contact bounce; the press handler's cooldown owns
    deliberate repeated-press semantics.
    """

    def __init__(self, pin: int = 17) -> None:
        from gpiozero import Button

        self._button = Button(pin, pull_up=True, bounce_time=0.05)

    def on_press(self, callback: Callable[[], None]) -> None:
        self._button.when_pressed = callback
