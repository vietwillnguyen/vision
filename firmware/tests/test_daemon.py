import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.fakes import (
    FailingStorageClient,
    FakeBatteryReader,
    FakeClock,
    FakeCommandRunner,
    FakeDiskStatsReader,
    FakeLedDriver,
    FakePiJuicePower,
    FakePowerController,
    FakeStatusClient,
    FakeStorageClient,
    FakeTelemetryBatteryReader,
    ScriptedBatteryReader,
    ScriptedStatusClient,
    ScriptedStorageClient,
    SimulatedSpiDevice,
    TriggerableBatteryReader,
    battery_state,
    responsive_battery,
    unlimited_upload_limit,
)
from tests.ws2812_waveform import decode_frame
from visio_recorder.battery import (
    HALT_CONFIRMATIONS,
    WAKE_ON_CHARGE_PCT,
    BatteryMonitor,
    BatteryState,
)
from visio_recorder.capture import AudioSettings, VideoSettings
from visio_recorder.daemon import (
    LoopDeps,
    load_config,
    resolve_audio,
    resolve_power_controller,
    run_recording_loop,
    run_startup_sequence,
)
from visio_recorder.drivers import Ws2812LedDriver
from visio_recorder.led import LedPattern
from visio_recorder.power import (
    PiJuicePowerController,
    SystemdPowerController,
)
from visio_recorder.recording_loop import DiskStats, LoopOutcome, RetryBackoff
from visio_recorder.upload_queue import list_pending

CRITICAL_LED = ((255, 0, 0), LedPattern.FLASHING)
RECORDING_LED = ((0, 255, 0), LedPattern.SOLID)
UPLOADING_LED = ((0, 0, 255), LedPattern.PULSING)
LOW_BATTERY_LED = ((255, 255, 0), LedPattern.PULSING)


def test_load_config_reads_all_values_from_env():
    env = {
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_ANON_KEY": "anon-key-123",
        "VISIO_DATA_DIR": "/data/visio",
        "VISIO_SEGMENT_DURATION_MS": "30000",
        "VISIO_VIDEO_WIDTH": "1920",
        "VISIO_VIDEO_HEIGHT": "1080",
        "VISIO_FRAMERATE": "24",
        "VISIO_VIDEO_BITRATE": "6000000",
        "VISIO_MAX_UPLOAD_BYTES": "524288000",
    }

    config = load_config(env)

    assert config.supabase_url == "https://example.supabase.co"
    assert config.supabase_anon_key == "anon-key-123"
    assert config.data_dir == Path("/data/visio")
    assert config.segment_duration_ms == 30000
    assert config.video == VideoSettings(
        width=1920, height=1080, framerate=24, bitrate_bps=6_000_000
    )
    assert config.max_upload_bytes == 524_288_000


def test_load_config_applies_defaults_when_optional_keys_absent():
    env = {
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_ANON_KEY": "anon-key-123",
    }

    config = load_config(env)

    assert config.data_dir == Path("/var/lib/visio-recorder")
    # 720p30 at 4 Mbps in one-minute segments under the hosted 50 MiB limit.
    assert config.video == VideoSettings(
        width=1280, height=720, framerate=30, bitrate_bps=4_000_000
    )
    assert config.max_upload_bytes == 52_428_800
    assert config.segment_duration_ms == 60_000


def test_load_config_derives_the_segment_length_from_the_configured_bitrate():
    env = {
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_ANON_KEY": "anon-key-123",
        "VISIO_VIDEO_BITRATE": "2000000",
    }

    assert load_config(env).segment_duration_ms == 120_000


def test_load_config_derives_a_longer_segment_under_a_raised_upload_limit():
    env = {
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_ANON_KEY": "anon-key-123",
        "VISIO_MAX_UPLOAD_BYTES": str(100 * 1024 * 1024),
    }

    assert load_config(env).segment_duration_ms == 130_000


def test_load_config_warns_about_an_explicit_duration_predicted_to_overflow(caplog):
    env = {
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_ANON_KEY": "anon-key-123",
        "VISIO_SEGMENT_DURATION_MS": "300000",
    }

    config = load_config(env)

    assert config.segment_duration_ms == 300_000
    assert "will be split before upload" in caplog.text


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
    battery,
    led_driver,
    storage_client=None,
    status_client=None,
    audio=None,
    retry_backoff=None,
    monotonic=None,
    power_controller=None,
    upload_drain_timeout_sec=None,
) -> LoopDeps:
    return LoopDeps(
        command_runner=command_runner,
        storage_client=storage_client or FakeStorageClient(),
        status_client=status_client or FakeStatusClient(),
        led_driver=led_driver,
        battery=battery,
        # Never a real one: no test may reach systemctl poweroff.
        power_controller=power_controller or FakePowerController(),
        disk_stats_reader=FakeDiskStatsReader(DiskStats(used_gb=1.0, free_gb=9.0)),
        data_dir=tmp_path / "data",
        queue_dir=tmp_path / "queue",
        device_id="device-abc",
        segment_duration_ms=1000,
        video=VideoSettings(),
        upload_limit=unlimited_upload_limit(),
        audio=audio,
        **({} if retry_backoff is None else {"retry_backoff": retry_backoff}),
        **({} if monotonic is None else {"monotonic": monotonic}),
        **(
            {}
            if upload_drain_timeout_sec is None
            else {"upload_drain_timeout_sec": upload_drain_timeout_sec}
        ),
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
        battery=battery_state(85),
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
    # One upsert per segment, bracketed by the loop's start and exit reports.
    assert [u["segments_uploaded_today"] for u in status.upserts] == [0, 1, 2, 3, 3]


def test_halt_battery_sets_critical_led_drains_queue_and_powers_off(tmp_path):
    stop = threading.Event()
    led = FakeLedDriver()
    storage = FakeStorageClient()
    power = FakePowerController()
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
        battery=responsive_battery(battery),
        led_driver=led,
        storage_client=storage,
        power_controller=power,
    )

    outcome = run_recording_loop(
        deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0))
    )

    assert outcome is LoopOutcome.HALTED
    assert runner.record_count == 2
    # Both recorded-before-halt segments were drained by the worker.
    assert len(storage.uploaded) == 2
    # The drained segments see the halted (low) battery, so each applies
    # LOW_BATTERY twice; the halt CRITICAL must come after the whole drain so
    # a battery-halted device finishes red, never green/yellow.
    assert led.calls == [LOW_BATTERY_LED] * 4 + [CRITICAL_LED]
    assert led.calls[-1] == CRITICAL_LED
    # And the halt ends in a power-off rather than leaving the Pi powered and
    # discharging, with the wake armed so nothing needs a human to come back.
    assert power.power_offs == [WAKE_ON_CHARGE_PCT]


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
        battery=responsive_battery(battery),
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
        battery=battery_state(85),
        led_driver=led,
        storage_client=storage,
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0)))

    # Recording continued past the first CRITICAL: all eight cycles ran.
    assert runner.record_count == 8
    assert storage.uploaded == ["device-abc/20260704_120004.mp4"]
    # All LED calls come from the single worker in FIFO order, so the exact
    # sequence is deterministic. on_segment_complete makes exactly two LED
    # calls per segment, both derived from the shared UploadHealth: once the
    # counter is at the threshold, every state it derives is CRITICAL, so the
    # red is held across whole segments (4 and 5's upload) rather than being
    # overwritten by UPLOADING blue and restored RECORDING green and then
    # re-applied. The success at segment 5 resets the counter, which is what
    # clears the red.
    per_segment = [UPLOADING_LED, RECORDING_LED]
    assert led.calls == (
        per_segment  # 1: fail (1 consecutive)
        + per_segment  # 2: fail (2)
        + [UPLOADING_LED, CRITICAL_LED]  # 3: fail (3 -> threshold)
        + [CRITICAL_LED, CRITICAL_LED]  # 4: fail (4 -> stays critical)
        + [CRITICAL_LED, RECORDING_LED]  # 5: success resets and clears it
        + per_segment  # 6: fail (1)
        + per_segment  # 7: fail (2)
        + [UPLOADING_LED, CRITICAL_LED]  # 8: fail (3 -> threshold again)
    )


