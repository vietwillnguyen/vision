import pytest

from tests.fakes import FakeBatteryReader, FakeTelemetryBatteryReader
from visio_recorder.battery import (
    BATTERY_FLOOR_MV,
    BATTERY_HALT_PCT,
    BATTERY_WARN_CLEAR_PCT,
    BATTERY_WARN_PCT,
    FLOOR_CONFIRMATIONS,
    HALT_CONFIRMATIONS,
    PCT_AVERAGE_WINDOW,
    BatteryMonitor,
    BatterySample,
    BatteryState,
    HaltReason,
    read_battery_sample,
)


def on_battery(pct: int, voltage_mv: int | None = None) -> BatterySample:
    return BatterySample(pct=pct, voltage_mv=voltage_mv, external_power=False)


def observe_all(monitor: BatteryMonitor, percentages):
    """Feed a whole percentage trace and return every verdict it produced."""
    return [monitor.observe(on_battery(pct)) for pct in percentages]


# --- reading a sample out of whichever reader is wired -----------------------


def test_a_percentage_only_reader_yields_no_voltage_and_no_external_power():
    # VISIO_BATTERY_SOURCE=none is such a reader. It must stay readable without
    # implementing telemetry it has no hardware for.
    sample = read_battery_sample(FakeBatteryReader(100))

    assert sample == BatterySample(pct=100, voltage_mv=None, external_power=False)


def test_a_telemetry_reader_yields_voltage_and_external_power():
    reader = FakeTelemetryBatteryReader(pct=42, voltage_mv=3810, external_power=True)

    assert read_battery_sample(reader) == BatterySample(
        pct=42, voltage_mv=3810, external_power=True
    )


# --- the low-battery latch and its hysteresis --------------------------------


def test_a_healthy_battery_is_neither_low_nor_halting():
    verdict = BatteryMonitor().observe(on_battery(85))

    assert verdict.pct == 85
    assert verdict.is_low is False
    assert verdict.should_halt is False
    assert verdict.halt_reason is None


def test_the_low_latch_sets_below_the_warn_threshold():
    verdict = BatteryMonitor().observe(on_battery(BATTERY_WARN_PCT - 1))

    assert verdict.is_low is True
    assert verdict.should_halt is False


def test_the_low_latch_does_not_set_at_the_warn_threshold():
    assert BatteryMonitor().observe(on_battery(BATTERY_WARN_PCT)).is_low is False


def test_the_low_latch_holds_everywhere_inside_the_hysteresis_band():
    # Charge recovering from 19 up through 24 stays inside the 20-25 band, so
    # the latch must stay set: this flicker is what the band exists to prevent.
    monitor = BatteryMonitor()
    monitor.observe(on_battery(19))

    for pct in range(BATTERY_WARN_PCT, BATTERY_WARN_CLEAR_PCT):
        assert monitor.observe(on_battery(pct)).is_low is True, pct


def test_the_low_latch_clears_once_the_smoothed_charge_reaches_the_clear_level():
    monitor = BatteryMonitor()
    assert monitor.observe(on_battery(19)).is_low is True

    verdicts = observe_all(monitor, [40] * PCT_AVERAGE_WINDOW)

    assert verdicts[-1].smoothed_pct >= BATTERY_WARN_CLEAR_PCT
    assert verdicts[-1].is_low is False


def test_a_single_dip_under_the_warn_threshold_does_not_set_the_latch():
    # The moving average absorbs it: one 15 among four 30s averages to 27.
    monitor = BatteryMonitor()

    assert observe_all(monitor, [30, 30, 30, 30, 15])[-1].is_low is False


# --- the charge halt ---------------------------------------------------------


def test_one_sample_under_the_halt_threshold_never_halts():
    # The whole point: a single reading is not evidence, however low it is.
    assert BatteryMonitor().observe(on_battery(1)).should_halt is False


def test_the_halt_needs_consecutive_confirming_readings():
    monitor = BatteryMonitor()

    verdicts = observe_all(monitor, [5] * HALT_CONFIRMATIONS)

    assert [v.should_halt for v in verdicts[:-1]] == [False] * (HALT_CONFIRMATIONS - 1)
    assert verdicts[-1].should_halt is True
    assert verdicts[-1].halt_reason is HaltReason.CHARGE


