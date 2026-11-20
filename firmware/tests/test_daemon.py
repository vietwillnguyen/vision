import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.fakes import (
    FailingStorageClient,
    FakeBatteryReader,
    FakeCaptureRunner,
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
from visio_recorder.capture import AudioSettings, CaptureExit, VideoSettings
from visio_recorder.daemon import (
    STOP_MARKER_FLUSH_TIMEOUT_SEC,
    LoopDeps,
    ensure_paired,
    handle_revoked_login,
    load_config,
    pair_and_connect,
    resolve_audio,
    resolve_power_controller,
    run_recording_loop,
    run_startup_sequence,
)
from visio_recorder.device_identity import store_paired_device_id
from visio_recorder.drivers import Ws2812LedDriver
from visio_recorder.flag_button import (
    FLAG_UPLOAD_THREAD_NAME,
    FlagUploadWorker,
    make_flag_press_handler,
    write_flag_marker,
)
from visio_recorder.led import LedController, LedPattern, LedState, apply_led_state
from visio_recorder.pairing import (
    MAX_FAILED_PENDING_ACTIVATIONS,
    Pairing,
    PairingRejectedError,
    PairingState,
    PairingUnavailableError,
    pairing_state,
    write_pending_pairing,
)
from visio_recorder.power import (
    PiJuicePowerController,
    SystemdPowerController,
)
from visio_recorder.recording_loop import DiskStats, LoopOutcome, RetryBackoff
from visio_recorder.supabase_clients import SupabaseStorage
from visio_recorder.upload_queue import list_pending
from visio_recorder.wifi_onboard import write_session_credentials

CRITICAL_LED = ((255, 0, 0), LedPattern.FLASHING)
HALTED_LED = ((255, 0, 0), LedPattern.SOLID)
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
    capture_runner=None,
    command_runner=None,
    battery,
    led_driver,
    storage_client=None,
    status_client=None,
    audio=None,
    retry_backoff=None,
    monotonic=None,
    power_controller=None,
    upload_drain_timeout_sec=None,
    flush_markers=None,
    sleep=None,
) -> LoopDeps:
    return LoopDeps(
        command_runner=command_runner or FakeCommandRunner(),
        capture_runner=capture_runner or FakeCaptureRunner(),
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
        flush_markers=flush_markers,
        sleep=sleep or (lambda seconds: None),
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

    runner = FakeCaptureRunner(
        on_record=lambda count: stop.set() if count >= 3 else None
    )
    deps = _make_deps(
        tmp_path,
        capture_runner=runner,
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
    # One upsert per segment, bracketed by the loop's start report and the
    # stop's two reports, one before the drain and one after it.
    counts = [u["segments_uploaded_today"] for u in status.upserts]
    assert len(counts) == 6
    assert counts[0] == 0
    assert {1, 2, 3} <= set(counts)
    assert counts[-1] == 3


def test_halt_battery_sets_halted_led_drains_queue_and_powers_off(tmp_path):
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

    runner = FakeCaptureRunner(on_record=on_record)
    deps = _make_deps(
        tmp_path,
        capture_runner=runner,
        command_runner=FakeCommandRunner(mux_gate=gate),
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
    # LOW_BATTERY twice; the halt HALTED must come after the whole drain so
    # a battery-halted device finishes red, never green/yellow.
    assert led.calls == [LOW_BATTERY_LED] * 4 + [HALTED_LED]
    assert led.calls[-1] == HALTED_LED
    # And the halt ends in a power-off rather than leaving the Pi powered and
    # discharging, with the wake armed so nothing needs a human to come back.
    assert power.power_offs == [WAKE_ON_CHARGE_PCT]


def test_halt_join_waits_for_slow_worker_before_applying_halted_led(tmp_path):
    # Regression test: the halt path used to join(timeout=5), so a worker
    # call running past 5s let the main thread give up waiting and apply the
    # halt HALTED LED while the worker was still mid-flight; the worker
    # could then overwrite it with its own restore LED. Joining without a
    # timeout closes that race, so this proves the loop stays blocked well
    # past the old 5s window and HALTED is applied only after the worker's
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

    runner = FakeCaptureRunner(on_record=on_record)
    deps = _make_deps(
        tmp_path,
        capture_runner=runner,
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
    # have given up here and returned, applying HALTED while the worker was
    # still blocked above.
    loop_thread.join(timeout=5.2)
    assert loop_thread.is_alive(), "join returned before the worker finished"
    assert HALTED_LED not in led.calls

    release_upload.set()
    loop_thread.join(timeout=5)
    assert not loop_thread.is_alive()

    assert led.calls == [LOW_BATTERY_LED, LOW_BATTERY_LED, HALTED_LED]


def test_three_consecutive_upload_failures_set_critical_and_success_resets(tmp_path):
    stop = threading.Event()
    led = FakeLedDriver()
    # Segments 1-4 fail (CRITICAL at 3 and STILL CRITICAL at 4: a sustained
    # outage must stay red, not blip red once), 5 succeeds (reset), 6-8 fail
    # (counter climbs back to the threshold at 8).
    storage = ScriptedStorageClient(fail_on_attempts={1, 2, 3, 4, 6, 7, 8})

    runner = FakeCaptureRunner(
        on_record=lambda count: stop.set() if count >= 8 else None
    )
    deps = _make_deps(
        tmp_path,
        capture_runner=runner,
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

    starts = iter(range(1, 100))

    def on_capture_start() -> None:
        # Hold each capture until the worker has reported the one before, so
        # attempt 2 is segment 1's upsert and not the stop's own push.
        started = next(starts)
        _wait_for(lambda: status.attempts >= started)

    runner = FakeCaptureRunner(
        on_record=lambda count: stop.set() if count >= 3 else None,
        on_capture_start=on_capture_start,
    )
    deps = _make_deps(
        tmp_path,
        capture_runner=runner,
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
    # Start report, segment 2, the stop's report before the drain, segment 3
    # uploaded by that drain, and the report after it.
    assert [u["recording_active"] for u in status.upserts] == [
        True,
        True,
        False,
        False,
        False,
    ]


def test_setting_stop_exits_after_in_flight_cycle_and_joins_worker(tmp_path):
    stop = threading.Event()
    led = FakeLedDriver()
    storage = FakeStorageClient()

    runner = FakeCaptureRunner(
        on_record=lambda count: stop.set() if count >= 2 else None
    )
    deps = _make_deps(
        tmp_path,
        capture_runner=runner,
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


def test_a_battery_flat_across_every_boot_sample_halts_and_shows_halted():
    led = FakeLedDriver()

    result, slept = _startup(FakeTelemetryBatteryReader(pct=5), led)

    assert result.proceed is False
    assert result.battery_pct == 5
    assert led.calls == [HALTED_LED]
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
    assert led.calls == [HALTED_LED]


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


STOP_SEGMENT_SEC = 5.0


def test_sigterm_during_a_capture_cuts_it_short_instead_of_waiting_for_the_segment(
    tmp_path,
):
    # The loop observes ``stop`` only between segments, so a SIGTERM landing
    # just after a capture starts waits out the whole segment: 300 s measured
    # on the device with the old five-minute segments, and a full minute with
    # today's 60 s default. systemd's stop timeout fires long before that and
    # SIGKILLs the daemon, which is what loses the in-flight segment. The
    # capture must be cut short cleanly instead.
    stop = threading.Event()
    capturing = threading.Event()
    storage = FakeStorageClient()
    runner = FakeCaptureRunner(
        duration_sec=STOP_SEGMENT_SEC, on_capture_start=capturing.set
    )
    deps = _make_deps(
        tmp_path,
        capture_runner=runner,
        battery=battery_state(85),
        led_driver=FakeLedDriver(),
        storage_client=storage,
    )

    signalled_at: list[float] = []

    def sigterm_mid_capture() -> None:
        assert capturing.wait(timeout=10), "the capture never started"
        signalled_at.append(time.monotonic())
        os.kill(os.getpid(), signal.SIGTERM)

    # The daemon's own handler shape: SIGTERM sets stop, and the loop is on
    # the main thread, so the handler runs there exactly as it does in main().
    previous_handler = signal.signal(signal.SIGTERM, lambda *_: stop.set())
    killer = threading.Thread(target=sigterm_mid_capture)
    try:
        killer.start()
        run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 11, 18, 9, 0, 0)))
    finally:
        killer.join(timeout=10)
        signal.signal(signal.SIGTERM, previous_handler)

    stop_latency_sec = time.monotonic() - signalled_at[0]
    assert runner.record_count == 1
    assert runner.cancelled_captures == 1
    # The cut-short segment is muxed, queued and uploaded like a full one.
    assert storage.uploaded == ["device-abc/20261118_090000.mp4"]
    assert stop_latency_sec < STOP_SEGMENT_SEC / 2, (
        f"stop took {stop_latency_sec:.1f}s of a {STOP_SEGMENT_SEC:.0f}s segment"
    )


def test_a_cancelled_capture_exiting_non_zero_still_uploads_its_segment(tmp_path):
    # BENCH-1 again, this time through the loop: if rpicam-vid -s turns out to
    # exit non-zero on SIGUSR2, the footage it finalised is still footage. It
    # must be muxed, queued and uploaded, not dropped as a failed capture.
    stop = threading.Event()
    storage = FakeStorageClient()
    runner = FakeCaptureRunner(
        duration_sec=STOP_SEGMENT_SEC,
        exit_code_after_cancel=1,
        on_capture_start=stop.set,
    )
    deps = _make_deps(
        tmp_path,
        capture_runner=runner,
        battery=battery_state(85),
        led_driver=FakeLedDriver(),
        storage_client=storage,
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 11, 18, 9, 0, 0)))

    assert runner.cancelled_captures == 1
    assert storage.uploaded == ["device-abc/20261118_090000.mp4"]
    assert list(deps.data_dir.iterdir()) == []


@pytest.mark.parametrize("leaves_a_file", [True, False])
def test_a_capture_cancelled_before_its_first_frame_is_discarded(
    tmp_path, leaves_a_file
):
    # A stop landing in the first moments of a segment leaves an empty file,
    # or no file at all when the capture was signalled before it opened its
    # output. There is nothing to mux either way, so it is dropped here rather
    # than queued - and neither case may raise, because this is the ordinary
    # stop path, not a failure.
    stop = threading.Event()
    storage = FakeStorageClient()

    class CancelledBeforeTheFirstFrame(FakeCaptureRunner):
        def run(self, args: list[str], cancel: threading.Event) -> CaptureExit:
            capture_exit = super().run(args, cancel)
            if capture_exit.cancelled:
                output_path = Path(args[args.index("-o") + 1])
                if leaves_a_file:
                    output_path.write_bytes(b"")
                else:
                    output_path.unlink()
            return capture_exit

    runner = CancelledBeforeTheFirstFrame(
        duration_sec=STOP_SEGMENT_SEC, on_capture_start=stop.set
    )
    deps = _make_deps(
        tmp_path,
        capture_runner=runner,
        battery=battery_state(85),
        led_driver=FakeLedDriver(),
        storage_client=storage,
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 11, 18, 9, 0, 0)))

    assert runner.cancelled_captures == 1
    assert storage.uploaded == []
    # Nothing left behind for the next boot to recover either.
    assert list(deps.data_dir.iterdir()) == []


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
        capture_runner=FakeCaptureRunner(on_record=on_record),
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
        capture_runner=FakeCaptureRunner(
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
    # Start report, two segments, and the stop's reports before and after the
    # drain; once a row has said stopped, no later row says recording.
    flags = [u["recording_active"] for u in status.upserts]
    assert len(flags) == 5
    assert flags[0] is True
    assert flags == sorted(flags, reverse=True)


def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition never became true"
        time.sleep(0.005)


class _OrderedStorage(FakeStorageClient):
    def __init__(self, events: list[tuple], gate: threading.Event | None = None):
        super().__init__()
        self._events = events
        self._gate = gate

    def upload(self, bucket: str, object_path: str, local_path: Path) -> None:
        self._events.append(("upload", object_path))
        if self._gate is not None and len(self.uploaded) == 0:
            assert self._gate.wait(timeout=5), "the stop's push never came"
        super().upload(bucket, object_path, local_path)


class _OrderedStatus(FakeStatusClient):
    def __init__(self, events: list[tuple]):
        super().__init__()
        self._events = events

    def upsert_device_status(self, status: dict) -> None:
        self._events.append(
            ("status", status["recording_active"], status["segments_pending"])
        )
        super().upsert_device_status(status)


def test_a_stop_reports_recording_inactive_before_the_drain_uploads(tmp_path):
    # systemd's 30 s stop budget can expire while the drain is still
    # uploading over slow WiFi. The app must already show the stop by then:
    # the cut segment is muxed and queued, recording_active=False is pushed
    # with it counted as pending, and only then does its upload start.
    events: list[tuple] = []
    stop = threading.Event()
    deps = _make_deps(
        tmp_path,
        capture_runner=FakeCaptureRunner(
            duration_sec=STOP_SEGMENT_SEC, on_capture_start=stop.set
        ),
        battery=battery_state(85),
        led_driver=FakeLedDriver(),
        storage_client=_OrderedStorage(events),
        status_client=_OrderedStatus(events),
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 11, 18, 9, 0, 0)))

    assert events == [
        ("status", True, 0),
        ("status", False, 1),
        ("upload", "device-abc/20261118_090000.mp4"),
        ("status", False, 0),
        ("status", False, 0),
    ]


