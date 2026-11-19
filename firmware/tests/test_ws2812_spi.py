"""The SPI WS2812B driver, checked byte-for-byte and against LED timing.

Expected streams are spelled out literally (0xC0 = a WS2812B 0 bit, 0xF0 = a
1 bit, 240 zero bytes = the reset either side) rather than computed with the
encoder under test, and ``ws2812_waveform`` re-derives the colour from the
datasheet timing windows independently of both.
"""

import threading

import pytest

from tests.fakes import SimulatedSpiDevice
from tests.ws2812_waveform import WaveformError, decode_frame, to_pulses
from visio_recorder.drivers import Ws2812LedDriver
from visio_recorder.led import LedPattern, LedState, apply_led_state
from visio_recorder.ws2812_spi import SPI_SPEED_HZ, encode_frame

ZERO = 0xC0
ONE = 0xF0
RESET = bytes(240)


def _channel(value: int) -> bytes:
    return bytes(ONE if value & (0x80 >> i) else ZERO for i in range(8))


def _frame(green: bytes, red: bytes, blue: bytes) -> bytes:
    return RESET + green + red + blue + RESET


ALL_ZERO = bytes([ZERO] * 8)
ALL_ONE = bytes([ONE] * 8)

# Every LED state's exact wire bytes, channel order green, red, blue.
EXPECTED_STATE_FRAMES = {
    LedState.RECORDING: _frame(ALL_ONE, ALL_ZERO, ALL_ZERO),  # green
    LedState.UPLOADING: _frame(ALL_ZERO, ALL_ZERO, ALL_ONE),  # blue
    LedState.LOW_BATTERY: _frame(ALL_ONE, ALL_ONE, ALL_ZERO),  # yellow
    LedState.CRITICAL: _frame(ALL_ZERO, ALL_ONE, ALL_ZERO),  # red
    LedState.HALTED: _frame(ALL_ZERO, ALL_ONE, ALL_ZERO),  # red
}

EXPECTED_STATE_COLOURS = {
    LedState.RECORDING: (0, 255, 0),
    LedState.UPLOADING: (0, 0, 255),
    LedState.LOW_BATTERY: (255, 255, 0),
    LedState.CRITICAL: (255, 0, 0),
    LedState.HALTED: (255, 0, 0),
}

# What the BCM2835 SPI block actually produces for a 6.4 MHz request: the
# kernel rounds the divider up to an even number, and both the 400 MHz
# (cdiv 64) and 250 MHz (cdiv 40) core clocks of a Zero 2 W land on 6.25 MHz.
EFFECTIVE_HZ = 6_250_000


def _driver() -> tuple[Ws2812LedDriver, SimulatedSpiDevice]:
    spi = SimulatedSpiDevice()
    return Ws2812LedDriver(spi=spi), spi


def test_driver_opens_spi0_ce0_in_mode_0_at_6_4_mhz():
    _, spi = _driver()
    assert spi.opened == (0, 0)
    assert spi.mode == 0
    assert spi.max_speed_hz == 6_400_000 == SPI_SPEED_HZ


@pytest.mark.parametrize("state", list(LedState))
def test_each_led_state_writes_its_exact_byte_stream(state):
    driver, spi = _driver()
    apply_led_state(driver, state)
    assert spi.writes == [EXPECTED_STATE_FRAMES[state]]


@pytest.mark.parametrize("state", list(LedState))
@pytest.mark.parametrize("spi_hz", [SPI_SPEED_HZ, EFFECTIVE_HZ])
def test_each_led_state_decodes_to_its_colour_within_datasheet_timing(state, spi_hz):
    driver, spi = _driver()
    apply_led_state(driver, state)
    assert decode_frame(spi.writes[0], spi_hz) == [EXPECTED_STATE_COLOURS[state]]