def test_a_healthy_reading_breaks_the_halt_streak():
    # Confirmations must be consecutive, not cumulative: a gauge that dips and
    # recovers is reporting load, not depletion.
    monitor = BatteryMonitor()
    observe_all(monitor, [5] * (HALT_CONFIRMATIONS - 1))

    assert monitor.observe(on_battery(40)).should_halt is False
    assert monitor.observe(on_battery(5)).should_halt is False


def test_a_sustained_low_run_still_halts_after_an_interruption():
    # Breaking the streak delays the halt; it does not cancel it.
    monitor = BatteryMonitor()
    monitor.observe(on_battery(40))

    verdicts = observe_all(monitor, [5] * (PCT_AVERAGE_WINDOW + HALT_CONFIRMATIONS))

    assert verdicts[-1].should_halt is True


def test_the_halt_compares_the_smoothed_percentage_not_the_raw_one():
    # A noisy trace whose average stays above the halt threshold must not halt,
    # even though every other raw sample is far below it.
    monitor = BatteryMonitor()

    verdicts = observe_all(monitor, [25, 2, 25, 2, 25, 2])

    assert not any(v.should_halt for v in verdicts)
    assert verdicts[-1].pct == 2
    assert verdicts[-1].smoothed_pct > BATTERY_HALT_PCT


def test_a_sagging_then_recovering_trace_neither_halts_nor_latches_low():
    # The shape of a camera encode plus a WiFi burst on a half-full pack.
    monitor = BatteryMonitor()

    verdicts = observe_all(monitor, [40, 35, 18, 16, 19, 34, 38, 41])

    assert not any(v.should_halt for v in verdicts)
    assert not any(v.is_low for v in verdicts)


# --- the voltage floor -------------------------------------------------------


def test_the_voltage_floor_halts_even_while_the_percentage_looks_healthy():
    # The reason the floor exists: the fuel gauge lags under load, so a cell
    # already at the floor can still be reported as half full.
    monitor = BatteryMonitor()
    sample = on_battery(55, voltage_mv=BATTERY_FLOOR_MV - 1)

    for _ in range(FLOOR_CONFIRMATIONS - 1):
        assert monitor.observe(sample).should_halt is False
    verdict = monitor.observe(sample)

    assert verdict.should_halt is True
    assert verdict.halt_reason is HaltReason.VOLTAGE


def test_one_sagging_voltage_sample_does_not_halt():
    monitor = BatteryMonitor()

    assert monitor.observe(on_battery(55, voltage_mv=3100)).should_halt is False


def test_a_recovering_voltage_sample_resets_the_floor_streak():
    # A WiFi transmit burst sags the rail for one sample and it comes back.
    monitor = BatteryMonitor()
    for _ in range(FLOOR_CONFIRMATIONS - 1):
        monitor.observe(on_battery(55, voltage_mv=3100))

    monitor.observe(on_battery(55, voltage_mv=3900))

    for _ in range(FLOOR_CONFIRMATIONS - 1):
        assert monitor.observe(on_battery(55, voltage_mv=3100)).should_halt is False
    assert monitor.observe(on_battery(55, voltage_mv=3100)).should_halt is True


def test_an_alternating_voltage_trace_never_confirms_the_floor():
    # Mean 3300, but never FLOOR_CONFIRMATIONS consecutive samples under it.
    # Raw and consecutive, both, are what make this trace survivable.
    monitor = BatteryMonitor()
    trace = [3200, 3400, 3100, 3500, 3200, 3400]

    verdicts = [monitor.observe(on_battery(55, voltage_mv=mv)) for mv in trace]

    assert not any(v.should_halt for v in verdicts)


def test_a_reader_without_voltage_never_trips_the_floor():
    monitor = BatteryMonitor()

    for _ in range(FLOOR_CONFIRMATIONS + 5):
        assert monitor.observe(on_battery(55, voltage_mv=None)).should_halt is False


def test_voltage_at_the_floor_is_not_under_it():
    monitor = BatteryMonitor()

    for _ in range(FLOOR_CONFIRMATIONS + 1):
        verdict = monitor.observe(on_battery(55, voltage_mv=BATTERY_FLOOR_MV))
        assert verdict.should_halt is False


# --- external power ----------------------------------------------------------


