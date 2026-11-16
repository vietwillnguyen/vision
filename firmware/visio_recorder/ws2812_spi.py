"""WS2812B frames for the SPI0 MOSI line (GPIO 10).

A WS2812B reads one data bit per high/low pulse pair and tells a 0 from a 1
by how long the line stays high. The Worldsemi WS2812B-V5/W datasheet (the
revision sold today) gives these windows:

    bit 0: high 220-380 ns, low 580-1000 ns
    bit 1: high 580-1000 ns, low 580-1000 ns
    latch: low for > 280 us

SPI can draw that waveform because MOSI is just a bit stream at a fixed clock.
At 6.4 MHz one SPI bit lasts 156.25 ns, so one SPI byte lasts exactly one
1.25 us WS2812B bit period, and each WS2812B bit becomes one byte:

    bit 0 -> 0b1100_0000 (0xC0): 2 high + 6 low
    bit 1 -> 0b1111_0000 (0xF0): 4 high + 4 low

The BCM2835 SPI block clocks at ``core_clock / CDIV`` with an even CDIV, and
the kernel rounds CDIV *up*, so the real clock is never faster than asked.
On a Zero 2 W both core-clock settings land on the same rate: 400 MHz / 64 and
250 MHz / 40 are both 6.25 MHz, 160 ns per SPI bit:

    clock      T0H     T0L     T1H     T1L
    6.40 MHz   312 ns  938 ns  625 ns  625 ns
    6.25 MHz   320 ns  960 ns  640 ns  640 ns

Every value sits at least 40 ns inside its V5 window at either clock, and
also inside the SK6812 windows (T0H 300, T1H 600, T0L 900, T1L 600, each
+/-150 ns). Legacy pre-V5 WS2812B parts (T0H 400, T1H 800, T0L 850, T1L 450,
each +/-150 ns) see T1H 10 ns short and T1L 40 ns long at 6.25 MHz; no
byte-aligned encoding at any even BCM2835 divider fits both the legacy and
the V5 windows (their T1L windows only overlap at 580-600 ns), so this
targets V5 and leaves a legacy part as a bench check.

Two more properties of the byte-per-bit layout matter on real hardware: each
byte ends low, so an inter-byte gap only stretches a low phase (which the
part tolerates) and can never stretch a high one into a different bit; and
the frame is plain bytes, so no word-size or bit-order configuration is
involved.

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
ZERO_BIT = 0b1100_0000
ONE_BIT = 0b1111_0000
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
