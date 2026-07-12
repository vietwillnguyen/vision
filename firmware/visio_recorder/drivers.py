"""Real hardware drivers for visio-recorder.

These wrap third-party device libraries (``pijuice``, ``rpi_ws281x``) that are
only installed on the Raspberry Pi target via apt/system packages, not
declared in this project's pyproject.toml. Each import is deferred into
``__init__`` so this module stays importable on machines without the
hardware present (including CI). Deliberately not unit tested: this is the
real-driver convention used throughout ``daemon.py`` - behavior is validated
on the assembled device in Epic 5, not emulated here.
"""

from visio_recorder.led import LedPattern


class PiJuiceBatteryReader:
    """Reads battery charge percentage from a PiJuice HAT over I2C."""

    def __init__(self) -> None:
        from pijuice import PiJuice

        self._pijuice = PiJuice(1, 0x14)

    def get_charge_pct(self) -> int:
        return self._pijuice.status.GetChargeLevel()["data"]


class Ws2812LedDriver:
    """Drives a single WS2812B status LED on GPIO pin 18."""

    def __init__(self) -> None:
        from rpi_ws281x import Color, PixelStrip

        self._color = Color
        self._strip = PixelStrip(1, 18)
        self._strip.begin()

    def set(self, color: tuple[int, int, int], pattern: LedPattern) -> None:
        # Single-pixel strip: PULSING and FLASHING render as solid color here.
        # Real pulse/flash animation timing is validated on-device in Epic 5.
        r, g, b = color
        self._strip.setPixelColor(0, self._color(r, g, b))
        self._strip.show()
