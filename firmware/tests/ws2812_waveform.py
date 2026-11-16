"""An independent WS2812B receiver model for checking SPI frames.

Written from the WS2812B datasheet rather than from ``ws2812_spi``, so the
tests that use it check the encoder against the LED's timing contract instead
of against itself. It turns the MOSI bit stream into the waveform the LED
sees at a given SPI clock, splits it into high/low pulse pairs, and accepts a
pulse pair only when both halves land inside the datasheet window for a 0 bit
or a 1 bit. Anything else raises ``WaveformError``.

Datasheet timing (Worldsemi WS2812B, every value +/-150 ns): T0H 400 ns, T0L
850 ns, T1H 800 ns, T1L 450 ns. Reset/latch is a low period of at least
280 us (WS2812B-V5 and later; the original part latched after 50 us).
"""

from dataclasses import dataclass

TOLERANCE_NS = 150.0
T0H_NS = 400.0
T0L_NS = 850.0
T1H_NS = 800.0
T1L_NS = 450.0
RESET_NS = 280_000.0


class WaveformError(ValueError):
    pass


@dataclass(frozen=True)
class Pulse:
    high_ns: float
    low_ns: float


def _within(value: float, nominal: float) -> bool:
    return nominal - TOLERANCE_NS <= value <= nominal + TOLERANCE_NS


def to_pulses(frame: bytes, spi_hz: float) -> list[Pulse]:
    """MOSI bytes (MSB first, SPI mode 0) -> high/low pulse pairs."""
    bit_ns = 1e9 / spi_hz
    bits = [(byte >> (7 - i)) & 1 for byte in frame for i in range(8)]
    pulses: list[Pulse] = []
    i = 0
    leading_low = 0
    while i < len(bits) and bits[i] == 0:
        leading_low += 1
        i += 1
    if leading_low * bit_ns < RESET_NS:
        raise WaveformError(
            f"frame must open with a >= {RESET_NS / 1000:.0f} us reset, "
            f"got {leading_low * bit_ns / 1000:.1f} us"
        )
    while i < len(bits):
        high = 0
        while i < len(bits) and bits[i] == 1:
            high += 1
            i += 1
        low = 0
        while i < len(bits) and bits[i] == 0:
            low += 1
            i += 1
        pulses.append(Pulse(high * bit_ns, low * bit_ns))
    return pulses


def decode_frame(frame: bytes, spi_hz: float) -> list[tuple[int, int, int]]:
    """Decode a frame into the (r, g, b) colours a WS2812B chain would latch.

    The last pulse's low period must be a reset (the latch); every other
    pulse must be a valid 0 or 1 bit. The LED receives green, red, blue,
    most significant bit first.
    """
    pulses = to_pulses(frame, spi_hz)
    if not pulses:
        raise WaveformError("frame carries no data")
    last = pulses[-1]
    if last.low_ns < RESET_NS:
        raise WaveformError(
            f"frame must close with a >= {RESET_NS / 1000:.0f} us reset, "
            f"got {last.low_ns / 1000:.1f} us"
        )
    bits: list[int] = []
    for index, pulse in enumerate(pulses):
        is_last = index == len(pulses) - 1
        low_ok_0 = is_last or _within(pulse.low_ns, T0L_NS)
        low_ok_1 = is_last or _within(pulse.low_ns, T1L_NS)
        if _within(pulse.high_ns, T0H_NS) and low_ok_0:
            bits.append(0)
        elif _within(pulse.high_ns, T1H_NS) and low_ok_1:
            bits.append(1)
        else:
            raise WaveformError(
                f"bit {index}: high {pulse.high_ns:.0f} ns / low "
                f"{pulse.low_ns:.0f} ns is neither a WS2812B 0 nor a 1"
            )
    if len(bits) % 24:
        raise WaveformError(f"{len(bits)} bits is not a whole number of pixels")
    colours: list[tuple[int, int, int]] = []
    for start in range(0, len(bits), 24):
        channel = [
            int("".join(str(b) for b in bits[start + k : start + k + 8]), 2)
            for k in (0, 8, 16)
        ]
        green, red, blue = channel
        colours.append((red, green, blue))
    return colours