def test_a_stop_during_an_earlier_upload_neither_waits_for_it_nor_is_undone(
    tmp_path,
):
    # A stop that lands while the worker is still uploading an earlier
    # segment must push recording_active=False without waiting for that
    # upload, and the earlier segment's own upsert, landing after the stop's
    # push, must not flip the app back to Recording.
    events: list[tuple] = []
    stop = threading.Event()
    pushed_stopped = threading.Event()
    storage = _OrderedStorage(events, gate=pushed_stopped)

    class Status(_OrderedStatus):
        def upsert_device_status(self, status: dict) -> None:
            super().upsert_device_status(status)
            if status["recording_active"] is False:
                pushed_stopped.set()

    starts = iter(range(1, 100))

    def on_capture_start() -> None:
        if next(starts) == 2:
            _wait_for(lambda: len(events) >= 2)
            stop.set()

    deps = _make_deps(
        tmp_path,
        capture_runner=FakeCaptureRunner(on_capture_start=on_capture_start),
        battery=battery_state(85),
        led_driver=FakeLedDriver(),
        storage_client=storage,
        status_client=Status(events),
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 11, 18, 9, 0, 0)))

    assert events == [
        ("status", True, 0),
        ("upload", "device-abc/20261118_090000.mp4"),
        ("status", False, 2),
        ("status", False, 1),
        ("upload", "device-abc/20261118_090001.mp4"),
        ("status", False, 0),
        ("status", False, 0),
    ]
    assert storage.uploaded == [
        "device-abc/20261118_090000.mp4",
        "device-abc/20261118_090001.mp4",
    ]