def test_worker_survives_status_upsert_exception_and_keeps_processing(tmp_path):
    stop = threading.Event()
    led = FakeLedDriver()
    storage = FakeStorageClient()
    # The status upsert raises on the first segment only (attempt 1 is the
    # loop's start report); the worker must log and continue rather than
    # dying, so segments 2 and 3 still get muxed, uploaded, and their status
    # reported.
    status = ScriptedStatusClient(fail_on_attempts={2})

    runner = FakeCommandRunner(
        on_record=lambda count: stop.set() if count >= 3 else None
    )
    deps = _make_deps(
        tmp_path,
        command_runner=runner,
        battery=battery_state(85),
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
    # Start report, segments 2 and 3, exit report.
    assert [u["recording_active"] for u in status.upserts] == [
        True,
        True,
        True,
        False,
    ]


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
        battery=battery_state(85),
        led_driver=led,
        storage_client=storage,
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0)))

    # Stop was observed after the in-flight cycle; exactly two segments ran
    # and both were uploaded before the worker joined.
    assert runner.record_count == 2
    assert len(storage.uploaded) == 2


def _startup(reader, led, **kwargs):
    """Run the boot check with its inter-sample sleep stubbed out."""
    slept: list[float] = []
    result = run_startup_sequence(reader, led, sleep=slept.append, **kwargs)
    return result, slept


def test_healthy_battery_proceeds_and_shows_recording_led():
    led = FakeLedDriver()
    result, _ = _startup(FakeTelemetryBatteryReader(pct=85), led)

    assert result.proceed is True
    assert result.battery_pct == 85
    assert led.calls == [RECORDING_LED]


def test_low_battery_proceeds_and_shows_low_battery_led():
    led = FakeLedDriver()
    result, _ = _startup(FakeTelemetryBatteryReader(pct=15), led)

    assert result.proceed is True
    assert result.battery_pct == 15
    assert led.calls == [LOW_BATTERY_LED]


def test_battery_at_warn_threshold_shows_recording_led():
    led = FakeLedDriver()
    result, _ = _startup(FakeTelemetryBatteryReader(pct=20), led)

    assert result.proceed is True
    assert result.battery_pct == 20
    assert led.calls == [RECORDING_LED]


def test_a_battery_flat_across_every_boot_sample_halts_and_shows_critical():
    led = FakeLedDriver()

    result, slept = _startup(FakeTelemetryBatteryReader(pct=5), led)

    assert result.proceed is False
    assert result.battery_pct == 5
    assert led.calls == [CRITICAL_LED]
    # Three samples, so two waits: the whole cost of not trusting one reading.
    assert slept == [1.0, 1.0]


def test_one_bad_reading_at_power_on_does_not_cost_the_day():
    # The regression this exists for: the boot check used to halt a whole day
    # of recording on a single sample, and a gauge reporting one bad value at
    # power-on is exactly what it would halt on.
    led = FakeLedDriver()
    reader = ScriptedBatteryReader([(2, 3900, False), (80, 3900, False)])

    result, _ = _startup(reader, led)

    assert result.proceed is True
    assert led.calls == [RECORDING_LED]


class FailsFirstReads(FakeTelemetryBatteryReader):
    """A gauge whose first ``failures`` charge reads raise, as a faulted bus."""

    def __init__(self, failures: int, pct: int = 85) -> None:
        super().__init__(pct=pct)
        self.failures = failures
        self.reads = 0

    def get_charge_pct(self) -> int:
        self.reads += 1
        if self.reads <= self.failures:
            raise RuntimeError("pijuice GetChargeLevel failed: COMM_ERROR")
        return super().get_charge_pct()


def test_a_gauge_faulted_from_power_on_refuses_the_boot_so_main_powers_off():
    # Before, the first read raised out of main() and systemd restarted it
    # forever, leaving an idle Pi powered until it browned out.
    led = FakeLedDriver()
    reader = FailsFirstReads(failures=10**6)

    result, slept = _startup(reader, led)

    assert result.proceed is False
    assert result.battery is None
    assert reader.reads == HALT_CONFIRMATIONS
    assert slept == [1.0] * (HALT_CONFIRMATIONS - 1)
    assert led.calls == [CRITICAL_LED]


def test_a_boot_read_that_fails_once_then_succeeds_still_records():
    led = FakeLedDriver()
    reader = FailsFirstReads(failures=1)

    result, _ = _startup(reader, led)

    assert result.proceed is True
    assert result.battery_pct == 85
    assert led.calls == [RECORDING_LED]


def test_a_charging_pendant_never_halts_at_boot():
    # Plugged in at 3 %: it is charging, so it has to come up and record.
    led = FakeLedDriver()
    reader = FakeTelemetryBatteryReader(pct=3, voltage_mv=3200, external_power=True)

    result, _ = _startup(reader, led)

    assert result.proceed is True
    assert result.battery_pct == 3
    assert led.calls == [LOW_BATTERY_LED]


def test_the_boot_check_stops_sampling_as_soon_as_a_halt_is_confirmed():
    # A genuinely flat pendant is still caught in about the time the single
    # sample used to take, rather than always paying for the full window.
    led = FakeLedDriver()

    result, slept = _startup(
        FakeTelemetryBatteryReader(pct=5),
        led,
        samples=6,
        monitor=BatteryMonitor(halt_confirmations=2),
    )

    assert result.proceed is False
    assert slept == [1.0]


