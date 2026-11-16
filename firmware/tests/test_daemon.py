import subprocess
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
    unlimited_upload_limit,
)
from visio_recorder.capture import AudioSettings, VideoSettings
from visio_recorder.daemon import (
    LoopDeps,
    load_config,
    resolve_audio,
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