def test_battery_halt_reports_recording_inactive_after_the_drain(tmp_path):
    stop = threading.Event()
    status = FakeStatusClient()
    led = FakeLedDriver()
    battery = TriggerableBatteryReader(healthy_pct=85, halt_pct=5)
    deps = _make_deps(
        tmp_path,
        capture_runner=FakeCaptureRunner(
            on_record=lambda count: battery.trigger_halt() if count >= 1 else None
        ),
        battery=responsive_battery(battery),
        led_driver=led,
        status_client=status,
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0)))

    assert status.upserts[-1]["recording_active"] is False
    assert status.upserts[-1]["battery_pct"] == 5
    assert led.calls[-1] == HALTED_LED


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
        capture_runner=FakeCaptureRunner(
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
    assert led.calls[-1] == HALTED_LED
    assert power.power_offs == [WAKE_ON_CHARGE_PCT]


def test_a_charging_pendant_keeps_recording_through_a_flat_gauge(tmp_path):
    # Decision 4, end to end: 2 % and 3.0 V, but plugged in, so the loop runs
    # until something else stops it and nothing powers the device off.
    stop = threading.Event()
    power = FakePowerController()
    battery = FakeTelemetryBatteryReader(pct=2, voltage_mv=3000, external_power=True)
    runner = FakeCaptureRunner(on_record=lambda n: stop.set() if n >= 3 else None)
    deps = _make_deps(
        tmp_path,
        capture_runner=runner,
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
        capture_runner=FakeCaptureRunner(
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
    assert HALTED_LED in led.calls
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
        capture_runner=FakeCaptureRunner(
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
        capture_runner=FakeCaptureRunner(
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
    deps.capture_runner = FakeCaptureRunner(
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
    deps.capture_runner = FakeCaptureRunner(
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

    runner = FakeCaptureRunner()
    deps = _make_deps(
        tmp_path,
        capture_runner=runner,
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
    assert led.calls[-1] == HALTED_LED
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
        capture_runner=FakeCaptureRunner(on_record=on_record),
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
    class FailingCapture(FakeCaptureRunner):
        def run(self, args: list[str], cancel: threading.Event) -> CaptureExit:
            raise RuntimeError("camera not detected")

    status = FakeStatusClient()
    deps = _make_deps(
        tmp_path,
        capture_runner=FailingCapture(),
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
        capture_runner=FakeCaptureRunner(on_record=lambda count: stop.set()),
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
    runner = FakeCaptureRunner(on_record=lambda n: stop.set() if n >= 2 else None)
    deps = _make_deps(
        tmp_path,
        capture_runner=runner,
        battery=battery_state(85),
        led_driver=FakeLedDriver(),
        storage_client=storage,
        status_client=status,
        audio=AudioSettings(),
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 11, 16, 9, 0, 0)))

    captures = runner.calls
    assert all(c[c.index("-o") + 1].endswith(".ts") for c in captures)
    assert storage.uploaded == [
        "device-abc/20261116_090000.mp4",
        "device-abc/20261116_090001.mp4",
    ]
    assert [u["audio_active"] for u in status.upserts] == [True] * 5


def test_a_failing_audio_capture_falls_back_to_video_only_and_keeps_recording(
    tmp_path,
):
    # The microphone disappears mid-run: rpicam-vid's libav capture exits
    # non-zero. Recording must carry on video only instead of stopping.
    stop = threading.Event()
    storage = FakeStorageClient()
    status = FakeStatusClient()

    class MicGoneOnSecondCapture(FakeCaptureRunner):
        def run(self, args: list[str], cancel: threading.Event) -> CaptureExit:
            if "--libav-audio" in args and self.record_count == 1:
                with self._lock:
                    self.calls.append(args)
                raise subprocess.CalledProcessError(1, args)
            return super().run(args, cancel)

    runner = MicGoneOnSecondCapture(on_record=lambda n: stop.set() if n >= 3 else None)
    deps = _make_deps(
        tmp_path,
        capture_runner=runner,
        battery=battery_state(85),
        led_driver=FakeLedDriver(),
        storage_client=storage,
        status_client=status,
        audio=AudioSettings(),
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 11, 16, 9, 0, 0)))

    outputs = [Path(c[c.index("-o") + 1]).name for c in runner.calls]
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
    # The start report and the .ts segment report audio; the two .h264
    # segments after the fallback and the stop's two reports do not.
    flags = [u["audio_active"] for u in status.upserts]
    assert len(flags) == 6
    assert flags[0] is True
    assert flags.count(True) == 2
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

    class DiesMidSegment(FakeCaptureRunner):
        def run(self, args: list[str], cancel: threading.Event) -> CaptureExit:
            if "--libav-audio" in args:
                Path(args[args.index("-o") + 1]).write_bytes(partial_bytes)
                raise subprocess.CalledProcessError(1, args)
            return super().run(args, cancel)

    runner = DiesMidSegment(on_record=lambda n: stop.set() if n >= 1 else None)
    deps = _make_deps(
        tmp_path,
        capture_runner=runner,
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

    class DiesAtOnce(FakeCaptureRunner):
        def run(self, args: list[str], cancel: threading.Event) -> CaptureExit:
            if "--libav-audio" in args:
                Path(args[args.index("-o") + 1]).write_bytes(b"ts-data")
                raise subprocess.CalledProcessError(1, args)
            return super().run(args, cancel)

    deps = _make_deps(
        tmp_path,
        capture_runner=DiesAtOnce(on_record=lambda n: stop.set()),
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

    class DiesOnce(FakeCaptureRunner):
        def run(self, args: list[str], cancel: threading.Event) -> CaptureExit:
            if "--libav-audio" in args:
                Path(args[args.index("-o") + 1]).write_bytes(b"ts-data")
                raise subprocess.CalledProcessError(1, args)
            return super().run(args, cancel)

    deps = _make_deps(
        tmp_path,
        capture_runner=DiesOnce(on_record=lambda n: stop.set() if n >= 2 else None),
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
    class Killed(FakeCaptureRunner):
        def run(self, args: list[str], cancel: threading.Event) -> CaptureExit:
            raise subprocess.CalledProcessError(-15, args)

    deps = _make_deps(
        tmp_path,
        capture_runner=Killed(),
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
    runner = FakeCaptureRunner(on_record=lambda n: stop.set() if n >= 6 else None)

    assert _startup(battery, led)[0].proceed
    deps = _make_deps(
        tmp_path,
        capture_runner=runner,
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
    runner = FakeCaptureRunner(on_record=lambda n: stop.set() if n >= 2 else None)

    assert _startup(battery, led)[0].proceed
    deps = _make_deps(
        tmp_path,
        capture_runner=runner,
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
    runner = FakeCaptureRunner(on_record=lambda n: stop.set() if n >= 5 else None)
    deps = _make_deps(
        tmp_path,
        capture_runner=runner,
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
        capture_runner=FakeCaptureRunner(on_record=on_record),
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
        capture_runner=FakeCaptureRunner(on_record=on_record),
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
        capture_runner=FakeCaptureRunner(on_record=on_record),
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
        capture_runner=FakeCaptureRunner(on_record=on_record),
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
        capture_runner=FakeCaptureRunner(on_record=on_record),
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
        capture_runner=FakeCaptureRunner(on_record=on_record),
        battery=battery_state(85),
        led_driver=FakeLedDriver(),
        storage_client=storage,
        retry_backoff=RetryBackoff(base_sec=0.01, max_sec=0.02),
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0)))

    assert "device-abc/20260703_235900.mp4" in storage.uploaded
    assert list_pending(queue_dir) == []


# The pairing gate main() runs before anything touches Supabase.

PAIRED_DEVICE_ID = "11111111-1111-1111-1111-111111111111"
QR_V2 = (
    '{"v": 2, "ssid": "HomeNet", "password": "hunter2secret", "pairing_code": "code-1"}'
)


class ScriptedScanner:
    def __init__(self, results) -> None:
        self._results = list(results)
        self.scans = 0

    def scan(self):
        self.scans += 1
        return self._results.pop(0) if self._results else None


class RecordingActivator:
    def __init__(self, events: list, fail: bool = False) -> None:
        self._events = events
        self._fail = fail

    def activate(self, connection_id: str) -> None:
        self._events.append(("activate", connection_id))
        if self._fail:
            raise RuntimeError("nmcli failed")


@dataclass
class PairingRig:
    data_dir: Path
    events: list = field(default_factory=list)
    led: FakeLedDriver = field(default_factory=FakeLedDriver)
    pair_error: Exception | None = None

    def pair(self) -> Pairing:
        self.events.append("pair")
        if self.pair_error is not None:
            if isinstance(self.pair_error, PairingRejectedError):
                # What complete_pairing does before raising it.
                (self.data_dir / "pairing.json").unlink(missing_ok=True)
                (self.data_dir / "session.json").unlink(missing_ok=True)
            raise self.pair_error
        store_paired_device_id(self.data_dir / "device.json", PAIRED_DEVICE_ID)
        (self.data_dir / "pairing.json").unlink(missing_ok=True)
        return Pairing(device_id=PAIRED_DEVICE_ID)

    def run(
        self, scanner: ScriptedScanner, *, activation_fails: bool = False
    ) -> str | None:
        paired = ensure_paired(
            self.data_dir,
            scanner,
            RecordingActivator(self.events, fail=activation_fails),
            self.led,
            self.pair,
            nm_keyfile_path=self.data_dir / "visio.nmconnection",
            max_attempts=3,
        )
        return None if paired is None else paired.device_id


def _session(data_dir: Path) -> None:
    write_session_credentials(data_dir / "session.json", "at", "rt")


def test_unpaired_deletes_a_stale_session_then_scans_activates_and_pairs(tmp_path):
    # A pendant onboarded the old way still has the owner's session on disk.
    (tmp_path / "device.json").write_text('{"device_id": "legacy"}')
    _session(tmp_path)
    rig = PairingRig(tmp_path)
    scanner = ScriptedScanner([QR_V2])

    device_id = rig.run(scanner)

    assert device_id == PAIRED_DEVICE_ID
    assert scanner.scans == 1
    assert rig.events == [("activate", "visio"), "pair"]
    assert not (tmp_path / "session.json").exists()


def test_unpaired_without_a_good_scan_never_touches_the_network(tmp_path):
    rig = PairingRig(tmp_path)

    assert rig.run(ScriptedScanner([])) is None

    assert rig.events == []


def test_pending_resumes_without_scanning(tmp_path):
    write_pending_pairing(tmp_path / "pairing.json", "code-1")
    rig = PairingRig(tmp_path)
    scanner = ScriptedScanner([QR_V2])

    assert rig.run(scanner) == PAIRED_DEVICE_ID

    assert scanner.scans == 0
    assert rig.events == [("activate", "visio"), "pair"]


def test_pending_with_failed_activation_keeps_the_code_and_shows_critical(tmp_path):
    write_pending_pairing(tmp_path / "pairing.json", "code-1")
    rig = PairingRig(tmp_path)

    assert rig.run(ScriptedScanner([]), activation_fails=True) is None

    assert "pair" not in rig.events
    assert rig.led.calls[-1] == CRITICAL_LED
    assert pairing_state(tmp_path) is PairingState.PENDING


def test_pending_gives_up_on_the_code_after_repeated_failed_activations(tmp_path):
    # A mistyped Wi-Fi password at first pairing can never connect, so
    # retrying the saved code forever would leave the pendant stuck.
    write_pending_pairing(tmp_path / "pairing.json", "code-1")
    rig = PairingRig(tmp_path)

    for _ in range(MAX_FAILED_PENDING_ACTIVATIONS - 1):
        assert rig.run(ScriptedScanner([]), activation_fails=True) is None
        assert pairing_state(tmp_path) is PairingState.PENDING
    assert rig.run(ScriptedScanner([]), activation_fails=True) is None

    assert pairing_state(tmp_path) is PairingState.UNPAIRED
    assert "pair" not in rig.events
    assert rig.led.calls[-1] == CRITICAL_LED
    scanner = ScriptedScanner([QR_V2])
    assert rig.run(scanner) == PAIRED_DEVICE_ID
    assert scanner.scans == 1


def test_a_fresh_scan_whose_activation_fails_counts_towards_giving_up(tmp_path):
    rig = PairingRig(tmp_path)

    assert rig.run(ScriptedScanner([QR_V2]), activation_fails=True) is None
    for _ in range(MAX_FAILED_PENDING_ACTIVATIONS - 1):
        rig.run(ScriptedScanner([]), activation_fails=True)

    assert pairing_state(tmp_path) is PairingState.UNPAIRED


def test_a_successful_activation_resets_the_failed_activation_count(tmp_path):
    # A home router that was briefly down is not a wrong password.
    write_pending_pairing(tmp_path / "pairing.json", "code-1")
    rig = PairingRig(tmp_path, pair_error=PairingUnavailableError("offline"))
    for _ in range(MAX_FAILED_PENDING_ACTIVATIONS - 1):
        rig.run(ScriptedScanner([]), activation_fails=True)
    rig.run(ScriptedScanner([]))
    for _ in range(MAX_FAILED_PENDING_ACTIVATIONS - 1):
        rig.run(ScriptedScanner([]), activation_fails=True)

    assert pairing_state(tmp_path) is PairingState.PENDING


def test_a_rejected_pairing_shows_critical_and_rescans_next_boot(tmp_path):
    write_pending_pairing(tmp_path / "pairing.json", "code-1")
    rig = PairingRig(tmp_path, pair_error=PairingRejectedError("used code"))

    assert rig.run(ScriptedScanner([])) is None

    assert rig.led.calls[-1] == CRITICAL_LED
    assert pairing_state(tmp_path) is PairingState.UNPAIRED


def test_an_unavailable_pairing_retries_next_boot(tmp_path):
    write_pending_pairing(tmp_path / "pairing.json", "code-1")
    rig = PairingRig(tmp_path, pair_error=PairingUnavailableError("offline"))

    assert rig.run(ScriptedScanner([])) is None

    assert pairing_state(tmp_path) is PairingState.PENDING


def test_paired_boots_normally_without_scanning_or_pairing(tmp_path):
    store_paired_device_id(tmp_path / "device.json", PAIRED_DEVICE_ID)
    _session(tmp_path)
    (tmp_path / "visio.nmconnection").touch()
    rig = PairingRig(tmp_path)
    scanner = ScriptedScanner([QR_V2])

    assert rig.run(scanner) == PAIRED_DEVICE_ID

    assert scanner.scans == 0
    # The best-effort reactivation a restart boot always does.
    assert rig.events == [("activate", "visio")]


def test_paired_with_an_unreadable_session_scans_for_a_new_qr(tmp_path):
    store_paired_device_id(tmp_path / "device.json", PAIRED_DEVICE_ID)
    (tmp_path / "session.json").write_text("")
    rig = PairingRig(tmp_path)
    scanner = ScriptedScanner([QR_V2])

    assert rig.run(scanner) == PAIRED_DEVICE_ID

    assert scanner.scans == 1
    assert rig.events == [("activate", "visio"), "pair"]


def test_a_revoked_login_returns_the_pendant_to_scanning(tmp_path):
    store_paired_device_id(tmp_path / "device.json", PAIRED_DEVICE_ID)
    _session(tmp_path)
    led = FakeLedDriver()

    handle_revoked_login(tmp_path, led)

    assert pairing_state(tmp_path) is PairingState.UNPAIRED
    assert not (tmp_path / "session.json").exists()
    assert led.calls[-1] == CRITICAL_LED


# pair_and_connect, against a fake supabase-py client.


class _FakeAuthSession:
    def __init__(self, access_token: str, refresh_token: str) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token


class _FakeAuth:
    def __init__(self) -> None:
        self._subscribers: list = []

    def on_auth_state_change(self, callback) -> None:
        self._subscribers.append(callback)

    def _emit(self, event: str) -> None:
        for callback in self._subscribers:
            callback(event, _FakeAuthSession(f"{event}-at", f"{event}-rt"))

    def sign_in_anonymously(self) -> None:
        self._emit("SIGNED_IN")

    def refresh_session(self, refresh_token: str | None = None) -> None:
        self._emit("TOKEN_REFRESHED")


class _FakeClaim:
    def execute(self):
        return type("Response", (), {"data": PAIRED_DEVICE_ID})()


class _FakeSupabaseClient:
    def __init__(self) -> None:
        self.auth = _FakeAuth()

    def rpc(self, name: str, params: dict) -> _FakeClaim:
        return _FakeClaim()


def _pair_and_connect(data_dir: Path, scanner: ScriptedScanner, made: list):
    def factory(url: str, key: str) -> _FakeSupabaseClient:
        made.append(_FakeSupabaseClient())
        return made[-1]

    return pair_and_connect(
        data_dir,
        scanner,
        RecordingActivator([]),
        FakeLedDriver(),
        "http://x",
        "anon",
        client_factory=factory,
        nm_keyfile_path=data_dir / "visio.nmconnection",
        max_attempts=3,
    )


def test_a_pairing_boot_keeps_using_the_client_that_paired(tmp_path):
    # Two live clients meant two auto-refresh timers rotating one refresh
    # token and two writers racing on one session.json.
    made: list = []

    connected = _pair_and_connect(tmp_path, ScriptedScanner([QR_V2]), made)

    assert connected is not None
    device_id, clients = connected
    assert device_id == PAIRED_DEVICE_ID
    assert len(made) == 1
    assert isinstance(clients.storage, SupabaseStorage)
    assert clients.storage._client is made[0]


def test_a_paired_boot_restores_the_login_with_one_client(tmp_path):
    store_paired_device_id(tmp_path / "device.json", PAIRED_DEVICE_ID)
    _session(tmp_path)
    made: list = []

    connected = _pair_and_connect(tmp_path, ScriptedScanner([]), made)

    assert connected is not None
    assert connected[0] == PAIRED_DEVICE_ID
    assert len(made) == 1


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
    runner = FakeCaptureRunner(on_record=lambda n: stop.set() if n >= 3 else None)
    deps = _make_deps(
        tmp_path,
        capture_runner=runner,
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
    assert HALTED_LED not in led.calls


def test_a_fixed_full_percentage_only_source_boots_straight_into_recording():
    led = FakeLedDriver()

    result, _ = _startup(FakeBatteryReader(100), led)

    assert result.proceed is True
    assert result.battery_pct == 100
    assert led.calls == [RECORDING_LED]


MAGENTA = (255, 0, 255)


class _HeldBlink:
    """Holds a blink open until the test releases it."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, seconds: float) -> None:
        self.started.set()
        assert self.release.wait(timeout=5), "the blink was never released"


def _colours(spi: SimulatedSpiDevice) -> list[tuple[int, int, int]]:
    return [decode_frame(frame, 6_250_000)[0] for frame in spi.writes]


def test_a_press_blinks_magenta_on_the_wire_and_puts_the_state_back(tmp_path):
    # The real led.py, the real SPI driver and the real press handler, with
    # only /dev/spidev0.0 simulated: the colours a WS2812B would latch,
    # decoded from the wire bytes by the datasheet-timing receiver. Storage
    # is never reached, which is the point - the acknowledgement is what the
    # wearer gets while WiFi is down.
    spi = SimulatedSpiDevice()
    blink = _HeldBlink()
    led = LedController(Ws2812LedDriver(spi=spi), sleep=blink)
    battery = FakeBatteryReader(85)
    assert run_startup_sequence(battery, led).proceed

    press = make_flag_press_handler(
        queue_dir=tmp_path / "queue",
        submit=lambda path: None,
        ack=led.ack_flag,
        clock=FakeClock(datetime(2026, 7, 4, 12, 3, 45)),
    )
    press()

    assert blink.started.wait(timeout=5)
    assert _colours(spi) == [(0, 255, 0), MAGENTA]
    blink.release.set()
    led.close()

    assert _colours(spi) == [(0, 255, 0), MAGENTA, (0, 255, 0)]
    assert spi.overlapping_writes == 0


def test_a_press_blinks_over_a_latched_critical_and_restores_the_red(tmp_path):
    # WiFi has been down for hours, so the LED is red: exactly the case the
    # local-first acknowledgement exists for. The latch has to come back.
    spi = SimulatedSpiDevice()
    blink = _HeldBlink()
    led = LedController(Ws2812LedDriver(spi=spi), sleep=blink)
    apply_led_state(led, LedState.CRITICAL)

    press = make_flag_press_handler(
        queue_dir=tmp_path / "queue",
        submit=lambda path: None,
        ack=led.ack_flag,
        clock=FakeClock(datetime(2026, 7, 4, 12, 3, 45)),
    )
    press()

    assert blink.started.wait(timeout=5)
    blink.release.set()
    led.close()

    red = (255, 0, 0)
    assert _colours(spi) == [red, MAGENTA, red]
    assert spi.overlapping_writes == 0


def test_a_queued_marker_reaches_storage_before_the_next_segment(tmp_path):
    # The outage case end to end: a press whose upload failed is still on the
    # card when WiFi returns, and the segment it flags must not overtake it -
    # the nightly run scores a segment against the markers in storage when it
    # runs, and anything later falls to the DLQ's late-marker retry.
    queue_dir = tmp_path / "queue"
    storage = FakeStorageClient()
    write_flag_marker(queue_dir, datetime(2026, 7, 4, 11, 59, 30))
    flag_worker = FlagUploadWorker(storage, "device-abc", queue_dir)
    stop = threading.Event()

    deps = _make_deps(
        tmp_path,
        capture_runner=FakeCaptureRunner(on_record=lambda n: stop.set()),
        battery=battery_state(85),
        led_driver=FakeLedDriver(),
        storage_client=storage,
        flush_markers=flag_worker.flush_markers,
    )
    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0)))
    flag_worker.stop()

    assert storage.uploaded == [
        "device-abc/FLAG_20260704_115930.marker",
        "device-abc/20260704_120000.mp4",
    ]
    assert list_pending(queue_dir) == []


def test_a_press_late_in_the_restart_gap_reaches_storage_before_its_segment(
    tmp_path,
):
    # Segment N's capture returns at T, the worker has it muxed by T+1 - where
    # the flush used to run - and the wearer presses at T+2.5, inside the 3 s
    # gap the pipeline attaches to segment N. One fake clock drives all of it.
    # Recording carries on (the stop lands only on the next capture, once
    # segment N is up), since the gap is waited out only while recording.
    queue_dir = tmp_path / "queue"
    capture_returned_at = 100.0
    press_at = capture_returned_at + 2.5
    lock = threading.Lock()
    now = [0.0]
    slept: list[float] = []
    first_segment_uploaded = threading.Event()

    class TimedStorage(FakeStorageClient):
        markers_stored_when_segment_started: list[str] | None = None

        def upload(self, bucket, object_path, local_path):
            first = (
                local_path.suffix == ".mp4"
                and self.markers_stored_when_segment_started is None
            )
            if first:
                self.markers_stored_when_segment_started = [
                    key for key in self.uploaded if key.endswith(".marker")
                ]
                advance(2.0)
            super().upload(bucket, object_path, local_path)
            if first:
                first_segment_uploaded.set()

    storage = TimedStorage()
    flag_worker = FlagUploadWorker(storage, "device-abc", queue_dir)
    press = make_flag_press_handler(
        queue_dir=queue_dir,
        submit=flag_worker.submit,
        clock=FakeClock(datetime(2026, 7, 4, 12, 0, 3), step_seconds=0),
    )

    def advance(seconds: float) -> None:
        with lock:
            before = now[0]
            now[0] += seconds
            after = now[0]
        if before < press_at <= after:
            press()

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        advance(seconds)

    class MuxTakesASecond(FakeCommandRunner):
        def run(self, args):
            if "ffmpeg" in args:
                advance(1.0)
            super().run(args)

    stop = threading.Event()

    def capture_returns(count: int) -> None:
        if count == 1:
            with lock:
                now[0] = capture_returned_at
            return
        assert first_segment_uploaded.wait(timeout=10), "segment N never uploaded"
        stop.set()

    deps = _make_deps(
        tmp_path,
        capture_runner=FakeCaptureRunner(on_record=capture_returns),
        command_runner=MuxTakesASecond(),
        battery=battery_state(85),
        led_driver=FakeLedDriver(),
        storage_client=storage,
        monotonic=lambda: now[0],
        flush_markers=flag_worker.flush_markers,
        sleep=sleep,
    )
    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0)))
    flag_worker.stop()

    assert storage.markers_stored_when_segment_started == [
        "device-abc/FLAG_20260704_120003.marker"
    ]
    # Only segment N waited; the cut-short segment N+1 the stop left did not.
    assert slept == [2.0]
    assert storage.uploaded == [
        "device-abc/FLAG_20260704_120003.marker",
        "device-abc/20260704_120000.mp4",
        "device-abc/20260704_120001.mp4",
    ]
    assert list_pending(queue_dir) == []


def test_markers_only_ever_leave_on_the_flag_workers_own_thread(tmp_path):
    # The invariant that lets both workers run without a lock on the queue:
    # the upload worker asks for a flush instead of uploading a marker, so
    # the two can never see the same marker and both send it.
    queue_dir = tmp_path / "queue"

    class ThreadRecordingStorage(FakeStorageClient):
        def __init__(self) -> None:
            super().__init__()
            self.marker_threads: set[str] = set()

        def upload(self, bucket, object_path, local_path):
            if local_path.suffix == ".marker":
                self.marker_threads.add(threading.current_thread().name)
            super().upload(bucket, object_path, local_path)

    storage = ThreadRecordingStorage()
    for second in (10, 40):
        write_flag_marker(queue_dir, datetime(2026, 7, 4, 11, 59, second))
    flag_worker = FlagUploadWorker(storage, "device-abc", queue_dir)
    stop = threading.Event()

    deps = _make_deps(
        tmp_path,
        capture_runner=FakeCaptureRunner(
            on_record=lambda n: stop.set() if n >= 2 else None
        ),
        battery=battery_state(85),
        led_driver=FakeLedDriver(),
        storage_client=storage,
        flush_markers=flag_worker.flush_markers,
    )
    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0)))
    flag_worker.submit(write_flag_marker(queue_dir, datetime(2026, 7, 4, 12, 0, 30)))
    flag_worker.stop()

    assert storage.marker_threads == {FLAG_UPLOAD_THREAD_NAME}
    assert list_pending(queue_dir) == []


def test_a_stop_mid_capture_flushes_markers_before_the_cut_segment_without_the_gap(
    tmp_path,
):
    # The stop path queues the cut-short last segment on the main thread and
    # hands the worker an already-muxed segment. A flag pressed during that
    # capture must still reach storage first, but the stop does not wait out
    # the restart gap for it: the clock here never moves, so a gap wait would
    # sleep the full 3 s, and the flush gets the short stop timeout instead of
    # the flag worker's 30 s, which alone would spend TimeoutStopSec=30.
    queue_dir = tmp_path / "queue"
    storage = FakeStorageClient()
    flag_worker = FlagUploadWorker(storage, "device-abc", queue_dir)
    stop = threading.Event()
    flush_timeouts: list[float | None] = []
    slept: list[float] = []

    def flush(timeout: float | None = None) -> bool:
        flush_timeouts.append(timeout)
        return flag_worker.flush_markers(timeout=timeout)

    def press_then_stop() -> None:
        write_flag_marker(queue_dir, datetime(2026, 11, 18, 9, 0, 1))
        stop.set()

    runner = FakeCaptureRunner(
        duration_sec=STOP_SEGMENT_SEC, on_capture_start=press_then_stop
    )
    deps = _make_deps(
        tmp_path,
        capture_runner=runner,
        battery=battery_state(85),
        led_driver=FakeLedDriver(),
        storage_client=storage,
        monotonic=lambda: 50.0,
        flush_markers=flush,
        sleep=slept.append,
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 11, 18, 9, 0, 0)))
    flag_worker.stop()

    assert runner.cancelled_captures == 1
    assert storage.uploaded == [
        "device-abc/FLAG_20261118_090001.marker",
        "device-abc/20261118_090000.mp4",
    ]
    assert slept == []
    assert flush_timeouts == [STOP_MARKER_FLUSH_TIMEOUT_SEC]
    assert list_pending(queue_dir) == []


def test_a_stuck_marker_flush_on_stop_does_not_hold_the_cut_segment_back(tmp_path):
    # The flush on the stop path is bounded: markers the network will not
    # take stay on the card, and the cut-short segment still goes up inside
    # the shutdown budget rather than after the flag worker's own timeout.
    queue_dir = tmp_path / "queue"
    release = threading.Event()

    class MarkersHang(FakeStorageClient):
        def upload(self, bucket, object_path, local_path):
            if local_path.suffix == ".marker":
                release.wait(timeout=10)
                raise OSError("network unreachable")
            super().upload(bucket, object_path, local_path)

    storage = MarkersHang()
    flag_worker = FlagUploadWorker(storage, "device-abc", queue_dir)
    write_flag_marker(queue_dir, datetime(2026, 11, 18, 8, 59, 59))
    stop = threading.Event()
    deps = replace(
        _make_deps(
            tmp_path,
            capture_runner=FakeCaptureRunner(
                duration_sec=STOP_SEGMENT_SEC, on_capture_start=stop.set
            ),
            battery=battery_state(85),
            led_driver=FakeLedDriver(),
            storage_client=storage,
            flush_markers=flag_worker.flush_markers,
        ),
        stop_marker_flush_timeout_sec=0.2,
    )

    started = time.monotonic()
    try:
        run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 11, 18, 9, 0, 0)))
        elapsed = time.monotonic() - started
    finally:
        release.set()
        flag_worker.stop()

    assert storage.uploaded == ["device-abc/20261118_090000.mp4"]
    assert elapsed < 2.0
    # The marker is still on the card for the next try.
    assert [p.name for p in list_pending(queue_dir)] == ["FLAG_20261118_085959.marker"]


def test_a_capture_with_no_footage_is_discarded_without_a_marker_flush(tmp_path):
    # An empty capture never reaches the worker: nothing is uploaded for it
    # and no flush is spent on it, and since a segment's restart-gap clock
    # travels with the segment itself, it cannot move the time the next
    # flush waits on either. The segments either side keep their flushes,
    # the cut-short last one included.
    stop = threading.Event()
    storage = FakeStorageClient()
    flushes: list[float | None] = []

    def flush(timeout: float | None = None) -> bool:
        flushes.append(timeout)
        return True

    class SecondCaptureIsEmpty(FakeCaptureRunner):
        def run(self, args: list[str], cancel: threading.Event) -> CaptureExit:
            capture_exit = super().run(args, cancel)
            if self.record_count == 2:
                Path(args[args.index("-o") + 1]).write_bytes(b"")
            return capture_exit

    starts = [0]

    def stop_on_the_third_capture() -> None:
        starts[0] += 1
        if starts[0] == 3:
            stop.set()

    runner = SecondCaptureIsEmpty(on_capture_start=stop_on_the_third_capture)
    deps = _make_deps(
        tmp_path,
        capture_runner=runner,
        battery=battery_state(85),
        led_driver=FakeLedDriver(),
        storage_client=storage,
        flush_markers=flush,
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 11, 18, 9, 0, 0)))

    assert runner.record_count == 3
    assert storage.uploaded == [
        "device-abc/20261118_090000.mp4",
        "device-abc/20261118_090002.mp4",
    ]
    assert len(flushes) == 2
    assert list(deps.data_dir.iterdir()) == []


# --- the battery halt composed with the stop path and the flag button --------


def test_a_stop_during_the_halt_drain_still_ends_in_the_power_off(tmp_path):
    # The halt drain may wait 120 s, but a SIGTERM arriving meanwhile gives
    # systemd 30 s before it kills the daemon - and a halt killed mid-drain
    # never powers off, which is the brownout the halt exists to prevent. So a
    # pending stop cuts the rest of the drain short and the power-off follows.
    stop = threading.Event()
    power = FakePowerController()
    led = FakeLedDriver()
    battery = FakeTelemetryBatteryReader(pct=85, voltage_mv=3900)
    release = threading.Event()
    wedged = threading.Event()

    class WedgedStorageClient:
        def upload(self, bucket, object_path, local_path):
            wedged.set()
            assert release.wait(timeout=10), "the test never released the upload"

    def on_record(count: int) -> None:
        if count == 1:
            return
        assert wedged.wait(timeout=5), "the worker never reached the upload"
        battery.pct = 5

    class SigtermAsTheDrainStarts(FakeStatusClient):
        # The False push comes just before the drain, so the stop provably
        # lands while the halt is already draining.
        def upsert_device_status(self, status: dict) -> None:
            super().upsert_device_status(status)
            if not status["recording_active"]:
                stop.set()

    deps = replace(
        _make_deps(
            tmp_path,
            capture_runner=FakeCaptureRunner(on_record=on_record),
            battery=responsive_battery(battery),
            led_driver=led,
            storage_client=WedgedStorageClient(),
            status_client=SigtermAsTheDrainStarts(),
            power_controller=power,
            upload_drain_timeout_sec=30.0,
        ),
        stop_halt_drain_timeout_sec=0.2,
    )

    started = time.monotonic()
    try:
        outcome = run_recording_loop(
            deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0))
        )
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert outcome is LoopOutcome.HALTED
    assert elapsed < 5.0, f"the halt drain ignored the stop for {elapsed:.1f}s"
    assert led.calls[-1] == HALTED_LED
    assert power.power_offs == [WAKE_ON_CHARGE_PCT]


def test_a_halt_drain_flushes_markers_before_its_segments_without_the_gap(
    tmp_path,
):
    # The halt drains through the same stopping path as a stop: flags still go
    # up ahead of the segments, but neither the restart gap nor the flag
    # worker's 30 s flush may eat into a dying battery's drain.
    queue_dir = tmp_path / "queue"
    storage = FakeStorageClient()
    flag_worker = FlagUploadWorker(storage, "device-abc", queue_dir)
    power = FakePowerController()
    battery = TriggerableBatteryReader(healthy_pct=85, halt_pct=5)
    gate = threading.Event()
    flush_timeouts: list[float | None] = []
    slept: list[float] = []

    def flush(timeout: float | None = None) -> bool:
        flush_timeouts.append(timeout)
        return flag_worker.flush_markers(timeout=timeout)

    def on_record(count: int) -> None:
        write_flag_marker(queue_dir, datetime(2026, 7, 4, 12, 0, 0, 500000))
        battery.trigger_halt()
        gate.set()

    deps = _make_deps(
        tmp_path,
        capture_runner=FakeCaptureRunner(on_record=on_record),
        # The gate holds the worker until the halt is observed, so the
        # segment provably drains on the stopping path.
        command_runner=FakeCommandRunner(mux_gate=gate),
        battery=responsive_battery(battery),
        led_driver=FakeLedDriver(),
        storage_client=storage,
        power_controller=power,
        monotonic=lambda: 50.0,
        flush_markers=flush,
        sleep=slept.append,
    )

    outcome = run_recording_loop(
        deps, threading.Event(), clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0))
    )
    flag_worker.stop()

    assert outcome is LoopOutcome.HALTED
    assert storage.uploaded == [
        "device-abc/FLAG_20260704_120000.marker",
        "device-abc/20260704_120000.mp4",
    ]
    assert slept == []
    assert flush_timeouts == [STOP_MARKER_FLUSH_TIMEOUT_SEC]
    assert power.power_offs == [WAKE_ON_CHARGE_PCT]


def test_a_stop_that_cuts_a_capture_short_ends_as_a_stop_not_a_halt(tmp_path):
    # A stop mid-capture leaves the loop straight from the cut-short segment,
    # without another battery reading, so a battery that would have confirmed
    # a halt on the next read does not turn an operator's stop into a
    # power-off. The segment is kept and uploaded like any stop's, and the
    # next boot's battery check is what confirms the halt and powers off.
    stop = threading.Event()
    storage = FakeStorageClient()
    power = FakePowerController()
    battery = TriggerableBatteryReader(healthy_pct=85, halt_pct=5)

    def flat_then_stop() -> None:
        battery.trigger_halt()
        stop.set()

    runner = FakeCaptureRunner(
        duration_sec=STOP_SEGMENT_SEC, on_capture_start=flat_then_stop
    )
    deps = _make_deps(
        tmp_path,
        capture_runner=runner,
        battery=responsive_battery(battery),
        led_driver=FakeLedDriver(),
        storage_client=storage,
        power_controller=power,
    )

    outcome = run_recording_loop(
        deps, stop, clock=FakeClock(datetime(2026, 11, 18, 9, 0, 0))
    )

    assert outcome is LoopOutcome.STOPPED
    assert runner.cancelled_captures == 1
    assert storage.uploaded == ["device-abc/20261118_090000.mp4"]
    assert power.power_offs == []


def test_a_press_after_the_halt_leaves_the_halted_red_on_the_wire(tmp_path):
    # The single LED owner is LedController, and Halted is the last thing the
    # wearer sees before the pendant powers off: an acknowledgement is an
    # overlay on Recording, so a press once the halt is showing blinks
    # nothing, and the decoded wire ends on solid red.
    spi = SimulatedSpiDevice()
    led = LedController(Ws2812LedDriver(spi=spi), sleep=lambda _: None)
    power = FakePowerController()
    battery = TriggerableBatteryReader(healthy_pct=85, halt_pct=5)
    deps = _make_deps(
        tmp_path,
        capture_runner=FakeCaptureRunner(
            on_record=lambda count: battery.trigger_halt()
        ),
        battery=responsive_battery(battery),
        led_driver=led,
        power_controller=power,
    )

    outcome = run_recording_loop(
        deps, threading.Event(), clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0))
    )
    press = make_flag_press_handler(
        queue_dir=tmp_path / "queue",
        submit=lambda path: None,
        ack=led.ack_flag,
        clock=FakeClock(datetime(2026, 7, 4, 12, 0, 5)),
    )
    press()
    led.close()

    assert outcome is LoopOutcome.HALTED
    assert _colours(spi)[-1] == (255, 0, 0)
    assert MAGENTA not in _colours(spi)
    assert spi.overlapping_writes == 0


# --- main(): the battery boot check, the pairing gate and the recording run ---
#
# main() composes the boot from parts tested above. These tests drive it with
# every hardware factory replaced by a fake, to pin the order the merged boot
# runs them in and what it leaves behind on each early exit.


class _RecordingLedController(LedController):
    """The real single LED owner, remembering every instance main() builds."""

    instances: list["_RecordingLedController"] = []

    def __init__(self, driver, **kwargs) -> None:
        super().__init__(driver, **kwargs)
        self.writes: list[tuple[tuple[int, int, int], LedPattern]] = []
        _RecordingLedController.instances.append(self)

    def set(self, color, pattern) -> None:
        self.writes.append((color, pattern))
        super().set(color, pattern)


class _FakeFlagButton:
    def __init__(self) -> None:
        self.callback: Callable[[], None] | None = None

    def on_press(self, callback: Callable[[], None]) -> None:
        self.callback = callback


@dataclass
class MainRig:
    """Everything main() reaches for, faked; ``run`` calls the real main()."""

    data_dir: Path
    monkeypatch: pytest.MonkeyPatch
    battery_pct: int = 85
    scanner: ScriptedScanner = field(default_factory=lambda: ScriptedScanner([]))
    activation_fails: bool = False
    refresh_error: Exception | None = None
    power: FakePowerController = field(default_factory=FakePowerController)
    raw_led: FakeLedDriver = field(default_factory=FakeLedDriver)
    made_clients: list = field(default_factory=list)
    flag_workers: list = field(default_factory=list)
    loop_deps: list = field(default_factory=list)
    events: list = field(default_factory=list)
    # What the fake recording loop does with its deps and stop event.
    during_run: Callable[[LoopDeps, threading.Event], None] | None = None

    def run(self) -> int:
        import visio_recorder.daemon as daemon

        mp = self.monkeypatch
        _RecordingLedController.instances = []
        mp.setenv("SUPABASE_URL", "http://x")
        mp.setenv("SUPABASE_ANON_KEY", "anon")
        mp.setenv("VISIO_DATA_DIR", str(self.data_dir))
        mp.setenv("VISIO_AUDIO", "off")
        mp.setattr(daemon, "open_pijuice", lambda: None)
        mp.setattr(
            daemon,
            "PiJuiceBatteryReader",
            lambda _: FakeBatteryReader(self.battery_pct),
        )
        mp.setattr(daemon, "Ws2812LedDriver", lambda: self.raw_led)
        mp.setattr(daemon, "LedController", _RecordingLedController)
        mp.setattr(daemon, "resolve_power_controller", lambda *_: self.power)
        mp.setattr(daemon, "SubprocessCommandRunner", FakeCommandRunner)
        mp.setattr(daemon, "SubprocessInterruptibleRunner", FakeCaptureRunner)
        mp.setattr(daemon, "RpicamZbarScanner", lambda: self.scanner)
        mp.setattr(
            daemon,
            "NmcliConnectionActivator",
            lambda: RecordingActivator(self.events, fail=self.activation_fails),
        )
        mp.setattr(daemon, "GpioZeroFlagButton", _FakeFlagButton)
        mp.setattr(daemon.signal, "signal", lambda *_: None)

        real_startup = daemon.run_startup_sequence

        def startup(reader, led):
            self.events.append("battery check")
            return real_startup(reader, led, sleep=lambda _: None)

        mp.setattr(daemon, "run_startup_sequence", startup)

        real_pair_and_connect = daemon.pair_and_connect

        def factory(url: str, key: str) -> _FakeSupabaseClient:
            client = _FakeSupabaseClient()
            if self.refresh_error is not None:
                error = self.refresh_error

                def refuse(refresh_token=None):
                    raise error

                client.auth.refresh_session = refuse  # type: ignore[method-assign]
            self.made_clients.append(client)
            return client

        def pair_and_connect_(*args, **kwargs):
            self.events.append("pairing gate")
            return real_pair_and_connect(
                *args,
                client_factory=factory,
                nm_keyfile_path=self.data_dir / "visio.nmconnection",
                max_attempts=2,
                **kwargs,
            )

        mp.setattr(daemon, "pair_and_connect", pair_and_connect_)

        real_worker = daemon.FlagUploadWorker

        def flag_worker(*args, **kwargs):
            worker = real_worker(*args, **kwargs)
            self.flag_workers.append(worker)
            return worker

        mp.setattr(daemon, "FlagUploadWorker", flag_worker)

        def loop(deps, stop):
            self.events.append("recording loop")
            self.loop_deps.append(deps)
            if self.during_run is not None:
                self.during_run(deps, stop)
            return LoopOutcome.STOPPED

        mp.setattr(daemon, "run_recording_loop", loop)
        return daemon.main()

    @property
    def led(self) -> "_RecordingLedController":
        assert len(_RecordingLedController.instances) == 1
        return _RecordingLedController.instances[0]


def test_main_a_flat_battery_powers_off_before_an_unpaired_pendant_scans(
    tmp_path, monkeypatch
):
    # The legacy session an old-contract pendant holds is only deleted by the
    # pairing gate, which a flat battery never reaches.
    _session(tmp_path)
    rig = MainRig(tmp_path, monkeypatch, battery_pct=2)

    assert rig.run() == 0

    assert rig.power.powered_off
    assert rig.events == ["battery check"]
    assert rig.scanner.scans == 0
    assert rig.made_clients == []
    assert (tmp_path / "session.json").exists()
    assert rig.led.writes[-1] == HALTED_LED
    assert not rig.led._thread.is_alive()


def test_main_runs_the_battery_check_before_the_pairing_gate(tmp_path, monkeypatch):
    rig = MainRig(tmp_path, monkeypatch, scanner=ScriptedScanner([QR_V2]))

    assert rig.run() == 0

    assert rig.events[:2] == ["battery check", "pairing gate"]
    assert rig.events[-1] == "recording loop"
    assert not rig.power.powered_off


def test_main_onboarding_led_writes_go_through_the_single_led_owner(
    tmp_path, monkeypatch
):
    # A fresh scan whose Wi-Fi fails shows Critical; it must be drawn by the
    # one LedController, not by a second writer on the raw driver.
    rig = MainRig(
        tmp_path,
        monkeypatch,
        scanner=ScriptedScanner([QR_V2]),
        activation_fails=True,
    )

    assert rig.run() == 1

    assert rig.led.writes[-1] == CRITICAL_LED
    assert rig.raw_led.calls == rig.led.writes
    assert pairing_state(tmp_path) is PairingState.PENDING
    assert not rig.led._thread.is_alive()


def test_main_flag_worker_and_status_reporter_share_the_one_pairing_client(
    tmp_path, monkeypatch
):
    rig = MainRig(tmp_path, monkeypatch, scanner=ScriptedScanner([QR_V2]))

    assert rig.run() == 0

    assert len(rig.made_clients) == 1
    (client,) = rig.made_clients
    (deps,) = rig.loop_deps
    (flag_worker,) = rig.flag_workers
    assert deps.device_id == PAIRED_DEVICE_ID
    assert deps.storage_client._client is client
    assert deps.status_client._client is client
    assert flag_worker._storage_client._client is client
    assert deps.flush_markers == flag_worker.flush_markers
    assert deps.led_driver is rig.led
    assert not flag_worker._thread.is_alive()
    assert not rig.led._thread.is_alive()


def test_main_a_paired_boot_restores_the_login_with_one_client(tmp_path, monkeypatch):
    store_paired_device_id(tmp_path / "device.json", PAIRED_DEVICE_ID)
    _session(tmp_path)
    rig = MainRig(tmp_path, monkeypatch, scanner=ScriptedScanner([QR_V2]))

    assert rig.run() == 0

    assert rig.scanner.scans == 0
    assert len(rig.made_clients) == 1
    assert rig.loop_deps[0].storage_client._client is rig.made_clients[0]


def test_main_a_revoked_login_at_boot_goes_back_to_scanning_and_stops_threads(
    tmp_path, monkeypatch
):
    from supabase_auth.errors import AuthApiError

    store_paired_device_id(tmp_path / "device.json", PAIRED_DEVICE_ID)
    _session(tmp_path)
    rig = MainRig(
        tmp_path,
        monkeypatch,
        refresh_error=AuthApiError("refused", 400, "user_not_found"),
    )

    assert rig.run() == 1

    assert pairing_state(tmp_path) is PairingState.UNPAIRED
    assert not (tmp_path / "session.json").exists()
    assert rig.led.writes[-1] == CRITICAL_LED
    assert rig.flag_workers == []
    assert rig.loop_deps == []
    assert not rig.led._thread.is_alive()


def test_main_a_login_revoked_during_the_run_stops_and_goes_back_to_scanning(
    tmp_path, monkeypatch
):
    # Remove device while the pendant records: the next on-demand refresh is
    # refused, which must end the run and unpair.
    from supabase_auth.errors import AuthApiError

    from visio_recorder.session_store import SessionRevokedError

    store_paired_device_id(tmp_path / "device.json", PAIRED_DEVICE_ID)
    _session(tmp_path)
    segment = tmp_path / "elsewhere" / "20261120_090000.mp4"
    segment.parent.mkdir()
    segment.write_bytes(b"segment")
    seen: list = []

    def revoke(deps, stop) -> None:
        session = deps.storage_client._session
        session.mark_stale()

        def refuse(refresh_token=None):
            raise AuthApiError("refused", 404, "session_not_found")

        session.client.auth.refresh_session = refuse
        with pytest.raises(SessionRevokedError):
            deps.storage_client.upload("segments", "d/x.mp4", segment)
        seen.append(stop.is_set())

    rig = MainRig(tmp_path, monkeypatch, during_run=revoke)

    assert rig.run() == 1

    assert seen == [True]
    assert pairing_state(tmp_path) is PairingState.UNPAIRED
    assert not (tmp_path / "session.json").exists()
    assert rig.led.writes[-1] == CRITICAL_LED
    assert not rig.flag_workers[0]._thread.is_alive()
