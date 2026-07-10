import threading
from datetime import datetime

from visio_recorder.daemon import LoopDeps, run_recording_loop, run_startup_sequence
from visio_recorder.led import LedPattern

from tests.fakes import (
    FakeBatteryReader,
    FakeClock,
    FakeCommandRunner,
    FakeDiskStatsReader,
    FakeLedDriver,
    FakeStatusClient,
    FakeStorageClient,
    ScriptedStorageClient,
    TriggerableBatteryReader,
)
from visio_recorder.recording_loop import DiskStats

CRITICAL_LED = ((255, 0, 0), LedPattern.FLASHING)


def _make_deps(
    tmp_path,
    *,
    command_runner,
    battery_reader,
    led_driver,
    storage_client=None,
    status_client=None,
) -> LoopDeps:
    return LoopDeps(
        command_runner=command_runner,
        storage_client=storage_client or FakeStorageClient(),
        status_client=status_client or FakeStatusClient(),
        led_driver=led_driver,
        battery_reader=battery_reader,
        disk_stats_reader=FakeDiskStatsReader(DiskStats(used_gb=1.0, free_gb=9.0)),
        data_dir=tmp_path / "data",
        queue_dir=tmp_path / "queue",
        device_id="device-abc",
        segment_duration_ms=1000,
        framerate=30,
    )


def test_each_cycle_records_a_named_segment_and_worker_uploads_it(tmp_path):
    stop = threading.Event()
    led = FakeLedDriver()
    storage = FakeStorageClient()
    status = FakeStatusClient()

    runner = FakeCommandRunner(
        on_record=lambda count: stop.set() if count >= 3 else None
    )
    deps = _make_deps(
        tmp_path,
        command_runner=runner,
        battery_reader=FakeBatteryReader(85),
        led_driver=led,
        storage_client=storage,
        status_client=status,
    )

    run_recording_loop(
        deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0))
    )

    assert runner.record_count == 3
    assert storage.uploaded == [
        "device-abc/20260704_120000.mp4",
        "device-abc/20260704_120001.mp4",
        "device-abc/20260704_120002.mp4",
    ]
    assert len(status.upserts) == 3


def test_halt_battery_sets_critical_led_drains_queue_and_exits(tmp_path):
    stop = threading.Event()
    led = FakeLedDriver()
    storage = FakeStorageClient()
    battery = TriggerableBatteryReader(healthy_pct=85, halt_pct=5)

    runner = FakeCommandRunner(
        on_record=lambda count: battery.trigger_halt() if count >= 2 else None
    )
    deps = _make_deps(
        tmp_path,
        command_runner=runner,
        battery_reader=battery,
        led_driver=led,
        storage_client=storage,
    )

    run_recording_loop(
        deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0))
    )

    assert runner.record_count == 2
    assert CRITICAL_LED in led.calls
    # Both recorded-before-halt segments were drained by the worker.
    assert len(storage.uploaded) == 2


def test_three_consecutive_upload_failures_set_critical_and_success_resets(tmp_path):
    stop = threading.Event()
    led = FakeLedDriver()
    # Segments 1-3 fail (-> CRITICAL), 4 succeeds (-> reset), 5-7 fail (-> CRITICAL).
    storage = ScriptedStorageClient(fail_on_attempts={1, 2, 3, 5, 6, 7})

    runner = FakeCommandRunner(
        on_record=lambda count: stop.set() if count >= 7 else None
    )
    deps = _make_deps(
        tmp_path,
        command_runner=runner,
        battery_reader=FakeBatteryReader(85),
        led_driver=led,
        storage_client=storage,
    )

    run_recording_loop(
        deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0))
    )

    # Recording continued past the first CRITICAL: all seven cycles ran.
    assert runner.record_count == 7
    # The one success (segment 4) reset the counter, so the threshold was
    # reached twice: after segments 3 and 7.
    assert led.calls.count(CRITICAL_LED) == 2
    assert storage.uploaded == ["device-abc/20260704_120003.mp4"]


def test_setting_stop_exits_after_in_flight_cycle_and_joins_worker(tmp_path):
    stop = threading.Event()
    led = FakeLedDriver()
    storage = FakeStorageClient()

    runner = FakeCommandRunner(
        on_record=lambda count: stop.set() if count >= 2 else None
    )
    deps = _make_deps(
        tmp_path,
        command_runner=runner,
        battery_reader=FakeBatteryReader(85),
        led_driver=led,
        storage_client=storage,
    )

    run_recording_loop(
        deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0))
    )

    # Stop was observed after the in-flight cycle; exactly two segments ran
    # and both were uploaded before the worker joined.
    assert runner.record_count == 2
    assert len(storage.uploaded) == 2


def test_healthy_battery_proceeds_and_shows_recording_led():
    led = FakeLedDriver()
    result = run_startup_sequence(FakeBatteryReader(85), led)

    assert result.proceed is True
    assert result.battery_pct == 85
    assert led.calls == [((0, 255, 0), LedPattern.SOLID)]


def test_low_battery_proceeds_and_shows_low_battery_led():
    led = FakeLedDriver()
    result = run_startup_sequence(FakeBatteryReader(15), led)

    assert result.proceed is True
    assert result.battery_pct == 15
    assert led.calls == [((255, 255, 0), LedPattern.PULSING)]


def test_battery_at_warn_threshold_shows_recording_led():
    led = FakeLedDriver()
    result = run_startup_sequence(FakeBatteryReader(20), led)

    assert result.proceed is True
    assert result.battery_pct == 20
    assert led.calls == [((0, 255, 0), LedPattern.SOLID)]


def test_critical_battery_halts_and_shows_critical_led():
    led = FakeLedDriver()
    result = run_startup_sequence(FakeBatteryReader(5), led)

    assert result.proceed is False
    assert result.battery_pct == 5
    assert led.calls == [((255, 0, 0), LedPattern.FLASHING)]