def test_the_boot_samples_seed_the_loop_s_moving_average():
    # The loop inherits a warm window instead of starting blind, so its first
    # iteration is already debounced against three real readings.
    led = FakeLedDriver()
    reader = FakeTelemetryBatteryReader(pct=85)

    result, _ = _startup(reader, led)
    reader.pct = 5
    assert result.battery is not None

    # Two more readings fold into a window that already holds three 85s, so
    # the smoothed value is nowhere near the halt threshold.
    result.battery.sample()
    assert result.battery.sample().should_halt is False


def test_loop_reports_recording_active_before_the_first_segment(tmp_path):
    # Without this the app keeps whatever the last run left (after a halt or
    # stop: Paused) for a whole segment after recording has resumed.
    stop = threading.Event()
    status = FakeStatusClient()
    upserts_before_first_capture: list[int] = []

    def on_record(count: int) -> None:
        if count == 1:
            upserts_before_first_capture.append(len(status.upserts))
        stop.set()

    deps = _make_deps(
        tmp_path,
        command_runner=FakeCommandRunner(on_record=on_record),
        battery=battery_state(85),
        led_driver=FakeLedDriver(),
        status_client=status,
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0)))

    assert upserts_before_first_capture == [1]
    assert status.upserts[0]["recording_active"] is True
    assert status.upserts[0]["segments_uploaded_today"] == 0


def test_stop_reports_recording_inactive_as_the_final_status(tmp_path):
    # The app renders Paused off recording_active=False, but nothing ever sent
    # it: after the daemon stopped, the Device tab said Recording forever.
    stop = threading.Event()
    status = FakeStatusClient()
    deps = _make_deps(
        tmp_path,
        command_runner=FakeCommandRunner(
            on_record=lambda count: stop.set() if count >= 2 else None
        ),
        battery=battery_state(85),
        led_driver=FakeLedDriver(),
        status_client=status,
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0)))

    final = status.upserts[-1]
    assert final["recording_active"] is False
    assert final["segments_uploaded_today"] == 2
    assert final["segments_pending"] == 0
    assert final["battery_pct"] == 85
    assert [u["recording_active"] for u in status.upserts] == [
        True,
        True,
        True,
        False,
    ]


def test_battery_halt_reports_recording_inactive_after_the_drain(tmp_path):
    stop = threading.Event()
    status = FakeStatusClient()
    led = FakeLedDriver()
    battery = TriggerableBatteryReader(healthy_pct=85, halt_pct=5)
    deps = _make_deps(
        tmp_path,
        command_runner=FakeCommandRunner(
            on_record=lambda count: battery.trigger_halt() if count >= 1 else None
        ),
        battery=responsive_battery(battery),
        led_driver=led,
        status_client=status,
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0)))

    assert status.upserts[-1]["recording_active"] is False
    assert status.upserts[-1]["battery_pct"] == 5
    assert led.calls[-1] == CRITICAL_LED


def test_a_confirmed_voltage_floor_halts_the_loop_the_same_way_a_flat_one_does(
    tmp_path,
):
    # The gauge still reads 55 %; only the rail has collapsed. That is the
    # case the percentage alone cannot catch, and it has to end in the same
    # drain, the same red, and the same power-off.
    stop = threading.Event()
    led = FakeLedDriver()
    power = FakePowerController()
    battery = FakeTelemetryBatteryReader(pct=55, voltage_mv=3900)
    deps = _make_deps(
        tmp_path,
        command_runner=FakeCommandRunner(
            on_record=lambda count: setattr(battery, "voltage_mv", 3100)
        ),
        battery=responsive_battery(battery),
        led_driver=led,
        power_controller=power,
    )

    outcome = run_recording_loop(
        deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0))
    )

    assert outcome is LoopOutcome.HALTED
    assert led.calls[-1] == CRITICAL_LED
    assert power.power_offs == [WAKE_ON_CHARGE_PCT]


def test_a_charging_pendant_keeps_recording_through_a_flat_gauge(tmp_path):
    # Decision 4, end to end: 2 % and 3.0 V, but plugged in, so the loop runs
    # until something else stops it and nothing powers the device off.
    stop = threading.Event()
    power = FakePowerController()
    battery = FakeTelemetryBatteryReader(pct=2, voltage_mv=3000, external_power=True)
    runner = FakeCommandRunner(on_record=lambda n: stop.set() if n >= 3 else None)
    deps = _make_deps(
        tmp_path,
        command_runner=runner,
        battery=responsive_battery(battery),
        led_driver=FakeLedDriver(),
        power_controller=power,
    )

    outcome = run_recording_loop(
        deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0))
    )

    assert outcome is LoopOutcome.STOPPED
    assert runner.record_count == 3
    assert power.power_offs == []


def test_charge_returning_during_the_drain_cancels_the_power_off(tmp_path):
    # Plugging a pendant in while its queue is draining keeps it running
    # rather than powering it off and waiting for the charge wake. The loop
    # reports RECOVERED so the daemon re-enters it.
    stop = threading.Event()
    led = FakeLedDriver()
    power = FakePowerController()
    battery = FakeTelemetryBatteryReader(pct=85, voltage_mv=3900)

    class PlugInWhileDraining(FakeStorageClient):
        def upload(self, bucket, object_path, local_path):
            super().upload(bucket, object_path, local_path)
            battery.pct = 30
            battery.external_power = True

    deps = _make_deps(
        tmp_path,
        # One segment recorded, then the pack reads flat: the halt is
        # declared with that segment still queued, so the charger lands
        # while the drain is in flight.
        command_runner=FakeCommandRunner(
            on_record=lambda count: setattr(battery, "pct", 5)
        ),
        battery=responsive_battery(battery),
        led_driver=led,
        storage_client=PlugInWhileDraining(),
        power_controller=power,
    )

    outcome = run_recording_loop(
        deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0))
    )

    assert outcome is LoopOutcome.RECOVERED
    assert power.power_offs == []
    # The red is shown while the halt is still a halt, then given up for the
    # live state once it is cancelled - a resuming pendant must not sit red.
    assert CRITICAL_LED in led.calls
    assert led.calls[-1] == RECORDING_LED


def test_a_cancelled_halt_resumes_with_the_upload_critical_latch_still_red(tmp_path):
    # Uploads were already failing when the halt was declared; plugging in
    # cancels the halt, but must not quietly clear the red the failures earned.
    stop = threading.Event()
    led = FakeLedDriver()
    battery = FakeTelemetryBatteryReader(pct=85, voltage_mv=3900)

    class PlugInWhileDrainingAndDown(FailingStorageClient):
        def upload(self, bucket, object_path, local_path):
            battery.pct = 30
            battery.external_power = True
            super().upload(bucket, object_path, local_path)

    deps = _make_deps(
        tmp_path,
        command_runner=FakeCommandRunner(
            on_record=lambda count: setattr(battery, "pct", 5)
        ),
        battery=responsive_battery(battery),
        led_driver=led,
        storage_client=PlugInWhileDrainingAndDown(),
    )
    deps.upload_health.consecutive_failures = 3

    outcome = run_recording_loop(
        deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0))
    )

    assert outcome is LoopOutcome.RECOVERED
    assert led.calls[-1] == CRITICAL_LED


