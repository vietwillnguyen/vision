"""Deciding what a battery reading means, without ever acting on one sample.

A fuel gauge reports state of charge, not energy, and the PiJuice's is
resistance-compensated against a battery profile rather than measured. Under
the pendant's own load - a 4 Mbps hardware H.264 encode plus a WiFi upload
burst, the two heaviest consumers on the board - the reported percentage dips
and recovers, and the cell's terminal voltage sags and recovers with it.
Reading the gauge once and halting on it therefore shuts a healthy pendant
down mid-morning as readily as it catches a real one at dusk.

So every threshold here is reached through a debounce, and the whole decision
lives in one pure object: ``BatteryMonitor`` reads no hardware and keeps no
clock, which makes a noisy, sagging, charging or recovering trace an ordinary
test input rather than something that needs a device to reproduce.

See the pendant spec's "Battery and Power Management" section for the design
this implements.
"""

import logging
from collections import deque
from dataclasses import dataclass, replace
from enum import Enum
from threading import Lock
from typing import Protocol, runtime_checkable

# Below this, the low-battery latch sets; at or above BATTERY_WARN_CLEAR_PCT it
# clears. The gap is the point: without it a battery hovering at one threshold
# flickers the LED between yellow and green all afternoon, and a wearer learns
# to ignore it.
BATTERY_WARN_PCT = 20
BATTERY_WARN_CLEAR_PCT = 25

# BATTERY_HALT_PCT and BATTERY_FLOOR_MV are PiJuice's own shipped defaults for
# the same two jobs (``min_charge.threshold`` 10 and ``min_bat_voltage``
# 3.3 V). Matching them is deliberate: the HAT's own system task is the
# independent backstop for a wedged daemon, and the two must not disagree about
# when the battery is empty.
BATTERY_HALT_PCT = 10
BATTERY_FLOOR_MV = 3300

PCT_AVERAGE_WINDOW = 5
HALT_CONFIRMATIONS = 3
# Two rather than three because the floor is the emergency path, and rather
# than one because a lone sagging sample during a WiFi transmit burst is the
# noise this module exists to reject.
FLOOR_CONFIRMATIONS = 2

# The charge level the PiJuice is told to wake the Pi at after a low-battery
# power-off. Deliberately the hysteresis *clear* threshold rather than
# PiJuice's own 50 % default: a pendant that wakes at 25 % comes up already out
# of the low-battery band instead of booting straight into a yellow LED.
WAKE_ON_CHARGE_PCT = BATTERY_WARN_CLEAR_PCT

# Samples the boot check takes before it believes a halt, and the gap between
# them. Three seconds is nothing against a day of recording, and it is what
# stops one bad read at power-on costing that day.
BATTERY_BOOT_SAMPLES = 3
BATTERY_BOOT_SAMPLE_INTERVAL_SEC = 1.0

_logger = logging.getLogger(__name__)


class BatteryReader(Protocol):
    """The minimum every battery source can answer."""

    def get_charge_pct(self) -> int: ...


@runtime_checkable
class BatteryTelemetryReader(BatteryReader, Protocol):
    """A source that also reports terminal voltage and external power.

    Kept separate from ``BatteryReader`` rather than folded into it so a
    percentage-only source - ``VISIO_BATTERY_SOURCE=none``, the USB power bank
    used while the PiJuice is unavailable - needs no change and reaches none of
    the paths that telemetry unlocks.
    """

    def get_battery_voltage_mv(self) -> int: ...

    def is_externally_powered(self) -> bool: ...


@dataclass(frozen=True)
class BatterySample:
    """One reading, as taken. ``voltage_mv`` is None for a gauge-only source."""

    pct: int
    voltage_mv: int | None = None
    external_power: bool = False


class HaltReason(Enum):
    CHARGE = "charge"
    VOLTAGE = "voltage"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class BatteryVerdict:
    """What the monitor makes of the trace so far.

    ``pct`` is the latest raw reading and is what ``device_status.battery_pct``
    reports: that row is a readout of the gauge, and the average exists to
    decide rather than to display. ``smoothed_pct`` is what every percentage
    threshold is actually compared against.
    """

    pct: int
    smoothed_pct: int
    is_low: bool
    halt_reason: HaltReason | None
    external_power: bool

    @property
    def should_halt(self) -> bool:
        return self.halt_reason is not None


def read_battery_sample(reader: BatteryReader) -> BatterySample:
    """Read whichever source is wired, for everything it can report.

    A reader that does not implement ``BatteryTelemetryReader`` yields no
    voltage, so the floor is never evaluated for it, and unknown external power
    is reported as absent. Absent is the safe direction: it can only make the
    firmware *more* willing to halt, and halting an idle Pi is recoverable
    while browning out mid-write is not.
    """
    if isinstance(reader, BatteryTelemetryReader):
        return BatterySample(
            pct=reader.get_charge_pct(),
            voltage_mv=reader.get_battery_voltage_mv(),
            external_power=reader.is_externally_powered(),
        )
    return BatterySample(pct=reader.get_charge_pct())


