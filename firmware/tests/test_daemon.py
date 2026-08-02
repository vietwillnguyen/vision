import threading
from datetime import datetime
from pathlib import Path

import pytest

from tests.fakes import (
    FakeBatteryReader,
    FakeClock,
    FakeCommandRunner,
    FakeDiskStatsReader,
    FakeLedDriver,
    FakeStatusClient,
    FakeStorageClient,
    ScriptedStatusClient,
    ScriptedStorageClient,
    TriggerableBatteryReader,
)
from visio_recorder.daemon import (
    LoopDeps,
    load_config,
    run_recording_loop,
    run_startup_sequence,
)
from visio_recorder.led import LedPattern
from visio_recorder.recording_loop import DiskStats

CRITICAL_LED = ((255, 0, 0), LedPattern.FLASHING)
RECORDING_LED = ((0, 255, 0), LedPattern.SOLID)
UPLOADING_LED = ((0, 0, 255), LedPattern.PULSING)
LOW_BATTERY_LED = ((255, 255, 0), LedPattern.PULSING)


def test_load_config_reads_all_values_from_env():
    env = {
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_ANON_KEY": "anon-key-123",
        "VISIO_DATA_DIR": "/data/visio",
        "VISIO_SEGMENT_DURATION_MS": "60000",
        "VISIO_FRAMERATE": "24",
    }

    config = load_config(env)

    assert config.supabase_url == "https://example.supabase.co"
    assert config.supabase_anon_key == "anon-key-123"
    assert config.data_dir == Path("/data/visio")
    assert config.segment_duration_ms == 60000
    assert config.framerate == 24


def test_load_config_applies_defaults_when_optional_keys_absent():
    env = {
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_ANON_KEY": "anon-key-123",
    }

    config = load_config(env)

    assert config.data_dir == Path("/var/lib/visio-recorder")
    assert config.segment_duration_ms == 300000
    assert config.framerate == 30


def test_load_config_missing_supabase_url_raises_value_error_naming_it():
    env = {"SUPABASE_ANON_KEY": "anon-key-123"}

    with pytest.raises(ValueError, match="SUPABASE_URL"):
        load_config(env)


def test_load_config_missing_supabase_anon_key_raises_value_error_naming_it():
    env = {"SUPABASE_URL": "https://example.supabase.co"}

    with pytest.raises(ValueError, match="SUPABASE_ANON_KEY"):
        load_config(env)


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

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0)))

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

    # The mux gate holds the worker back until both segments are recorded and
    # the halt is triggered, guaranteeing the queue is still full when the
    # main loop observes the halt: the drain provably happens after it.
    gate = threading.Event()

    def on_record(count: int) -> None:
        if count >= 2:
            battery.trigger_halt()
            gate.set()

    runner = FakeCommandRunner(on_record=on_record, mux_gate=gate)
    deps = _make_deps(
        tmp_path,
        command_runner=runner,
        battery_reader=battery,
        led_driver=led,
        storage_client=storage,
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0)))

    assert runner.record_count == 2
    # Both recorded-before-halt segments were drained by the worker.
    assert len(storage.uploaded) == 2
    # The drained segments see the halted (low) battery, so each applies
    # LOW_BATTERY twice; the halt CRITICAL must come after the whole drain so
    # a battery-halted device finishes red, never green/yellow.
    assert led.calls == [LOW_BATTERY_LED] * 4 + [CRITICAL_LED]
    assert led.calls[-1] == CRITICAL_LED