def test_a_re_entered_loop_keeps_the_day_count_and_the_failure_streak(tmp_path):
    # The daemon re-enters the loop with the same deps after a cancelled halt;
    # a fresh worker must not reset device_status.segments_uploaded_today to 0
    # or forget how many uploads have failed in a row.
    stop = threading.Event()
    status = FakeStatusClient()
    deps = _make_deps(
        tmp_path,
        command_runner=FakeCommandRunner(
            on_record=lambda n: stop.set() if n >= 2 else None
        ),
        battery=battery_state(85),
        led_driver=FakeLedDriver(),
        status_client=status,
    )
    clock = FakeClock(datetime(2026, 7, 4, 12, 0, 0))
    assert run_recording_loop(deps, stop, clock=clock) is LoopOutcome.STOPPED
    assert deps.segments_uploaded_today == 2

    status.upserts.clear()
    stop.clear()
    led = FakeLedDriver()
    deps.led_driver = led
    deps.storage_client = FailingStorageClient()
    deps.command_runner = FakeCommandRunner(
        on_record=lambda n: stop.set() if n >= 3 else None
    )
    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 13, 0, 0)))
    first_run_failures = deps.upload_health.consecutive_failures

    assert status.upserts[0]["segments_uploaded_today"] == 2
    assert status.upserts[-1]["segments_uploaded_today"] == 2
    assert first_run_failures == 3

    stop.clear()
    led = FakeLedDriver()
    deps.led_driver = led
    deps.command_runner = FakeCommandRunner(
        on_record=lambda n: stop.set() if n >= 1 else None
    )
    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 14, 0, 0)))

    # The streak carried in, so this run's lone failure is shown red from its
    # upload onward rather than starting the count again from zero.
    assert UPLOADING_LED not in led.calls
    assert CRITICAL_LED in led.calls


def test_a_failed_reading_after_the_drain_still_powers_off(tmp_path):
    # The recovery re-read is the last thing between a confirmed halt and the
    # power-off. An I2C fault there must not crash the daemon into a restart
    # loop that never powers off and browns out instead.
    stop = threading.Event()
    power = FakePowerController()

    class FailsAfterTheHalt(FakeTelemetryBatteryReader):
        # Reads: the seed, the sample before the first segment, the sample
        # that confirms the halt, then the recovery re-read.
        def __init__(self):
            super().__init__(pct=85)
            self.reads = 0

        def get_charge_pct(self):
            self.reads += 1
            if self.reads >= 4:
                raise RuntimeError("pijuice GetChargeLevel failed: COMM_ERROR")
            return 5 if self.reads == 3 else 85

    deps = _make_deps(
        tmp_path,
        command_runner=FakeCommandRunner(),
        battery=responsive_battery(FailsAfterTheHalt()),
        led_driver=FakeLedDriver(),
        power_controller=power,
    )

    outcome = run_recording_loop(
        deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0))
    )

    assert outcome is LoopOutcome.HALTED
    assert power.power_offs == [WAKE_ON_CHARGE_PCT]


def test_a_gauge_that_stays_unreadable_halts_and_powers_off(tmp_path):
    # A permanent I2C fault mid-run: without a bound the loop would keep
    # recording on the last good verdict until the cell browned out.
    stop = threading.Event()
    led = FakeLedDriver()
    power = FakePowerController()
    status = FakeStatusClient()

    class DiesAfterTwoReads(FakeTelemetryBatteryReader):
        def __init__(self):
            super().__init__(pct=60)
            self.reads = 0

        def get_charge_pct(self):
            self.reads += 1
            if self.reads > 2:
                raise RuntimeError("pijuice GetChargeLevel failed: COMM_ERROR")
            return super().get_charge_pct()

    runner = FakeCommandRunner()
    deps = _make_deps(
        tmp_path,
        command_runner=runner,
        battery=BatteryState(DiesAfterTwoReads()),
        led_driver=led,
        status_client=status,
        power_controller=power,
    )

    outcome = run_recording_loop(
        deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0))
    )

    assert outcome is LoopOutcome.HALTED
    # One segment on the good read, then one per failed read short of the
    # confirmation count.
    assert runner.record_count == HALT_CONFIRMATIONS
    assert status.upserts[-1]["recording_active"] is False
    assert led.calls[-1] == CRITICAL_LED
    assert power.power_offs == [WAKE_ON_CHARGE_PCT]


def test_a_wedged_upload_cannot_hold_a_dying_battery_open_forever(tmp_path):
    # The bounded drain. Without it the loop waits on an upload that will
    # never return while the pack empties, and the Pi browns out mid-write
    # instead of powering off.
    stop = threading.Event()
    power = FakePowerController()
    battery = FakeTelemetryBatteryReader(pct=85, voltage_mv=3900)
    release = threading.Event()
    wedged = threading.Event()

    class WedgedStorageClient:
        def upload(self, bucket, object_path, local_path):
            wedged.set()
            assert release.wait(timeout=10), "the test never released the upload"

    def on_record(count: int) -> None:
        # Declare the battery flat only once an upload is provably stuck, so
        # the halt always meets a drain that cannot finish.
        if count == 1:
            return
        assert wedged.wait(timeout=5), "the worker never reached the upload"
        battery.pct = 5

    deps = _make_deps(
        tmp_path,
        command_runner=FakeCommandRunner(on_record=on_record),
        battery=responsive_battery(battery),
        led_driver=FakeLedDriver(),
        storage_client=WedgedStorageClient(),
        power_controller=power,
        upload_drain_timeout_sec=0.2,
    )

    try:
        outcome = run_recording_loop(
            deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0))
        )

        assert outcome is LoopOutcome.HALTED
        assert power.power_offs == [WAKE_ON_CHARGE_PCT]
    finally:
        release.set()


def test_capture_failure_reports_recording_inactive_and_still_raises(tmp_path):
    class FailingCapture(FakeCommandRunner):
        def run(self, args: list[str]) -> None:
            if args[0] == "rpicam-vid":
                raise RuntimeError("camera not detected")
            super().run(args)

    status = FakeStatusClient()
    deps = _make_deps(
        tmp_path,
        command_runner=FailingCapture(),
        battery=battery_state(85),
        led_driver=FakeLedDriver(),
        status_client=status,
    )

    with pytest.raises(RuntimeError, match="camera not detected"):
        run_recording_loop(deps, threading.Event())

    assert status.upserts[-1]["recording_active"] is False