def test_channels_go_green_red_blue_most_significant_bit_first():
    # Distinct, asymmetric bytes per channel so a swapped channel or a
    # reversed bit order cannot pass.
    driver, spi = _driver()
    driver.set((0x12, 0x34, 0x56), LedPattern.SOLID)
    assert spi.writes == [_frame(_channel(0x34), _channel(0x12), _channel(0x56))]
    assert _channel(0x34) == bytes([0xC0, 0xC0, 0xF0, 0xF0, 0xC0, 0xF0, 0xC0, 0xC0])
    assert decode_frame(spi.writes[0], EFFECTIVE_HZ) == [(0x12, 0x34, 0x56)]


def test_patterns_render_solid_on_a_single_pixel():
    # Unchanged from the PWM driver: pulse/flash animation is not rendered.
    frames = set()
    for pattern in LedPattern:
        driver, spi = _driver()
        driver.set((255, 0, 0), pattern)
        frames.add(spi.writes[0])
    assert frames == {EXPECTED_STATE_FRAMES[LedState.CRITICAL]}


def test_bit_timings_sit_inside_the_datasheet_windows():
    frame = encode_frame([(0, 255, 0)])
    pulses = to_pulses(frame, EFFECTIVE_HZ)
    # 160 ns per SPI bit at 6.25 MHz: a 1 is 4 high + 4 low, a 0 is 2 + 6.
    assert pulses[0].high_ns == pytest.approx(640)
    assert pulses[0].low_ns == pytest.approx(640)
    assert pulses[8].high_ns == pytest.approx(320)
    assert pulses[8].low_ns == pytest.approx(960)


def test_frame_is_framed_by_resets_of_at_least_280_us_at_the_fastest_clock():
    frame = encode_frame([(1, 2, 3)])
    # The kernel only ever rounds the clock down from the request, so 6.4 MHz
    # is the shortest a reset can be: 240 bytes * 8 / 6.4 MHz = 300 us.
    assert frame[:240] == RESET
    assert frame[-240:] == RESET
    assert 240 * 8 / SPI_SPEED_HZ >= 280e-6


def test_frame_is_long_enough_for_spi_bcm2835_to_send_it_by_dma():
    # spi-bcm2835 only uses DMA for transfers of at least 96 bytes
    # (BCM2835_SPI_DMA_MIN_LENGTH); shorter ones go through the PIO/IRQ path,
    # which can leave idle clocks between bytes and stretch T0L past 1000 ns.
    assert len(encode_frame([(0, 0, 0)])) >= 96


def test_frame_fits_in_one_default_spidev_transfer():
    assert len(encode_frame([(255, 255, 255)])) <= SimulatedSpiDevice.BUFSIZ


def test_a_drifted_core_clock_breaks_the_encoding():
    # Why setup must pin the core clock (core_freq_fixed=1): spi-bcm2835
    # caches the core clock at probe, so if the cached 400 MHz drops to
    # 250 MHz under dynamic scaling the real SPI clock falls to 3.9 MHz and
    # a 0 bit's high stretches to 512 ns, outside its 220-380 ns window.
    frame = encode_frame([(0, 255, 0)])
    with pytest.raises(WaveformError):
        decode_frame(frame, 250e6 / 64)


def test_concurrent_set_calls_never_interleave_on_the_wire():
    # The upload worker and the main thread both drive the LED.
    driver, spi = _driver()
    barrier = threading.Barrier(len(LedState))

    def hammer(state: LedState) -> None:
        barrier.wait()
        for _ in range(20):
            apply_led_state(driver, state)

    threads = [threading.Thread(target=hammer, args=(s,)) for s in LedState]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert spi.overlapping_writes == 0
    assert len(spi.writes) == 20 * len(LedState)
    assert set(spi.writes) == set(EXPECTED_STATE_FRAMES.values())


def test_spi_device_must_be_open_before_configuring():
    # Guards the fake's realism: py-spidev raises on an unopened device.
    spi = SimulatedSpiDevice()
    with pytest.raises(OSError):
        spi.max_speed_hz = SPI_SPEED_HZ
