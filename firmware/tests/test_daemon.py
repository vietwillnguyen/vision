import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest

from tests.fakes import (
    FailingStorageClient,
    FakeBatteryReader,
    FakeClock,
    FakeCommandRunner,
    FakeDiskStatsReader,
    FakeLedDriver,
    FakeStatusClient,
    FakeStorageClient,
    ScriptedStatusClient,
    ScriptedStorageClient,
    SimulatedSpiDevice,
    TriggerableBatteryReader,
    unlimited_upload_limit,
)
from tests.ws2812_waveform import decode_frame
from visio_recorder.capture import AudioSettings, VideoSettings
from visio_recorder.daemon import (
    LoopDeps,
    load_config,
    resolve_audio,
    run_recording_loop,
    run_startup_sequence,
)
from visio_recorder.drivers import Ws2812LedDriver
from visio_recorder.flag_button import (
    FLAG_UPLOAD_THREAD_NAME,
    FlagUploadWorker,
    make_flag_press_handler,
    write_flag_marker,
)
from visio_recorder.led import LedController, LedPattern, LedState, apply_led_state
from visio_recorder.recording_loop import DiskStats, RetryBackoff
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
    battery_reader,
    led_driver,
    storage_client=None,
    status_client=None,
    audio=None,
    retry_backoff=None,
    monotonic=None,
    flush_markers=None,
    sleep=None,
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
        video=VideoSettings(),
        upload_limit=unlimited_upload_limit(),
        audio=audio,
        flush_markers=flush_markers,
        sleep=sleep or (lambda seconds: None),
        **({} if retry_backoff is None else {"retry_backoff": retry_backoff}),
        **({} if monotonic is None else {"monotonic": monotonic}),
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
    # One upsert per segment, bracketed by the loop's start and exit reports.
    assert [u["segments_uploaded_today"] for u in status.upserts] == [0, 1, 2, 3, 3]


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
        battery_reader=FakeBatteryReader(85),
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
        battery_reader=FakeBatteryReader(85),
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
        battery_reader=battery,
        led_driver=led,
        status_client=status,
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0)))

    assert status.upserts[-1]["recording_active"] is False
    assert status.upserts[-1]["battery_pct"] == 5
    assert led.calls[-1] == CRITICAL_LED


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
        battery_reader=FakeBatteryReader(85),
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
        battery_reader=FakeBatteryReader(85),
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
        battery_reader=FakeBatteryReader(85),
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
        battery_reader=FakeBatteryReader(85),
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
        battery_reader=FakeBatteryReader(85),
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
        battery_reader=FakeBatteryReader(85),
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
        battery_reader=FakeBatteryReader(85),
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
        battery_reader=FakeBatteryReader(85),
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
    spi = SimulatedSpiDevice()
    led = Ws2812LedDriver(spi=spi)
    # 15% once triggered: low battery, but above the halt threshold.
    battery = TriggerableBatteryReader(healthy_pct=85, halt_pct=15)

    class OutageThenLowBattery(ScriptedStorageClient):
        # Uploads 2-4 fail; the battery reads low from upload 5 on. The worker
        # reads the battery before each upload, so segment 6 is the first to
        # see it - deterministic, unlike a main-loop trigger.
        def upload(self, bucket, object_path, local_path):
            try:
                super().upload(bucket, object_path, local_path)
            finally:
                if self.attempts == 5:
                    battery.trigger_halt()

    storage = OutageThenLowBattery(fail_on_attempts={2, 3, 4})
    stop = threading.Event()
    runner = FakeCommandRunner(on_record=lambda n: stop.set() if n >= 6 else None)

    assert run_startup_sequence(battery, led).proceed
    deps = _make_deps(
        tmp_path,
        command_runner=runner,
        battery_reader=battery,
        led_driver=led,
        storage_client=storage,
    )
    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0)))

    green, blue, yellow, red = (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 0, 0)
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
        + [yellow, yellow]  # 6 uploads on a low battery
    )


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
        battery_reader=FakeBatteryReader(85),
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
        battery_reader=FakeBatteryReader(85),
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
        battery_reader=FakeBatteryReader(85),
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
        battery_reader=FakeBatteryReader(85),
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
        battery_reader=FakeBatteryReader(85),
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
        battery_reader=FakeBatteryReader(85),
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
        battery_reader=FakeBatteryReader(85),
        led_driver=FakeLedDriver(),
        storage_client=storage,
        retry_backoff=RetryBackoff(base_sec=0.01, max_sec=0.02),
    )

    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0)))

    assert "device-abc/20260703_235900.mp4" in storage.uploaded
    assert list_pending(queue_dir) == []


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
        command_runner=FakeCommandRunner(on_record=lambda n: stop.set()),
        battery_reader=FakeBatteryReader(85),
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
    queue_dir = tmp_path / "queue"
    capture_returned_at = 100.0
    press_at = capture_returned_at + 2.5
    lock = threading.Lock()
    now = [0.0]
    slept: list[float] = []

    class TimedStorage(FakeStorageClient):
        markers_stored_when_segment_started: list[str] | None = None

        def upload(self, bucket, object_path, local_path):
            if local_path.suffix == ".mp4":
                self.markers_stored_when_segment_started = [
                    key for key in self.uploaded if key.endswith(".marker")
                ]
                advance(2.0)
            super().upload(bucket, object_path, local_path)

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
        with lock:
            now[0] = capture_returned_at
        stop.set()

    deps = _make_deps(
        tmp_path,
        command_runner=MuxTakesASecond(on_record=capture_returns),
        battery_reader=FakeBatteryReader(85),
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
    assert slept == [2.0]
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
        command_runner=FakeCommandRunner(
            on_record=lambda n: stop.set() if n >= 2 else None
        ),
        battery_reader=FakeBatteryReader(85),
        led_driver=FakeLedDriver(),
        storage_client=storage,
        flush_markers=flag_worker.flush_markers,
    )
    run_recording_loop(deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0)))
    flag_worker.submit(write_flag_marker(queue_dir, datetime(2026, 7, 4, 12, 0, 30)))
    flag_worker.stop()

    assert storage.marker_threads == {FLAG_UPLOAD_THREAD_NAME}
    assert list_pending(queue_dir) == []