def test_status_push_failures_at_start_and_exit_never_stop_the_daemon(tmp_path):
    # Shutdown often happens with the network already gone; the final push
    # must not turn a clean stop into a crash, and a failed first push must
    # not keep the device from recording.
    class AlwaysFailingStatus:
        def upsert_device_status(self, status: dict) -> None:
            raise RuntimeError("network down")

    stop = threading.Event()
    storage = FakeStorageClient()
    deps = _make_deps(
        tmp_path,
        command_runner=FakeCommandRunner(on_record=lambda count: stop.set()),
        battery=battery_state(85),
        led_driver=FakeLedDriver(),
        storage_client=storage,
        status_client=AlwaysFailingStatus(),
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0)))

    assert storage.uploaded == ["device-abc/20260704_120000.mp4"]


_ENV = {"SUPABASE_URL": "https://example.supabase.co", "SUPABASE_ANON_KEY": "anon"}


def test_load_config_turns_audio_on_by_default_with_the_visio_mic_device():
    assert load_config(_ENV).audio == AudioSettings(
        device="visio_mic", bitrate_bps=64_000, samplerate=48_000
    )


def test_load_config_reads_the_audio_device_and_bitrate():
    config = load_config(
        {**_ENV, "VISIO_AUDIO_DEVICE": "plughw:CARD=adau7002,DEV=0",
         "VISIO_AUDIO_BITRATE": "96000"}
    )  # fmt: skip
    assert config.audio == AudioSettings(
        device="plughw:CARD=adau7002,DEV=0", bitrate_bps=96_000
    )


def test_load_config_can_turn_audio_off():
    assert load_config({**_ENV, "VISIO_AUDIO": "off"}).audio is None


def test_load_config_rejects_an_unknown_audio_mode():
    with pytest.raises(ValueError, match="VISIO_AUDIO"):
        load_config({**_ENV, "VISIO_AUDIO": "maybe"})


def test_default_segment_length_does_not_depend_on_whether_audio_is_on():
    on = load_config(_ENV).segment_duration_ms
    off = load_config({**_ENV, "VISIO_AUDIO": "off"}).segment_duration_ms
    assert on == off == 60_000


def test_a_high_audio_bitrate_shortens_the_default_segment():
    config = load_config({**_ENV, "VISIO_AUDIO_BITRATE": "2000000"})
    assert config.segment_duration_ms == 40_000


class _ProbeRunner:
    def __init__(self, device_opens: bool) -> None:
        self.device_opens = device_opens
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> None:
        self.calls.append(args)
        if not self.device_opens:
            raise subprocess.CalledProcessError(251, args)


def test_resolve_audio_keeps_a_microphone_that_opens():
    audio = AudioSettings()
    assert resolve_audio(_ProbeRunner(device_opens=True), audio) == audio


def test_resolve_audio_falls_back_to_video_only_with_a_warning(caplog):
    assert resolve_audio(_ProbeRunner(device_opens=False), AudioSettings()) is None
    assert "recording video only" in caplog.text


def test_resolve_audio_does_not_probe_when_audio_is_off():
    runner = _ProbeRunner(device_opens=True)
    assert resolve_audio(runner, None) is None
    assert runner.calls == []


def test_loop_records_audio_segments_and_reports_audio_active(tmp_path):
    stop = threading.Event()
    storage = FakeStorageClient()
    status = FakeStatusClient()
    runner = FakeCommandRunner(on_record=lambda n: stop.set() if n >= 2 else None)
    deps = _make_deps(
        tmp_path,
        command_runner=runner,
        battery=battery_state(85),
        led_driver=FakeLedDriver(),
        storage_client=storage,
        status_client=status,
        audio=AudioSettings(),
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 11, 16, 9, 0, 0)))

    captures = [c for c in runner.calls if c[0] == "rpicam-vid"]
    assert all(c[c.index("-o") + 1].endswith(".ts") for c in captures)
    assert storage.uploaded == [
        "device-abc/20261116_090000.mp4",
        "device-abc/20261116_090001.mp4",
    ]
    assert [u["audio_active"] for u in status.upserts] == [True, True, True, True]


def test_a_failing_audio_capture_falls_back_to_video_only_and_keeps_recording(
    tmp_path,
):
    # The microphone disappears mid-run: rpicam-vid's libav capture exits
    # non-zero. Recording must carry on video only instead of stopping.
    stop = threading.Event()
    storage = FakeStorageClient()
    status = FakeStatusClient()

    class MicGoneOnSecondCapture(FakeCommandRunner):
        def run(self, args: list[str]) -> None:
            mic_gone = "--libav-audio" in args and self.record_count == 1
            if args[0] == "rpicam-vid" and mic_gone:
                with self._lock:
                    self.calls.append(args)
                raise subprocess.CalledProcessError(1, args)
            super().run(args)

    runner = MicGoneOnSecondCapture(on_record=lambda n: stop.set() if n >= 3 else None)
    deps = _make_deps(
        tmp_path,
        command_runner=runner,
        battery=battery_state(85),
        led_driver=FakeLedDriver(),
        storage_client=storage,
        status_client=status,
        audio=AudioSettings(),
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 11, 16, 9, 0, 0)))

    outputs = [
        Path(c[c.index("-o") + 1]).name for c in runner.calls if c[0] == "rpicam-vid"
    ]
    assert outputs == [
        "20261116_090000.ts",
        "20261116_090001.ts",
        "20261116_090002.h264",
        "20261116_090003.h264",
    ]
    assert storage.uploaded == [
        "device-abc/20261116_090000.mp4",
        "device-abc/20261116_090002.mp4",
        "device-abc/20261116_090003.mp4",
    ]
    per_segment = [u["audio_active"] for u in status.upserts[1:-1]]
    assert per_segment == [True, False, False]
    assert status.upserts[-1]["audio_active"] is False


@pytest.mark.parametrize(
    ("partial_bytes", "uploaded_partial"), [(b"ts-data", True), (b"", False)]
)
def test_a_failed_audio_capture_uploads_its_partial_ts_the_same_run(
    tmp_path, partial_bytes, uploaded_partial
):
    # The capture dies mid-segment after writing part of its MPEG-TS. That
    # footage must upload now, not wait for the next boot's orphan recovery.
    stop = threading.Event()
    storage = FakeStorageClient()

    class DiesMidSegment(FakeCommandRunner):
        def run(self, args: list[str]) -> None:
            if args[0] == "rpicam-vid" and "--libav-audio" in args:
                Path(args[args.index("-o") + 1]).write_bytes(partial_bytes)
                raise subprocess.CalledProcessError(1, args)
            super().run(args)

    runner = DiesMidSegment(on_record=lambda n: stop.set() if n >= 1 else None)
    deps = _make_deps(
        tmp_path,
        command_runner=runner,
        battery=battery_state(85),
        led_driver=FakeLedDriver(),
        storage_client=storage,
        audio=AudioSettings(),
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 11, 16, 9, 0, 0)))

    partial_upload = ["device-abc/20261116_090000.mp4"] if uploaded_partial else []
    assert storage.uploaded == [*partial_upload, "device-abc/20261116_090001.mp4"]
    assert list(deps.data_dir.iterdir()) == []