def test_external_power_never_halts_however_low_the_charge():
    monitor = BatteryMonitor()
    charging = BatterySample(pct=1, voltage_mv=3000, external_power=True)

    for _ in range(HALT_CONFIRMATIONS + FLOOR_CONFIRMATIONS + 3):
        verdict = monitor.observe(charging)
        assert verdict.should_halt is False
        assert verdict.external_power is True


def test_external_power_resets_a_halt_streak_already_building():
    monitor = BatteryMonitor()
    observe_all(monitor, [5] * (HALT_CONFIRMATIONS - 1))

    monitor.observe(BatterySample(pct=5, voltage_mv=None, external_power=True))

    # The streak restarts from zero, so the next low sample cannot be the one
    # that confirms a halt.
    assert monitor.observe(on_battery(5)).should_halt is False


def test_unplugging_after_charging_can_still_halt():
    monitor = BatteryMonitor()
    monitor.observe(BatterySample(pct=5, voltage_mv=None, external_power=True))

    verdicts = observe_all(monitor, [5] * HALT_CONFIRMATIONS)

    assert verdicts[-1].should_halt is True
    assert verdicts[-1].halt_reason is HaltReason.CHARGE


def test_a_charging_battery_still_reports_low_so_the_led_stays_honest():
    # The charging exemption is about halting, not about the warning: a pendant
    # charging from 8 % is genuinely low and the wearer should see it.
    verdict = BatteryMonitor().observe(
        BatterySample(pct=8, voltage_mv=None, external_power=True)
    )

    assert verdict.is_low is True
    assert verdict.should_halt is False


# --- the moving average ------------------------------------------------------


def test_the_average_uses_what_has_arrived_before_the_window_fills():
    # A pendant two samples into its run is already protected rather than
    # running unguarded until the window is full.
    monitor = BatteryMonitor()
    monitor.observe(on_battery(40))

    assert monitor.observe(on_battery(20)).smoothed_pct == 30


def test_the_average_forgets_samples_older_than_the_window():
    monitor = BatteryMonitor()
    observe_all(monitor, [100] * PCT_AVERAGE_WINDOW)

    verdicts = observe_all(monitor, [10] * PCT_AVERAGE_WINDOW)

    assert verdicts[-1].smoothed_pct == 10


def test_the_verdict_reports_the_raw_percentage_for_the_status_row():
    # device_status.battery_pct is a readout of the gauge; the average exists
    # to decide, not to display.
    monitor = BatteryMonitor()
    monitor.observe(on_battery(90))

    assert monitor.observe(on_battery(50)).pct == 50


# --- reading straight from a reader -----------------------------------------


def test_the_monitor_reads_a_reader_directly():
    monitor = BatteryMonitor()

    verdict = monitor.read(FakeTelemetryBatteryReader(pct=12, voltage_mv=3700))

    assert verdict.pct == 12
    assert verdict.is_low is True


# --- the published state the upload worker reads -----------------------------


def test_battery_state_seeds_itself_from_one_reading():
    # A state always has a real verdict to hand out, never a neutral one.
    assert BatteryState(FakeBatteryReader(90)).current().pct == 90


def test_sampling_publishes_the_new_verdict_for_the_other_thread():
    # The upload worker reads this instead of touching the battery itself, so
    # the debounce has exactly one owner and the two threads cannot disagree.
    reader = FakeTelemetryBatteryReader(pct=90)
    state = BatteryState(reader)
    reader.pct = 15

    verdict = state.sample()

    assert verdict.pct == 15
    assert state.current() is verdict


def test_the_state_folds_every_sample_into_one_shared_debounce():
    # The seeding read counts as the first confirmation, so a reader pinned at
    # a halt level confirms after HALT_CONFIRMATIONS reads in total.
    reader = FakeTelemetryBatteryReader(pct=5)
    state = BatteryState(reader)

    for _ in range(HALT_CONFIRMATIONS - 2):
        assert state.sample().should_halt is False
    assert state.sample().should_halt is True


@pytest.mark.parametrize("pct", [0, 100])
def test_the_extremes_are_read_without_special_casing(pct):
    verdict = BatteryMonitor().observe(on_battery(pct))

    assert verdict.pct == pct
    assert verdict.is_low is (pct < BATTERY_WARN_PCT)
    assert verdict.should_halt is False
