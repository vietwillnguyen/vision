"""WS2812B frames for the SPI0 MOSI line (GPIO 10).

A WS2812B reads one data bit per high/low pulse pair and tells a 0 from a 1
by how long the line stays high. Worldsemi's datasheet timings, each +/-150 ns:

    bit 0: high 400 ns, low 850 ns
    bit 1: high 800 ns, low 450 ns
    latch: low for >= 280 us (WS2812B-V5; the original part needed 50 us)

SPI can draw that waveform because MOSI is just a bit stream at a fixed clock.
At 6.4 MHz one SPI bit lasts 156.25 ns, so one SPI byte lasts exactly one
1.25 us WS2812B bit period, and each WS2812B bit becomes one byte:

    bit 0 -> 0b1110_0000 (0xE0): 3 high + 5 low
    bit 1 -> 0b1111_1000 (0xF8): 5 high + 3 low

The BCM2835 SPI block clocks at ``core_clock / CDIV`` with an even CDIV, and
the kernel rounds CDIV *up*, so the real clock is never faster than asked.
On a Zero 2 W both core-clock settings land on the same rate: 400 MHz / 64 and
250 MHz / 40 are both 6.25 MHz, 160 ns per SPI bit:

    clock      T0H     T0L     T1H     T1L     window (datasheet)
    6.40 MHz   469 ns  781 ns  781 ns  469 ns  T0H 250-550, T0L 700-1000,
    6.25 MHz   480 ns  800 ns  800 ns  480 ns  T1H 650-950, T1L 300-600

Every value sits at least 70 ns inside its window at either clock. Two more
properties of the byte-per-bit layout matter on real hardware: each byte ends
low, so an inter-byte gap only stretches a low phase (which the part
tolerates) and can never stretch a high one into a different bit; and the
frame is plain bytes, so no word-size or bit-order configuration is involved.

The kernel driver (``spi-bcm2835``) caches the core clock at probe time, so
the device must pin the core clock (``core_freq_fixed=1`` in config.txt).
Otherwise firmware frequency scaling between 250 and 400 MHz moves the real
SPI clock away from the computed one by up to 1.6x, which pushes T0H out of
its window.

The frame is ``RESET_BYTES`` of zeros, the pixel bytes (green, red, blue, most
significant bit first, as the LED expects), then the same reset again. The
leading reset guarantees a clean latch boundary whatever MOSI did between
transfers; the trailing one latches the colour. 240 bytes is 300 us at
6.4 MHz, and only longer at any slower real clock. A single pixel is
240 + 24 + 240 = 504 bytes, well inside spidev's default 4096-byte transfer.
"""

import threading
from collections.abc import Sequence
from typing import Protocol

from visio_recorder.led import LedPattern

SPI_SPEED_HZ = 6_400_000
SPI_MODE = 0
ZERO_BIT = 0b1110_0000
ONE_BIT = 0b1111_1000
RESET_US = 300
RESET_BYTES = RESET_US * SPI_SPEED_HZ // 8 // 1_000_000  # 240


class SpiDevice(Protocol):
    """The subset of py-spidev's ``spidev.SpiDev`` the LED driver uses."""

    mode: int
    max_speed_hz: int

    def open(self, bus: int, device: int) -> None: ...

    def writebytes2(self, values: bytes) -> None: ...


def _encode_byte(value: int) -> bytes:
    return bytes(ONE_BIT if value & (0x80 >> i) else ZERO_BIT for i in range(8))


def encode_frame(colors: Sequence[tuple[int, int, int]]) -> bytes:
    """Encode (r, g, b) pixels into one complete SPI transfer."""
    data = b"".join(
        _encode_byte(g) + _encode_byte(r) + _encode_byte(b) for r, g, b in colors
    )
    reset = bytes(RESET_BYTES)
    return reset + data + reset


class SpiWs2812Led:
    """One WS2812B whose data line is SPI MOSI.

    Implements the ``LedDriver`` protocol. The upload worker and the main
    thread both set the LED, so a lock keeps two frames from interleaving on
    the wire.
    """

    def __init__(self, spi: SpiDevice, bus: int = 0, device: int = 0) -> None:
        spi.open(bus, device)
        spi.mode = SPI_MODE
        spi.max_speed_hz = SPI_SPEED_HZ
        self._spi = spi
        self._lock = threading.Lock()

    def set(self, color: tuple[int, int, int], pattern: LedPattern) -> None:
        # Single pixel, no animation thread: PULSING and FLASHING render as
        # solid color, exactly as the previous PWM driver did.
        frame = encode_frame([color])
        with self._lock:
            self._spi.writebytes2(frame)