def test_halt_join_waits_for_slow_worker_before_applying_critical_led(tmp_path):
    # Regression test: the halt path used to join(timeout=5), so a worker
    # call running past 5s let the main thread give up waiting and apply the
    # halt CRITICAL LED while the worker was still mid-flight; the worker
    # could then overwrite it with its own restore LED. Joining without a
    # timeout closes that race, so this proves the loop stays blocked well
    # past the old 5s window and CRITICAL is applied only after the worker's
    # own LED restore has already happened.
    stop = threading.Event()
    led = FakeLedDriver()
    battery = TriggerableBatteryReader(healthy_pct=85, halt_pct=5)

    release_upload = threading.Event()
    upload_blocked = threading.Event()

    class BlockingStorageClient:
        def __init__(self) -> None:
            self.uploaded: list[str] = []

        def upload(self, bucket, object_path, local_path):
            upload_blocked.set()
            release_upload.wait()
            self.uploaded.append(object_path)

    storage = BlockingStorageClient()

    def on_record(count: int) -> None:
        if count >= 1:
            battery.trigger_halt()

    runner = FakeCommandRunner(on_record=on_record)
    deps = _make_deps(
        tmp_path,
        command_runner=runner,
        battery_reader=battery,
        led_driver=led,
        storage_client=storage,
    )

    loop_thread = threading.Thread(
        target=run_recording_loop,
        args=(deps, stop),
        kwargs={"clock": FakeClock(datetime(2026, 7, 4, 12, 0, 0))},
    )
    loop_thread.start()

    assert upload_blocked.wait(timeout=5), "worker never reached the blocking upload"
    # Wait past the old 5s join timeout: with the bug, the main thread would
    # have given up here and returned, applying CRITICAL while the worker was
    # still blocked above.
    loop_thread.join(timeout=5.2)
    assert loop_thread.is_alive(), "join returned before the worker finished"
    assert CRITICAL_LED not in led.calls

    release_upload.set()
    loop_thread.join(timeout=5)
    assert not loop_thread.is_alive()

    assert led.calls == [LOW_BATTERY_LED, LOW_BATTERY_LED, CRITICAL_LED]


def test_three_consecutive_upload_failures_set_critical_and_success_resets(tmp_path):
    stop = threading.Event()
    led = FakeLedDriver()
    # Segments 1-4 fail (CRITICAL at 3 and STILL CRITICAL at 4: a sustained
    # outage must stay red, not blip red once), 5 succeeds (reset), 6-8 fail
    # (counter climbs back to the threshold at 8).
    storage = ScriptedStorageClient(fail_on_attempts={1, 2, 3, 4, 6, 7, 8})

    runner = FakeCommandRunner(
        on_record=lambda count: stop.set() if count >= 8 else None
    )
    deps = _make_deps(
        tmp_path,
        command_runner=runner,
        battery_reader=FakeBatteryReader(85),
        led_driver=led,
        storage_client=storage,
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0)))

    # Recording continued past the first CRITICAL: all eight cycles ran.
    assert runner.record_count == 8
    assert storage.uploaded == ["device-abc/20260704_120004.mp4"]
    # All LED calls come from the single worker in FIFO order, so the exact
    # sequence is deterministic. on_segment_complete applies UPLOADING then a
    # restore per segment; the worker must re-apply CRITICAL after every
    # failure at-or-past the threshold (segments 3, 4, and 8), so the LED
    # state after draining each of those segments is CRITICAL.
    per_segment = [UPLOADING_LED, RECORDING_LED]
    assert led.calls == (
        per_segment  # 1: fail (1 consecutive)
        + per_segment  # 2: fail (2)
        + per_segment
        + [CRITICAL_LED]  # 3: fail (3 -> threshold)
        + per_segment
        + [CRITICAL_LED]  # 4: fail (4 -> stays critical)
        + per_segment  # 5: success (reset)
        + per_segment  # 6: fail (1)
        + per_segment  # 7: fail (2)
        + per_segment
        + [CRITICAL_LED]  # 8: fail (3 -> threshold again)
    )


def test_worker_survives_status_upsert_exception_and_keeps_processing(tmp_path):
    stop = threading.Event()
    led = FakeLedDriver()
    storage = FakeStorageClient()
    # The status upsert raises on the first segment only; the worker must
    # log and continue rather than dying, so segments 2 and 3 still get
    # muxed, uploaded, and their status reported.
    status = ScriptedStatusClient(fail_on_attempts={1})

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

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0)))

    # All three cycles ran to completion: recording never stalled, and the
    # worker thread joined cleanly (run_recording_loop returning at all after
    # stop is set proves join() didn't hang).
    assert runner.record_count == 3
    # Segment 1's own upload may or may not have completed depending on where
    # in on_segment_complete the exception fires, so we don't assert on it
    # directly. The load-bearing assertion is that segments 2 and 3 -
    # recorded after the worker swallowed the segment-1 exception - still got
    # uploaded and their status reported, proving the worker kept running.
    assert "device-abc/20260704_120001.mp4" in storage.uploaded
    assert "device-abc/20260704_120002.mp4" in storage.uploaded
    assert len(status.upserts) == 2


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

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0)))

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