def test_the_capture_after_a_failed_audio_one_waits_for_a_new_second(tmp_path):
    # The audio capture writes some TS and dies within the second it started.
    # The video-only retry must not reuse that second's filename, or both
    # segments would mux and upload to the same object key.
    stop = threading.Event()
    storage = FakeStorageClient()
    slept: list[float] = []

    class DiesAtOnce(FakeCommandRunner):
        def run(self, args: list[str]) -> None:
            if args[0] == "rpicam-vid" and "--libav-audio" in args:
                Path(args[args.index("-o") + 1]).write_bytes(b"ts-data")
                raise subprocess.CalledProcessError(1, args)
            super().run(args)

    deps = _make_deps(
        tmp_path,
        command_runner=DiesAtOnce(on_record=lambda n: stop.set()),
        battery=battery_state(85),
        led_driver=FakeLedDriver(),
        storage_client=storage,
        audio=AudioSettings(),
    )

    run_recording_loop(
        deps,
        stop,
        clock=lambda: datetime(2026, 11, 16, 9, 0, 0, 250000),
        sleep=slept.append,
    )

    assert slept == [0.75]
    assert storage.uploaded == [
        "device-abc/20261116_090000.mp4",
        "device-abc/20261116_090001.mp4",
    ]


def test_a_clock_stepping_back_after_the_fallback_never_stalls_recording(tmp_path):
    # Only the capture right after the failure may wait, and never for long:
    # a later wall-clock step back (DST fall-back, an NTP step) must not make
    # the loop sleep until the failure time comes round again.
    stop = threading.Event()
    storage = FakeStorageClient()
    slept: list[float] = []
    readings = iter(
        [
            datetime(2026, 11, 16, 14, 0, 5),
            datetime(2026, 11, 16, 14, 0, 6),
            datetime(2026, 11, 16, 1, 0, 0),
        ]
    )

    class DiesOnce(FakeCommandRunner):
        def run(self, args: list[str]) -> None:
            if args[0] == "rpicam-vid" and "--libav-audio" in args:
                Path(args[args.index("-o") + 1]).write_bytes(b"ts-data")
                raise subprocess.CalledProcessError(1, args)
            super().run(args)

    deps = _make_deps(
        tmp_path,
        command_runner=DiesOnce(on_record=lambda n: stop.set() if n >= 2 else None),
        battery=battery_state(85),
        led_driver=FakeLedDriver(),
        storage_client=storage,
        audio=AudioSettings(),
    )

    run_recording_loop(deps, stop, clock=lambda: next(readings), sleep=slept.append)

    assert slept == []
    assert storage.uploaded == [
        "device-abc/20261116_140005.mp4",
        "device-abc/20261116_140006.mp4",
        "device-abc/20261116_010000.mp4",
    ]


def test_a_failing_video_only_capture_still_escapes_the_loop(tmp_path):
    class Killed(FakeCommandRunner):
        def run(self, args: list[str]) -> None:
            if args[0] == "rpicam-vid":
                raise subprocess.CalledProcessError(-15, args)
            super().run(args)

    deps = _make_deps(
        tmp_path,
        command_runner=Killed(),
        battery=battery_state(85),
        led_driver=FakeLedDriver(),
    )

    with pytest.raises(subprocess.CalledProcessError):
        run_recording_loop(
            deps, threading.Event(), clock=FakeClock(datetime(2026, 11, 16, 9))
        )


def test_startup_and_loop_drive_the_spi_led_through_every_colour(tmp_path):
    # The real led.py and the real SPI driver, with only /dev/spidev0.0
    # simulated: the colours a WS2812B on GPIO 10 would latch, decoded from the
    # wire bytes at the clock the SPI block really produces.
    #
    # The battery is healthy for the whole run. It used to go low part way
    # through, triggered from inside the worker's own upload so that the
    # worker's own battery read would see it - but the worker no longer reads
    # the battery at all. It reads the verdict the main loop published, and
    # with an instant fake capture the loop runs several segments ahead of the
    # worker, so which segment first sees a mid-run change is a race. The
    # low-battery colours are pinned in their own test below, where the
    # battery is low from the start and there is no ordering question.
    spi = SimulatedSpiDevice()
    led = Ws2812LedDriver(spi=spi)
    battery = FakeTelemetryBatteryReader(pct=85)
    storage = ScriptedStorageClient(fail_on_attempts={2, 3, 4})
    stop = threading.Event()
    runner = FakeCommandRunner(on_record=lambda n: stop.set() if n >= 6 else None)

    assert _startup(battery, led)[0].proceed
    deps = _make_deps(
        tmp_path,
        command_runner=runner,
        battery=battery_state(85),
        led_driver=led,
        storage_client=storage,
    )
    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0)))

    green, blue, red = (0, 255, 0), (0, 0, 255), (255, 0, 0)
    colours = [decode_frame(frame, 6_250_000)[0] for frame in spi.writes]
    assert spi.overlapping_writes == 0
    upload = [blue, green]
    assert colours == (
        [green]  # startup, healthy battery
        + upload  # segment 1 uploads
        + upload  # 2 fails
        + upload  # 3 fails
        + [blue, red]  # 4 fails: third in a row
        + [red, green]  # 5 uploads: red held until the success clears it
        + upload  # 6 uploads normally again
    )


def test_a_low_battery_paints_startup_and_every_upload_yellow_on_the_wire(tmp_path):
    # The fourth colour, decoded off the same simulated wire: a pendant that
    # is already low at boot shows yellow at startup and keeps showing it
    # through each segment's upload, instead of the blue and green a healthy
    # one would.
    spi = SimulatedSpiDevice()
    led = Ws2812LedDriver(spi=spi)
    battery = FakeTelemetryBatteryReader(pct=15)
    stop = threading.Event()
    runner = FakeCommandRunner(on_record=lambda n: stop.set() if n >= 2 else None)

    assert _startup(battery, led)[0].proceed
    deps = _make_deps(
        tmp_path,
        command_runner=runner,
        battery=responsive_battery(battery),
        led_driver=led,
        storage_client=FakeStorageClient(),
    )
    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0)))

    yellow = (255, 255, 0)
    colours = [decode_frame(frame, 6_250_000)[0] for frame in spi.writes]
    assert spi.overlapping_writes == 0
    assert colours == [yellow] * 5


