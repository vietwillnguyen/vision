"""Real hardware drivers for visio-recorder.

These wrap third-party device libraries (``pijuice``, ``spidev``,
``gpiozero``) that are
only installed on the Raspberry Pi target via apt/system packages, not
declared in this project's pyproject.toml. Each import is deferred into
``__init__`` so this module stays importable on machines without the
hardware present (including CI). The battery and button drivers are
deliberately not unit tested: that is the real-driver convention used
throughout ``daemon.py`` - behavior is validated on the assembled device in
Epic 5, not emulated here. The LED is the exception: its wire format is
pure bytes, so ``Ws2812LedDriver`` accepts an injected SPI device and the
tests assert the exact frames it sends (see ``ws2812_spi``).
"""

from collections.abc import Callable

from visio_recorder.ws2812_spi import SpiDevice, SpiWs2812Led


class PiJuiceBatteryReader:
    """Reads battery charge percentage from a PiJuice HAT over I2C."""

    def __init__(self) -> None:
        from pijuice import PiJuice

        self._pijuice = PiJuice(1, 0x14)

    def get_charge_pct(self) -> int:
        return self._pijuice.status.GetChargeLevel()["data"]


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


class UnmeteredPowerReader:
    """Battery source for VISIO_BATTERY_SOURCE=none: no battery HAT present.

    Always reports full charge. battery.py's read_battery_status is purely
    numeric (should_halt = pct < 10, is_low = pct < 20), so any fallback
    value has to clear both thresholds or it would falsely halt the device,
    or show a low-battery LED, while running from a mains-equivalent USB
    power bank. There is no meaningful percentage to report and "unknown"
    isn't a valid int for BatteryReader.get_charge_pct(), so 100 - not a
    sentinel - is the only value that's actually correct here.
    """

    def get_charge_pct(self) -> int:
        return 100