class BatteryMonitor:
    """Turns a stream of samples into a stable verdict.

    Holds the three pieces of state that make a verdict stable: the moving
    average window, the low-battery latch, and the two consecutive-confirmation
    counts. Not thread safe, and deliberately so - it is fed from the main loop
    thread alone, and ``BatteryState`` is how any other thread reads the
    result.
    """

    def __init__(
        self,
        *,
        warn_pct: int = BATTERY_WARN_PCT,
        warn_clear_pct: int = BATTERY_WARN_CLEAR_PCT,
        halt_pct: int = BATTERY_HALT_PCT,
        floor_mv: int = BATTERY_FLOOR_MV,
        average_window: int = PCT_AVERAGE_WINDOW,
        halt_confirmations: int = HALT_CONFIRMATIONS,
        floor_confirmations: int = FLOOR_CONFIRMATIONS,
    ) -> None:
        self._warn_pct = warn_pct
        self._warn_clear_pct = warn_clear_pct
        self._halt_pct = halt_pct
        self._floor_mv = floor_mv
        self._halt_confirmations = halt_confirmations
        self._floor_confirmations = floor_confirmations
        self._window: deque[int] = deque(maxlen=average_window)
        self._is_low = False
        self._halt_streak = 0
        self._floor_streak = 0
        self._unreadable_streak = 0

    def read(self, reader: BatteryReader) -> BatteryVerdict:
        """Take one sample from ``reader`` and fold it into the verdict."""
        return self.observe(read_battery_sample(reader))

    def observe(self, sample: BatterySample) -> BatteryVerdict:
        self._unreadable_streak = 0
        self._window.append(sample.pct)
        smoothed = round(sum(self._window) / len(self._window))

        if self._is_low:
            self._is_low = smoothed < self._warn_clear_pct
        else:
            self._is_low = smoothed < self._warn_pct

        halt_reason = self._observe_halt(sample, smoothed)
        return BatteryVerdict(
            pct=sample.pct,
            smoothed_pct=smoothed,
            is_low=self._is_low,
            halt_reason=halt_reason,
            external_power=sample.external_power,
        )

    def observe_unreadable(self, last: BatteryVerdict) -> BatteryVerdict:
        """The verdict after a reading that failed outright.

        An isolated failure keeps ``last``: one bad I2C transfer is noise like
        one sagging sample is. A gauge that stays unreadable for
        ``halt_confirmations`` readings in a row is treated as a confirmed
        empty battery, because a pendant that cannot see its battery cannot
        tell a full cell from one about to brown out mid-write, and halting an
        idle Pi is recoverable while that brownout is not. External power is
        reported absent for the same reason ``read_battery_sample`` reports it
        absent when unknown. The next successful reading resets the count.
        """
        self._unreadable_streak += 1
        if self._unreadable_streak < self._halt_confirmations:
            return last
        _logger.error(
            "battery gauge unreadable for %d consecutive readings; halting",
            self._unreadable_streak,
        )
        return replace(last, halt_reason=HaltReason.UNREADABLE, external_power=False)

    def _observe_halt(self, sample: BatterySample, smoothed: int) -> HaltReason | None:
        """The confirmed halt this sample completes, if it completes one.

        External power clears both counts and returns nothing at all, so a
        pendant on a charger cannot shut itself down no matter what the gauge
        says. PiJuice's own ``pijuice_sys.py`` does the same thing, returning
        from both of its evaluators before reading the gauge whenever external
        power is present.
        """
        if sample.external_power:
            self._halt_streak = 0
            self._floor_streak = 0
            return None

        # The floor is compared against the raw reading, never the average:
        # smoothing a sag would reintroduce exactly the lag the floor is there
        # to cover. Its debounce is the confirmation count instead.
        if sample.voltage_mv is not None and sample.voltage_mv < self._floor_mv:
            self._floor_streak += 1
        else:
            self._floor_streak = 0

        self._halt_streak = self._halt_streak + 1 if smoothed < self._halt_pct else 0

        if self._floor_streak >= self._floor_confirmations:
            _logger.error(
                "battery voltage %s mV confirmed below the %d mV floor over %d "
                "readings; halting",
                sample.voltage_mv,
                self._floor_mv,
                self._floor_streak,
            )
            return HaltReason.VOLTAGE
        if self._halt_streak >= self._halt_confirmations:
            _logger.error(
                "battery charge %d%% (smoothed) confirmed below %d%% over %d "
                "readings; halting",
                smoothed,
                self._halt_pct,
                self._halt_streak,
            )
            return HaltReason.CHARGE
        return None


class BatteryState:
    """The debounced battery: owned by one thread, readable from any.

    The debounce is stateful, so it must have exactly one owner. Before this,
    the upload worker read the battery itself once per segment: two I2C reads
    per segment on two threads that could disagree about the same battery, and
    a moving average fed from both would simply be wrong. Now the main loop
    calls ``sample`` and every other thread calls ``current``.

    Construction takes the first reading, so a state always has a real verdict
    to hand out rather than a neutral one that would be a lie.
    """

    def __init__(
        self, reader: BatteryReader, monitor: BatteryMonitor | None = None
    ) -> None:
        self._reader = reader
        self._monitor = BatteryMonitor() if monitor is None else monitor
        self._lock = Lock()
        self._current = self._monitor.read(reader)

    def sample(self) -> BatteryVerdict:
        """Take a reading, fold it into the debounce, and publish the verdict.

        Only the owning thread may call this; every other thread calls
        ``current``. A reading that fails - an I2C transfer is most likely to
        fail exactly when the cell is sagging - is logged and goes to
        ``BatteryMonitor.observe_unreadable``: an isolated failure keeps the
        verdict already published, so a halt confirmed before it stays
        confirmed and a failed read can never cancel or crash the power-off
        that follows, while a run of them confirms a halt of its own.
        """
        try:
            verdict = self._monitor.read(self._reader)
        except Exception:
            _logger.exception("battery reading failed")
            verdict = self._monitor.observe_unreadable(self.current())
        with self._lock:
            self._current = verdict
        return verdict

    def current(self) -> BatteryVerdict:
        with self._lock:
            return self._current