def test_critical_red_survives_the_next_segments_upload_colours(tmp_path):
    # Regression test for the Critical LED being painted over: the worker
    # applied CRITICAL after the third consecutive upload failure, but the
    # next segment's on_segment_complete then wrote UPLOADING blue and
    # restored RECORDING green before the worker re-applied red, so a
    # pendant in a sustained outage showed blue/green for most of every
    # segment instead of the red the spec promises. The spec's clear rule
    # for Critical is "any subsequent successful upload resets the counter"
    # (docs/superpowers/specs/2026-07-04-visio-pendant-design.md), so the
    # decoded waveform must stay red from the escalation until segment 5
    # succeeds.
    spi = SimulatedSpiDevice()
    led = Ws2812LedDriver(spi=spi)
    # Segments 1-4 fail (CRITICAL from 3 on), segment 5 succeeds and clears it.
    storage = ScriptedStorageClient(fail_on_attempts={1, 2, 3, 4})
    stop = threading.Event()
    runner = FakeCommandRunner(on_record=lambda n: stop.set() if n >= 5 else None)
    deps = _make_deps(
        tmp_path,
        command_runner=runner,
        battery=battery_state(85),
        led_driver=led,
        storage_client=storage,
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0)))

    green, blue, red = (0, 255, 0), (0, 0, 255), (255, 0, 0)
    colours = [decode_frame(frame, 6_250_000)[0] for frame in spi.writes]
    assert runner.record_count == 5
    assert storage.uploaded == ["device-abc/20260704_120004.mp4"]
    # Nothing but red between the escalation and the success that clears it.
    first_red = colours.index(red)
    clearing_green = len(colours) - 1
    assert colours[clearing_green] == green
    assert set(colours[first_red:clearing_green]) == {red}
    # Spelled out in full: once Critical, even the upload colour is red.
    assert colours == (
        [blue, green]  # 1: fail (1 consecutive)
        + [blue, green]  # 2: fail (2)
        + [blue, red]  # 3: fail (3 -> Critical)
        + [red, red]  # 4: fail (4 -> stays Critical, no blue, no green)
        + [red, green]  # 5: success resets the counter and clears Critical
    )


def test_a_failed_segment_is_retried_in_process_without_a_restart(tmp_path):
    # Reproduces the gap item 3 closes: a segment whose upload failed used to
    # sit in the queue for the rest of the run, because on_segment_complete
    # only ever uploads its own segment and flush_pending is called once, at
    # daemon start. A pendant that lost WiFi for a minute therefore held that
    # footage until someone power-cycled it.
    queue_dir = tmp_path / "queue"

    class FailFirstThenSucceed(FakeStorageClient):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0
            self.retry_succeeded = threading.Event()

        def upload(self, bucket, object_path, local_path):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("network down")
            super().upload(bucket, object_path, local_path)
            self.retry_succeeded.set()

    storage = FailFirstThenSucceed()
    stop = threading.Event()

    def on_record(count: int) -> None:
        # Segment 1 is already queued and failed by now, and the main thread
        # parks here, so the worker is idle: the only thing that can clear the
        # queue is its own in-process retry.
        if count >= 2:
            assert storage.retry_succeeded.wait(timeout=5), (
                "the upload worker never retried the failed segment in process"
            )
            stop.set()

    deps = _make_deps(
        tmp_path,
        command_runner=FakeCommandRunner(on_record=on_record),
        battery=battery_state(85),
        led_driver=FakeLedDriver(),
        storage_client=storage,
        retry_backoff=RetryBackoff(base_sec=0.01, max_sec=0.04),
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0)))

    # Segment 1 reached storage on the retry, before segment 2 was handled.
    assert storage.uploaded == [
        "device-abc/20260704_120000.mp4",
        "device-abc/20260704_120001.mp4",
    ]
    assert list_pending(queue_dir) == []


def test_a_retry_round_still_runs_while_segments_arrive_faster_than_the_backoff(
    tmp_path,
):
    # The production schedule (30 s doubling to 480 s) against the production
    # segment period (60 s), on a fake monotonic clock: from the second
    # consecutive failure on, every backoff delay is at least one segment
    # period, so a wait that restarted on each new segment would never elapse.
    now = [0.0]
    first = "device-abc/20260704_120000.mp4"
    attempted = threading.Condition()
    stop = threading.Event()

    class AlwaysDown(FailingStorageClient):
        def __init__(self) -> None:
            self.attempts: list[str] = []

        def upload(self, bucket, object_path, local_path):
            with attempted:
                self.attempts.append(object_path)
                attempted.notify_all()
            raise RuntimeError("network down")

    storage = AlwaysDown()

    def on_record(count: int) -> None:
        # Keep the fake clock in step with the worker: segment count - 1 has
        # had its own upload attempt before one more segment period passes.
        with attempted:
            assert attempted.wait_for(
                lambda: len(set(storage.attempts)) >= count - 1, timeout=5
            )
            if storage.attempts.count(first) >= 3 or count >= 40:
                stop.set()
        now[0] += 60.0

    deps = _make_deps(
        tmp_path,
        command_runner=FakeCommandRunner(on_record=on_record),
        battery=battery_state(85),
        led_driver=FakeLedDriver(),
        storage_client=storage,
        retry_backoff=RetryBackoff(),
        monotonic=lambda: now[0],
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0)))

    # Its own upload, then two retry rounds while recording carried on - the
    # second one after far more than two consecutive failures had accrued.
    assert storage.attempts.count(first) >= 3


def test_a_stop_with_a_lapsed_retry_deadline_leaves_the_critical_led_red(tmp_path):
    # Three failed segments latch Critical. The third upload is still timing
    # out when the stop arrives, long enough for the retry deadline to lapse.
    # Whether the worker next starts a real (failing) round or, once stopping,
    # skips straight to the shutdown sentinel, nothing reached storage, so the
    # red must still be in force when the loop returns. A round cancelled
    # before its first file must not read as a success and turn it green.
    spi = SimulatedSpiDevice()
    led = Ws2812LedDriver(spi=spi)
    red = (255, 0, 0)
    now = [0.0]
    stop = threading.Event()

    class SlowAndDown(FailingStorageClient):
        def __init__(self) -> None:
            self.attempts = 0

        def upload(self, bucket, object_path, local_path):
            self.attempts += 1
            if self.attempts == 3:
                assert stop.wait(timeout=5)
                now[0] += 3600.0
            raise RuntimeError("network down")

    def on_record(count: int) -> None:
        if count >= 3:
            stop.set()

    storage = SlowAndDown()
    deps = _make_deps(
        tmp_path,
        command_runner=FakeCommandRunner(on_record=on_record),
        battery=battery_state(85),
        led_driver=led,
        storage_client=storage,
        retry_backoff=RetryBackoff(),
        monotonic=lambda: now[0],
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0)))

    colours = [decode_frame(frame, 6_250_000)[0] for frame in spi.writes]
    assert storage.attempts >= 3
    assert colours[-1] == red
    assert set(colours[colours.index(red) :]) == {red}


@dataclass(frozen=True)
class _RecordingBackoff(RetryBackoff):
    """A real backoff that also records the failure count it was asked about.

    Still a ``RetryBackoff``, so the loop is exercised through the type it
    really takes; the list is mutated, not the frozen field.
    """

    asked: list[int] = field(default_factory=list)

    def delay_sec(self, consecutive_failures: int) -> float:
        self.asked.append(consecutive_failures)
        return super().delay_sec(consecutive_failures)


def test_the_worker_waits_longer_after_each_failed_retry_round(tmp_path):
    # Bounded, increasing waits, proved through the loop rather than the
    # dataclass: each round feeds the worker's live consecutive-failure count
    # into the backoff, which is the thing that makes the waits grow.
    backoff = _RecordingBackoff(base_sec=0.01, max_sec=0.03)
    rounds_done = threading.Event()
    stop = threading.Event()

    def on_record(count: int) -> None:
        if count >= 2:
            assert rounds_done.wait(timeout=5), "the worker never retried repeatedly"
            stop.set()

    class CountingFailure(FailingStorageClient):
        def __init__(self) -> None:
            self.attempts = 0

        def upload(self, bucket, object_path, local_path):
            self.attempts += 1
            if self.attempts >= 5:
                rounds_done.set()
            raise RuntimeError("network down")

    storage = CountingFailure()
    deps = _make_deps(
        tmp_path,
        command_runner=FakeCommandRunner(on_record=on_record),
        battery=battery_state(85),
        led_driver=FakeLedDriver(),
        storage_client=storage,
        retry_backoff=backoff,
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0)))

    # Segment 1's own upload is attempt 1; attempts 2+ are retry rounds, each
    # asked about one more consecutive failure than the last.
    assert storage.attempts >= 5
    assert backoff.asked[:4] == [1, 2, 3, 4]
    assert backoff.asked == sorted(backoff.asked)
    assert [backoff.delay_sec(n) for n in backoff.asked[:4]] == [0.01, 0.02, 0.03, 0.03]


def test_retry_rounds_escalate_to_the_critical_led_without_a_new_segment(tmp_path):
    # One segment, then nothing but retries: the third consecutive failure is
    # a retry round's, not a segment's, and it must still turn the LED red.
    spi = SimulatedSpiDevice()
    led = Ws2812LedDriver(spi=spi)
    red = (255, 0, 0)
    went_red = threading.Event()
    stop = threading.Event()

    class FailUntilRed(FailingStorageClient):
        def upload(self, bucket, object_path, local_path):
            if any(decode_frame(f, 6_250_000)[0] == red for f in spi.writes):
                went_red.set()
            raise RuntimeError("network down")

    def on_record(count: int) -> None:
        if count >= 2:
            assert went_red.wait(timeout=5), "retry failures never escalated to red"
            stop.set()

    deps = _make_deps(
        tmp_path,
        command_runner=FakeCommandRunner(on_record=on_record),
        battery=battery_state(85),
        led_driver=led,
        storage_client=FailUntilRed(),
        retry_backoff=RetryBackoff(base_sec=0.01, max_sec=0.02),
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0)))

    colours = [decode_frame(frame, 6_250_000)[0] for frame in spi.writes]
    assert red in colours
    # Only one segment ever uploaded, so the escalation came from retries.
    first_red = colours.index(red)
    assert first_red >= 2
    # And it never goes back to a normal colour while every retry fails.
    assert set(colours[first_red:]) == {red}


def test_a_queue_left_by_the_startup_flush_is_drained_without_a_restart(tmp_path):
    # flush_pending at daemon start can leave files behind (it ran while the
    # network was still down). With no failures of its own the worker must
    # still pick them up rather than wait for the next boot.
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir(parents=True)
    (queue_dir / "20260703_235900.mp4").write_bytes(b"yesterday")
    drained = threading.Event()

    class NoticingStorage(FakeStorageClient):
        def upload(self, bucket, object_path, local_path):
            super().upload(bucket, object_path, local_path)
            if object_path.endswith("20260703_235900.mp4"):
                drained.set()

    storage = NoticingStorage()
    stop = threading.Event()

    def on_record(count: int) -> None:
        if count >= 2:
            assert drained.wait(timeout=5), "the inherited queue was never drained"
            stop.set()

    deps = _make_deps(
        tmp_path,
        command_runner=FakeCommandRunner(on_record=on_record),
        battery=battery_state(85),
        led_driver=FakeLedDriver(),
        storage_client=storage,
        retry_backoff=RetryBackoff(base_sec=0.01, max_sec=0.02),
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0)))

    assert "device-abc/20260703_235900.mp4" in storage.uploaded
    assert list_pending(queue_dir) == []


# --- choosing how this device powers itself off ------------------------------


def test_a_pijuice_device_powers_off_through_the_hat():
    pijuice = SimpleNamespace(power=FakePiJuicePower())

    controller = resolve_power_controller(FakeCommandRunner(), pijuice)

    assert isinstance(controller, PiJuicePowerController)


def test_a_device_without_a_pijuice_falls_back_to_systemctl():
    controller = resolve_power_controller(FakeCommandRunner(), None)

    assert isinstance(controller, SystemdPowerController)


# --- an unmetered power source is untouched by all of the above --------------


def test_a_fixed_full_percentage_only_source_never_halts_or_warns(tmp_path):
    # VISIO_BATTERY_SOURCE=none, the USB power bank used while the PiJuice is
    # unavailable: a reader that reports a fixed 100 % and implements no
    # telemetry at all. It must behave exactly as it did before any of this
    # existed - no low-battery latch, no halt, no power-off, and no voltage to
    # compare against a floor.
    stop = threading.Event()
    led = FakeLedDriver()
    power = FakePowerController()
    unmetered = FakeBatteryReader(100)
    runner = FakeCommandRunner(on_record=lambda n: stop.set() if n >= 3 else None)
    deps = _make_deps(
        tmp_path,
        command_runner=runner,
        battery=BatteryState(unmetered),
        led_driver=led,
        power_controller=power,
    )

    outcome = run_recording_loop(
        deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0))
    )

    assert outcome is LoopOutcome.STOPPED
    assert runner.record_count == 3
    assert power.power_offs == []
    assert LOW_BATTERY_LED not in led.calls
    assert CRITICAL_LED not in led.calls


def test_a_fixed_full_percentage_only_source_boots_straight_into_recording():
    led = FakeLedDriver()

    result, _ = _startup(FakeBatteryReader(100), led)

    assert result.proceed is True
    assert result.battery_pct == 100
    assert led.calls == [RECORDING_LED]
