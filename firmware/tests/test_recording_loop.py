import subprocess
import threading
from datetime import datetime
from pathlib import Path

import pytest

from tests.fakes import (
    FailingSplitRunner,
    FailingStorageClient,
    FakeBatteryReader,
    FakeCaptureRunner,
    FakeClock,
    FakeCommandRunner,
    FakeDiskStatsReader,
    FakeDurationProbe,
    FakeLedDriver,
    FakeStatusClient,
    FakeStorageClient,
    FakeStorageClientFailingOn,
    SizeCheckedStorageClient,
    unlimited_upload_limit,
)
from visio_recorder.capture import CaptureExit, VideoSettings
from visio_recorder.daemon import LoopDeps, run_recording_loop
from visio_recorder.led import LedPattern, LedState
from visio_recorder.recording_loop import (
    RETRY_BASE_SEC,
    RETRY_MAX_SEC,
    UPLOAD_FAILURES_TO_CRITICAL,
    DiskStats,
    RetryBackoff,
    ShutilDiskStatsReader,
    UploadHealth,
    WorkerAction,
    WorkerStep,
    flush_pending,
    next_led_state,
    next_worker_step,
    on_segment_complete,
    pending_segments,
    recover_orphaned_segments,
    retry_pending_segments,
)
from visio_recorder.upload_limit import UploadLimit, rejected_dir
from visio_recorder.upload_queue import list_pending


def test_next_led_state_prioritizes_low_battery_over_uploading():
    assert (
        next_led_state(battery_is_low=True, is_uploading=True) == LedState.LOW_BATTERY
    )
    assert (
        next_led_state(battery_is_low=True, is_uploading=False) == LedState.LOW_BATTERY
    )


def test_next_led_state_shows_uploading_when_battery_is_healthy():
    assert next_led_state(battery_is_low=False, is_uploading=True) == LedState.UPLOADING


def test_next_led_state_shows_recording_when_idle_and_healthy():
    assert (
        next_led_state(battery_is_low=False, is_uploading=False) == LedState.RECORDING
    )


def test_next_led_state_lets_upload_critical_outrank_every_other_condition():
    for battery_is_low in (False, True):
        for is_uploading in (False, True):
            assert (
                next_led_state(
                    battery_is_low, is_uploading=is_uploading, upload_critical=True
                )
                == LedState.CRITICAL
            )


def test_upload_health_escalates_at_three_failures_and_stays_critical():
    # Snapshotted into a list rather than asserted in the loop: re-reading
    # health.is_critical after an assert narrows it for mypy, which then calls
    # the opposite assert unreachable.
    health = UploadHealth()
    escalated = []
    for failures in range(1, UPLOAD_FAILURES_TO_CRITICAL + 3):
        health.record(upload_ok=False)
        escalated.append((failures, health.is_critical))

    assert UPLOAD_FAILURES_TO_CRITICAL == 3
    assert escalated == [(1, False), (2, False), (3, True), (4, True), (5, True)]


def test_upload_health_is_cleared_by_one_successful_upload():
    health = UploadHealth(consecutive_failures=7)

    before = health.is_critical
    health.record(upload_ok=True)
    after = health.is_critical

    assert (before, after) == (True, False)
    assert health.consecutive_failures == 0


def test_on_segment_complete_holds_critical_red_across_a_failing_segment(tmp_path):
    # The LED bug end to end at the function level: a segment handled while
    # the run is already Critical must not write the upload blue or the
    # recording green at all.
    h264_path = tmp_path / "raw" / "20260704_120000.h264"
    h264_path.parent.mkdir()
    h264_path.write_bytes(b"")
    led = FakeLedDriver()
    health = UploadHealth(consecutive_failures=UPLOAD_FAILURES_TO_CRITICAL)

    result = on_segment_complete(
        h264_path=h264_path,
        queue_dir=tmp_path / "queue",
        command_runner=FakeCommandRunner(),
        storage_client=FailingStorageClient(),
        status_client=FakeStatusClient(),
        led_driver=led,
        battery_reader=FakeBatteryReader(85),
        device_id="device-abc",
        segments_uploaded_today=0,
        framerate=30,
        disk_stats_reader=FakeDiskStatsReader(DiskStats(used_gb=0.0, free_gb=0.0)),
        data_dir=tmp_path,
        upload_limit=unlimited_upload_limit(),
        upload_health=health,
    )

    assert result.upload_ok is False
    assert led.calls == [((255, 0, 0), LedPattern.FLASHING)] * 2
    assert health.consecutive_failures == UPLOAD_FAILURES_TO_CRITICAL + 1


def test_on_segment_complete_clears_critical_red_on_the_success_itself(tmp_path):
    # The clear rule from the spec: "any subsequent successful upload resets
    # the counter". The restore LED call must already see the reset, so the
    # green lands on the successful segment rather than one segment later.
    h264_path = tmp_path / "raw" / "20260704_120000.h264"
    h264_path.parent.mkdir()
    h264_path.write_bytes(b"")
    led = FakeLedDriver()
    health = UploadHealth(consecutive_failures=UPLOAD_FAILURES_TO_CRITICAL)

    on_segment_complete(
        h264_path=h264_path,
        queue_dir=tmp_path / "queue",
        command_runner=FakeCommandRunner(),
        storage_client=FakeStorageClient(),
        status_client=FakeStatusClient(),
        led_driver=led,
        battery_reader=FakeBatteryReader(85),
        device_id="device-abc",
        segments_uploaded_today=0,
        framerate=30,
        disk_stats_reader=FakeDiskStatsReader(DiskStats(used_gb=0.0, free_gb=0.0)),
        data_dir=tmp_path,
        upload_limit=unlimited_upload_limit(),
        upload_health=health,
    )

    assert led.calls == [
        ((255, 0, 0), LedPattern.FLASHING),  # still Critical while uploading
        ((0, 255, 0), LedPattern.SOLID),  # the success clears it
    ]
    assert not health.is_critical


def test_on_segment_complete_happy_path(tmp_path):
    h264_path = tmp_path / "raw" / "20260704_120000.h264"
    h264_path.parent.mkdir()
    h264_path.write_bytes(b"")
    queue_dir = tmp_path / "queue"
    led = FakeLedDriver()
    storage = FakeStorageClient()
    status_client = FakeStatusClient()
    command_runner = FakeCommandRunner()

    result = on_segment_complete(
        h264_path=h264_path,
        queue_dir=queue_dir,
        command_runner=command_runner,
        storage_client=storage,
        status_client=status_client,
        led_driver=led,
        battery_reader=FakeBatteryReader(85),
        device_id="device-abc",
        segments_uploaded_today=41,
        framerate=30,
        disk_stats_reader=FakeDiskStatsReader(DiskStats(used_gb=0.0, free_gb=0.0)),
        data_dir=tmp_path,
        upload_limit=unlimited_upload_limit(),
    )

    assert result.segments_uploaded_today == 42
    framerate_idx = command_runner.calls[0].index("-framerate") + 1
    assert command_runner.calls[0][framerate_idx] == "30"
    assert storage.uploaded == ["device-abc/20260704_120000.mp4"]
    assert not h264_path.exists()
    assert led.calls == [
        ((0, 0, 255), LedPattern.PULSING),
        ((0, 255, 0), LedPattern.SOLID),
    ]
    assert len(status_client.upserts) == 1
    upsert = dict(status_client.upserts[0])
    updated_at = upsert.pop("updated_at")
    assert datetime.fromisoformat(updated_at)
    assert upsert == {
        "device_id": "device-abc",
        "battery_pct": 85,
        "storage_used_gb": 0.0,
        "storage_free_gb": 0.0,
        "segments_pending": 0,
        "segments_uploaded_today": 42,
        "recording_active": True,
        "segments_rejected": 0,
        "audio_active": False,
    }


def test_on_segment_complete_with_low_battery_uses_low_battery_led(tmp_path):
    h264_path = tmp_path / "raw" / "20260704_120000.h264"
    h264_path.parent.mkdir()
    h264_path.write_bytes(b"")
    queue_dir = tmp_path / "queue"
    led = FakeLedDriver()

    on_segment_complete(
        h264_path=h264_path,
        queue_dir=queue_dir,
        command_runner=FakeCommandRunner(),
        storage_client=FakeStorageClient(),
        status_client=FakeStatusClient(),
        led_driver=led,
        battery_reader=FakeBatteryReader(15),
        device_id="device-abc",
        segments_uploaded_today=0,
        framerate=30,
        disk_stats_reader=FakeDiskStatsReader(DiskStats(used_gb=0.0, free_gb=0.0)),
        data_dir=tmp_path,
        upload_limit=unlimited_upload_limit(),
    )

    assert led.calls == [
        ((255, 255, 0), LedPattern.PULSING),
        ((255, 255, 0), LedPattern.PULSING),
    ]


def test_on_segment_complete_reports_real_disk_stats(tmp_path, monkeypatch):
    h264_path = tmp_path / "raw" / "20260704_120000.h264"
    h264_path.parent.mkdir()
    h264_path.write_bytes(b"")
    queue_dir = tmp_path / "queue"
    led = FakeLedDriver()
    storage = FakeStorageClient()
    status_client = FakeStatusClient()
    command_runner = FakeCommandRunner()

    on_segment_complete(
        h264_path=h264_path,
        queue_dir=queue_dir,
        command_runner=command_runner,
        storage_client=storage,
        status_client=status_client,
        led_driver=led,
        battery_reader=FakeBatteryReader(85),
        device_id="device-abc",
        segments_uploaded_today=41,
        framerate=30,
        disk_stats_reader=FakeDiskStatsReader(DiskStats(used_gb=3.5, free_gb=25.1)),
        data_dir=tmp_path,
        upload_limit=unlimited_upload_limit(),
    )

    assert len(status_client.upserts) == 1
    upsert = dict(status_client.upserts[0])
    updated_at = upsert.pop("updated_at")
    assert datetime.fromisoformat(updated_at)
    assert upsert == {
        "device_id": "device-abc",
        "battery_pct": 85,
        "storage_used_gb": 3.5,
        "storage_free_gb": 25.1,
        "segments_pending": 0,
        "segments_uploaded_today": 42,
        "recording_active": True,
        "segments_rejected": 0,
        "audio_active": False,
    }


def test_shutil_disk_stats_reader_converts_bytes_to_gb(tmp_path):
    stats = ShutilDiskStatsReader().usage(tmp_path)
    assert stats.used_gb >= 0.0
    assert stats.free_gb > 0.0


def test_failed_upload_restores_led_keeps_queued_file_and_reports_failure(tmp_path):
    h264_path = tmp_path / "raw" / "20260704_120000.h264"
    h264_path.parent.mkdir()
    h264_path.write_bytes(b"")
    queue_dir = tmp_path / "queue"
    led = FakeLedDriver()
    storage = FailingStorageClient()
    status_client = FakeStatusClient()
    command_runner = FakeCommandRunner()

    result = on_segment_complete(
        h264_path=h264_path,
        queue_dir=queue_dir,
        command_runner=command_runner,
        storage_client=storage,
        status_client=status_client,
        led_driver=led,
        battery_reader=FakeBatteryReader(85),
        device_id="device-abc",
        segments_uploaded_today=0,
        framerate=30,
        disk_stats_reader=FakeDiskStatsReader(DiskStats(used_gb=0.0, free_gb=0.0)),
        data_dir=tmp_path,
        upload_limit=unlimited_upload_limit(),
    )

    assert result.upload_ok is False
    assert result.segments_uploaded_today == 0
    assert len(list(queue_dir.iterdir())) == 1
    assert led.calls[-1] == ((0, 255, 0), LedPattern.SOLID)
    assert len(status_client.upserts) == 1
    upsert = dict(status_client.upserts[0])
    updated_at = upsert.pop("updated_at")
    assert datetime.fromisoformat(updated_at)
    assert upsert == {
        "device_id": "device-abc",
        "battery_pct": 85,
        "storage_used_gb": 0.0,
        "storage_free_gb": 0.0,
        "segments_pending": 1,
        "segments_uploaded_today": 0,
        "recording_active": True,
        "segments_rejected": 0,
        "audio_active": False,
    }


def test_successful_upload_returns_ok_result(tmp_path):
    h264_path = tmp_path / "raw" / "20260704_120000.h264"
    h264_path.parent.mkdir()
    h264_path.write_bytes(b"")
    queue_dir = tmp_path / "queue"
    led = FakeLedDriver()
    storage = FakeStorageClient()
    status_client = FakeStatusClient()
    command_runner = FakeCommandRunner()

    result = on_segment_complete(
        h264_path=h264_path,
        queue_dir=queue_dir,
        command_runner=command_runner,
        storage_client=storage,
        status_client=status_client,
        led_driver=led,
        battery_reader=FakeBatteryReader(85),
        device_id="device-abc",
        segments_uploaded_today=0,
        framerate=30,
        disk_stats_reader=FakeDiskStatsReader(DiskStats(used_gb=0.0, free_gb=0.0)),
        data_dir=tmp_path,
        upload_limit=unlimited_upload_limit(),
    )

    assert result.upload_ok is True
    assert result.segments_uploaded_today == 1


def test_flush_pending_uploads_and_clears_all_queued_files(tmp_path):
    (tmp_path / "20260708_235500.mp4").touch()
    (tmp_path / "FLAG_20260708_235700.marker").touch()
    client = FakeStorageClient()

    uploaded = flush_pending(tmp_path, client, "device-abc", unlimited_upload_limit())

    assert uploaded == 2
    assert list(tmp_path.iterdir()) == []


def test_flush_pending_leaves_failing_files_queued_and_continues(tmp_path):
    (tmp_path / "20260708_235500.mp4").touch()
    (tmp_path / "20260708_235000.mp4").touch()
    client = FakeStorageClientFailingOn("20260708_235000.mp4")

    uploaded = flush_pending(tmp_path, client, "device-abc", unlimited_upload_limit())

    assert uploaded == 1
    assert [p.name for p in tmp_path.iterdir()] == ["20260708_235000.mp4"]


class _MuxFailingRunner(FakeCommandRunner):
    """Fails the ffmpeg mux for one named input."""

    def __init__(self, failing_input: str) -> None:
        super().__init__()
        self._failing_input = failing_input

    def run(self, args: list[str]) -> None:
        if "ffmpeg" in args and Path(args[args.index("-i") + 1]).name == (
            self._failing_input
        ):
            with self._lock:
                self.calls.append(args)
            raise subprocess.CalledProcessError(1, args)
        super().run(args)


def test_recover_orphaned_segments_muxes_and_queues_a_leftover_h264(tmp_path):
    # An unclean stop (rpicam-vid killed mid-capture) leaves the raw capture in
    # data_dir; nothing else ever looks there, so it leaked SD space forever.
    data_dir = tmp_path / "data"
    queue_dir = data_dir / "queue"
    data_dir.mkdir()
    (data_dir / "20260803_120001.h264").write_bytes(b"partial capture")

    recovered = recover_orphaned_segments(
        data_dir, queue_dir, FakeCommandRunner(), framerate=30
    )

    assert [p.name for p in recovered] == ["20260803_120001.mp4"]
    assert [p.name for p in list_pending(queue_dir)] == ["20260803_120001.mp4"]
    assert (queue_dir / "20260803_120001.mp4").read_bytes() == b"partial capture"
    assert not (data_dir / "20260803_120001.h264").exists()


def test_recover_orphaned_segments_remuxes_over_a_half_written_mp4(tmp_path):
    # Interrupted mid-mux: both files exist and the .mp4 may be truncated, so
    # it is rebuilt from the .h264 rather than trusted.
    data_dir = tmp_path / "data"
    queue_dir = data_dir / "queue"
    data_dir.mkdir()
    (data_dir / "20260803_120001.h264").write_bytes(b"full capture")
    (data_dir / "20260803_120001.mp4").write_bytes(b"trunc")

    recover_orphaned_segments(data_dir, queue_dir, FakeCommandRunner(), framerate=30)

    assert (queue_dir / "20260803_120001.mp4").read_bytes() == b"full capture"
    assert sorted(p.name for p in data_dir.iterdir()) == ["queue"]


def test_recover_orphaned_segments_queues_a_finished_mp4_left_in_data_dir(tmp_path):
    # Crash between unlinking the .h264 and enqueueing the finished .mp4.
    data_dir = tmp_path / "data"
    queue_dir = data_dir / "queue"
    data_dir.mkdir()
    (data_dir / "20260803_120001.mp4").write_bytes(b"muxed")
    runner = FakeCommandRunner()

    recover_orphaned_segments(data_dir, queue_dir, runner, framerate=30)

    assert (queue_dir / "20260803_120001.mp4").read_bytes() == b"muxed"
    assert runner.calls == []


def test_recover_orphaned_segments_deletes_an_empty_unmuxable_capture(tmp_path):
    # A zero-byte capture (killed before its first frame) cannot be muxed and
    # holds no footage; keeping it would retry and leak forever. The other
    # orphan is still recovered.
    data_dir = tmp_path / "data"
    queue_dir = data_dir / "queue"
    data_dir.mkdir()
    (data_dir / "20260803_120001.h264").write_bytes(b"")
    (data_dir / "20260803_120001.mp4").write_bytes(b"")
    (data_dir / "20260803_120501.h264").write_bytes(b"ok")

    recovered = recover_orphaned_segments(
        data_dir,
        queue_dir,
        _MuxFailingRunner("20260803_120001.h264"),
        framerate=30,
    )

    assert [p.name for p in recovered] == ["20260803_120501.mp4"]
    assert [p.name for p in list_pending(queue_dir)] == ["20260803_120501.mp4"]
    assert sorted(p.name for p in data_dir.iterdir()) == ["queue"]


def test_recover_orphaned_segments_keeps_a_non_empty_capture_whose_mux_fails(
    tmp_path,
):
    # A mux can fail transiently (e.g. a full SD card at boot); a non-empty
    # capture is real footage, so it stays for the next boot while its partial
    # .mp4 is removed to free space. The other orphan is still recovered.
    data_dir = tmp_path / "data"
    queue_dir = data_dir / "queue"
    data_dir.mkdir()
    (data_dir / "20260803_120001.h264").write_bytes(b"real footage")
    (data_dir / "20260803_120001.mp4").write_bytes(b"trunc")
    (data_dir / "20260803_120501.h264").write_bytes(b"ok")

    recovered = recover_orphaned_segments(
        data_dir,
        queue_dir,
        _MuxFailingRunner("20260803_120001.h264"),
        framerate=30,
    )

    assert [p.name for p in recovered] == ["20260803_120501.mp4"]
    assert [p.name for p in list_pending(queue_dir)] == ["20260803_120501.mp4"]
    assert sorted(p.name for p in data_dir.iterdir()) == [
        "20260803_120001.h264",
        "queue",
    ]
    assert (data_dir / "20260803_120001.h264").read_bytes() == b"real footage"


def test_recover_orphaned_segments_leaves_device_state_files_alone(tmp_path):
    data_dir = tmp_path / "data"
    queue_dir = data_dir / "queue"
    queue_dir.mkdir(parents=True)
    for name in ("session.json", "device.json", "device_registered"):
        (data_dir / name).write_text("x")
    (queue_dir / "20260803_115500.mp4").write_bytes(b"queued")

    recovered = recover_orphaned_segments(
        data_dir, queue_dir, FakeCommandRunner(), framerate=30
    )

    assert recovered == []
    assert sorted(p.name for p in data_dir.iterdir()) == [
        "device.json",
        "device_registered",
        "queue",
        "session.json",
    ]
    assert [p.name for p in list_pending(queue_dir)] == ["20260803_115500.mp4"]


def test_capture_killed_mid_segment_is_uploaded_on_the_next_boot(tmp_path):
    # The report's probe 2, end to end through the real loop: systemd SIGTERMs
    # rpicam-vid mid-capture, it leaves a partial .h264 and exits non-zero, and
    # the exception escapes run_recording_loop. The next boot's recovery plus
    # flush_pending must upload that partial segment instead of leaking it.
    data_dir = tmp_path / "data"
    queue_dir = data_dir / "queue"

    class KilledOnSecondCapture(FakeCaptureRunner):
        def run(self, args: list[str], cancel: threading.Event) -> CaptureExit:
            if self.record_count == 1:
                output = Path(args[args.index("-o") + 1])
                output.write_bytes(b"partial frames")
                raise subprocess.CalledProcessError(-15, args)
            return super().run(args, cancel)

    storage = FakeStorageClient()
    deps = LoopDeps(
        command_runner=FakeCommandRunner(),
        capture_runner=KilledOnSecondCapture(),
        storage_client=storage,
        status_client=FakeStatusClient(),
        led_driver=FakeLedDriver(),
        battery_reader=FakeBatteryReader(85),
        disk_stats_reader=FakeDiskStatsReader(DiskStats(used_gb=1.0, free_gb=9.0)),
        data_dir=data_dir,
        queue_dir=queue_dir,
        device_id="device-abc",
        segment_duration_ms=1000,
        video=VideoSettings(),
        upload_limit=unlimited_upload_limit(),
    )
    with pytest.raises(subprocess.CalledProcessError):
        run_recording_loop(
            deps, threading.Event(), clock=FakeClock(datetime(2026, 8, 3, 12, 0, 0))
        )
    assert (data_dir / "20260803_120001.h264").exists()

    recover_orphaned_segments(data_dir, queue_dir, FakeCommandRunner(), framerate=30)
    flushed = flush_pending(queue_dir, storage, "device-abc", unlimited_upload_limit())

    assert flushed == 1
    assert storage.uploaded == [
        "device-abc/20260803_120000.mp4",
        "device-abc/20260803_120001.mp4",
    ]
    assert sorted(p.name for p in data_dir.iterdir()) == ["queue"]
    assert list_pending(queue_dir) == []


class _MuxThenSplitRunner(FakeCommandRunner):
    """Muxes like FakeCommandRunner; answers the split with two equal parts."""

    def __init__(self, fail_split: bool = False) -> None:
        super().__init__()
        self.fail_split = fail_split

    def run(self, args: list[str]) -> None:
        if "segment" not in args:
            super().run(args)
            return
        with self._lock:
            self.calls.append(args)
        if self.fail_split:
            raise subprocess.CalledProcessError(1, args)
        source = Path(args[args.index("-i") + 1])
        half = source.stat().st_size // 2
        pattern = Path(args[-1])
        csv_path = Path(args[args.index("-segment_list") + 1])
        for index in range(2):
            (pattern.parent / (pattern.name % index)).write_bytes(b"x" * half)
        csv_path.write_text("part_000.mp4,0.0,30.0\npart_001.mp4,30.0,60.0\n")


class _Probe:
    def duration_sec(self, path: Path) -> float:
        return 60.0


def _oversized_segment(tmp_path: Path) -> Path:
    h264_path = tmp_path / "raw" / "20261116_090000.h264"
    h264_path.parent.mkdir()
    h264_path.write_bytes(b"x" * 1500)
    return h264_path


def test_on_segment_complete_splits_an_oversized_segment_and_uploads_both_parts(
    tmp_path,
):
    runner = _MuxThenSplitRunner()
    storage = FakeStorageClient()
    status_client = FakeStatusClient()

    result = on_segment_complete(
        h264_path=_oversized_segment(tmp_path),
        queue_dir=tmp_path / "queue",
        command_runner=runner,
        storage_client=storage,
        status_client=status_client,
        led_driver=FakeLedDriver(),
        battery_reader=FakeBatteryReader(85),
        device_id="device-abc",
        segments_uploaded_today=0,
        framerate=30,
        disk_stats_reader=FakeDiskStatsReader(DiskStats(used_gb=0.0, free_gb=0.0)),
        data_dir=tmp_path,
        upload_limit=UploadLimit(max_bytes=1000, runner=runner, probe=_Probe()),
    )

    assert result.upload_ok is True
    assert result.segments_uploaded_today == 2
    assert storage.uploaded == [
        "device-abc/20261116_090000.mp4",
        "device-abc/20261116_090030.mp4",
    ]
    assert status_client.upserts[-1]["segments_pending"] == 0
    assert status_client.upserts[-1]["segments_rejected"] == 0


def test_an_unsplittable_oversized_segment_is_reported_and_not_retried(tmp_path):
    runner = _MuxThenSplitRunner(fail_split=True)
    storage = FakeStorageClient()
    status_client = FakeStatusClient()
    queue_dir = tmp_path / "queue"
    limit = UploadLimit(max_bytes=1000, runner=runner, probe=_Probe())

    result = on_segment_complete(
        h264_path=_oversized_segment(tmp_path),
        queue_dir=queue_dir,
        command_runner=runner,
        storage_client=storage,
        status_client=status_client,
        led_driver=FakeLedDriver(),
        battery_reader=FakeBatteryReader(85),
        device_id="device-abc",
        segments_uploaded_today=0,
        framerate=30,
        disk_stats_reader=FakeDiskStatsReader(DiskStats(used_gb=0.0, free_gb=0.0)),
        data_dir=tmp_path,
        upload_limit=limit,
    )

    assert result.upload_ok is False
    assert storage.uploaded == []
    assert status_client.upserts[-1]["segments_pending"] == 0
    assert status_client.upserts[-1]["segments_rejected"] == 1
    # The next boot's flush has nothing to retry.
    assert flush_pending(queue_dir, storage, "device-abc", limit) == 0
    assert storage.uploaded == []


def test_flush_pending_splits_an_oversized_queued_segment(tmp_path):
    # A segment queued by firmware that recorded five-minute segments.
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir()
    (queue_dir / "20261116_090000.mp4").write_bytes(b"x" * 1500)
    runner = _MuxThenSplitRunner()
    storage = FakeStorageClient()

    uploaded = flush_pending(
        queue_dir,
        storage,
        "device-abc",
        UploadLimit(max_bytes=1000, runner=runner, probe=_Probe()),
    )

    assert uploaded == 2
    assert storage.uploaded == [
        "device-abc/20261116_090000.mp4",
        "device-abc/20261116_090030.mp4",
    ]
    assert list_pending(queue_dir) == []


def test_on_segment_complete_muxes_an_audio_capture_and_reports_audio_active(
    tmp_path,
):
    ts_path = tmp_path / "raw" / "20261116_090000.ts"
    ts_path.parent.mkdir()
    ts_path.write_bytes(b"ts")
    runner = FakeCommandRunner()
    storage = FakeStorageClient()
    status_client = FakeStatusClient()

    on_segment_complete(
        h264_path=ts_path,
        queue_dir=tmp_path / "queue",
        command_runner=runner,
        storage_client=storage,
        status_client=status_client,
        led_driver=FakeLedDriver(),
        battery_reader=FakeBatteryReader(85),
        device_id="device-abc",
        segments_uploaded_today=0,
        framerate=30,
        disk_stats_reader=FakeDiskStatsReader(DiskStats(used_gb=0.0, free_gb=0.0)),
        data_dir=tmp_path,
        upload_limit=unlimited_upload_limit(),
    )

    assert "-framerate" not in runner.calls[0]
    assert storage.uploaded == ["device-abc/20261116_090000.mp4"]
    assert not ts_path.exists()
    assert status_client.upserts[-1]["audio_active"] is True


def test_recover_orphaned_segments_remuxes_a_leftover_transport_stream(tmp_path):
    # An audio+video capture killed mid-segment leaves a .ts, which (unlike a
    # half-written MP4) is still readable and remuxes like a raw .h264.
    data_dir = tmp_path / "data"
    queue_dir = data_dir / "queue"
    data_dir.mkdir()
    (data_dir / "20261116_090000.ts").write_bytes(b"partial ts")

    recovered = recover_orphaned_segments(
        data_dir, queue_dir, FakeCommandRunner(), framerate=30
    )

    assert [p.name for p in recovered] == ["20261116_090000.mp4"]
    assert not (data_dir / "20261116_090000.ts").exists()


def test_retry_backoff_delays_increase_and_stay_bounded():
    backoff = RetryBackoff()
    delays = [backoff.delay_sec(n) for n in range(0, 12)]

    # A queue inherited from the startup flush (no failures yet) waits the
    # base delay, same as the first failure.
    assert delays[0] == delays[1] == RETRY_BASE_SEC
    # Doubling per failure from there, then flat at the cap and never past it.
    assert delays[1:6] == [30.0, 60.0, 120.0, 240.0, 480.0]
    assert delays[5:] == [RETRY_MAX_SEC] * len(delays[5:])
    assert delays == sorted(delays)
    assert max(delays) == RETRY_MAX_SEC
    # Bounded on both ends: a wait is always long enough to be a backoff.
    assert min(delays) == RETRY_BASE_SEC > 0


def test_retry_backoff_is_configurable_for_tests_and_tuning():
    backoff = RetryBackoff(base_sec=0.5, max_sec=1.5)

    assert [backoff.delay_sec(n) for n in range(1, 5)] == [0.5, 1.0, 1.5, 1.5]


def test_pending_segments_excludes_flag_markers(tmp_path):
    # Flag markers belong to FlagUploadWorker while the daemon runs; retrying
    # them here as well would race it and manufacture spurious failures.
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir()
    (queue_dir / "20260704_120000.mp4").write_bytes(b"segment")
    (queue_dir / "FLAG_20260704_120005.marker").touch()

    assert [p.name for p in list_pending(queue_dir)] == [
        "20260704_120000.mp4",
        "FLAG_20260704_120005.marker",
    ]
    assert [p.name for p in pending_segments(queue_dir)] == ["20260704_120000.mp4"]


def _step(*, pending=True, stopping=False, retry_at=None, now=100.0, failures=0):
    return next_worker_step(
        segments_pending=pending,
        stopping=stopping,
        retry_at=retry_at,
        now=now,
        retry_backoff=RetryBackoff(base_sec=30.0, max_sec=480.0),
        consecutive_failures=failures,
    )


def test_the_worker_waits_for_a_segment_with_nothing_queued():
    assert _step(pending=False, retry_at=50.0) == WorkerStep(
        WorkerAction.WAIT_FOR_SEGMENT, retry_at=None
    )


def test_a_stopping_worker_never_starts_a_retry_round_even_with_a_lapsed_deadline():
    # The round would be cancelled before its first file; the worker goes
    # straight to collecting the shutdown sentinel instead.
    assert _step(stopping=True, retry_at=50.0, now=100.0) == WorkerStep(
        WorkerAction.WAIT_FOR_SEGMENT, retry_at=None
    )


def test_a_newly_queued_backlog_arms_the_deadline_from_the_live_failure_count():
    assert _step(retry_at=None, now=100.0, failures=3) == WorkerStep(
        WorkerAction.WAIT_UNTIL_RETRY, retry_at=220.0
    )


def test_an_armed_deadline_is_kept_rather_than_restarted():
    # A segment arriving wakes the worker before the deadline; the next step
    # keeps the same deadline, whatever the failure count has grown to, so
    # the wait shrinks instead of starting over.
    assert _step(retry_at=150.0, now=120.0, failures=5) == WorkerStep(
        WorkerAction.WAIT_UNTIL_RETRY, retry_at=150.0
    )


def test_a_passed_deadline_retries_now():
    assert _step(retry_at=150.0, now=150.0).action is WorkerAction.RETRY_NOW
    assert _step(retry_at=150.0, now=999.0).action is WorkerAction.RETRY_NOW


def _queued(queue_dir: Path, *names: str) -> None:
    queue_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (queue_dir / name).write_bytes(b"segment")


def _retry(queue_dir: Path, storage, health, led=None, limit=None, cancel=None):
    return retry_pending_segments(
        queue_dir=queue_dir,
        storage_client=storage,
        led_driver=led or FakeLedDriver(),
        battery_reader=FakeBatteryReader(85),
        device_id="device-abc",
        upload_limit=limit or unlimited_upload_limit(),
        upload_health=health,
        cancel=cancel,
    )


def test_retry_pending_segments_drains_the_queue_and_clears_critical(tmp_path):
    queue_dir = tmp_path / "queue"
    _queued(queue_dir, "20260704_120000.mp4", "20260704_120001.mp4")
    storage = FakeStorageClient()
    health = UploadHealth(consecutive_failures=UPLOAD_FAILURES_TO_CRITICAL)
    led = FakeLedDriver()

    outcome = _retry(queue_dir, storage, health, led=led)

    assert (outcome.uploaded, outcome.failed, outcome.rejected) == (2, 0, 0)
    assert list_pending(queue_dir) == []
    assert not health.is_critical
    # Red while the round runs, green once the success has reset the counter.
    assert led.calls == [
        ((255, 0, 0), LedPattern.FLASHING),
        ((0, 255, 0), LedPattern.SOLID),
    ]


def test_retry_failures_feed_the_same_counter_that_drives_critical(tmp_path):
    # The round is one more consecutive failure, exactly like a segment's own
    # failed upload, so a device whose retries keep failing still goes red.
    queue_dir = tmp_path / "queue"
    _queued(queue_dir, "20260704_120000.mp4")
    health = UploadHealth(consecutive_failures=UPLOAD_FAILURES_TO_CRITICAL - 1)
    led = FakeLedDriver()

    outcome = _retry(queue_dir, FailingStorageClient(), health, led=led)

    assert (outcome.uploaded, outcome.failed) == (0, 1)
    assert health.consecutive_failures == UPLOAD_FAILURES_TO_CRITICAL
    assert led.calls[-1] == ((255, 0, 0), LedPattern.FLASHING)
    assert list_pending(queue_dir) != []


def test_retry_pending_segments_on_an_empty_queue_leaves_critical_latched(tmp_path):
    # Nothing reached storage, so nothing may clear the red - otherwise a
    # no-op round would report success and un-escalate a real outage.
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir()
    (queue_dir / "FLAG_20260704_120005.marker").touch()
    health = UploadHealth(consecutive_failures=UPLOAD_FAILURES_TO_CRITICAL)
    led = FakeLedDriver()
    storage = FakeStorageClient()

    outcome = _retry(queue_dir, storage, health, led=led)

    assert (outcome.uploaded, outcome.failed, outcome.rejected) == (0, 0, 0)
    assert health.consecutive_failures == UPLOAD_FAILURES_TO_CRITICAL
    assert storage.uploaded == []
    assert led.calls == []


def test_a_retry_round_cancelled_before_its_first_file_leaves_critical_latched(
    tmp_path,
):
    # The same rule as the empty queue, reached through cancel: a round that
    # tried nothing must not report "nothing failed" and clear the red.
    queue_dir = tmp_path / "queue"
    _queued(queue_dir, "20260704_120000.mp4")
    health = UploadHealth(consecutive_failures=UPLOAD_FAILURES_TO_CRITICAL)
    led = FakeLedDriver()
    storage = FakeStorageClient()

    outcome = _retry(queue_dir, storage, health, led=led, cancel=lambda: True)

    assert (outcome.uploaded, outcome.failed, outcome.rejected) == (0, 0, 0)
    assert outcome.attempted == 0
    assert health.consecutive_failures == UPLOAD_FAILURES_TO_CRITICAL
    assert health.is_critical
    assert storage.uploaded == []
    assert led.calls == []
    assert [p.name for p in pending_segments(queue_dir)] == ["20260704_120000.mp4"]


def test_retry_moves_an_unsplittable_oversized_segment_aside_exactly_once(tmp_path):
    # The 50 MB limit cannot become a retry loop: the first round moves a
    # segment that will not fit and cannot be split to rejected/ beside the
    # queue, where device_status.segments_rejected counts it for the app and
    # nothing ever retries it again. It is kept on the card for manual
    # recovery, not deleted.
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir(parents=True)
    (queue_dir / "20260704_120000.mp4").write_bytes(b"x" * 4096)
    storage = FakeStorageClient()
    health = UploadHealth()
    # An ffmpeg that cannot split: the segment can never be made to fit.
    limit = UploadLimit(
        max_bytes=1024,
        runner=FailingSplitRunner(),
        probe=FakeDurationProbe(60.0),
    )

    first = _retry(queue_dir, storage, health, limit=limit)
    second = _retry(queue_dir, storage, health, limit=limit)

    assert (first.uploaded, first.failed, first.rejected) == (0, 0, 1)
    # Nothing left to retry, so the second round is a no-op, not a rejection.
    assert (second.uploaded, second.failed, second.rejected) == (0, 0, 0)
    assert pending_segments(queue_dir) == []
    assert [p.name for p in rejected_dir(queue_dir).iterdir()] == [
        "20260704_120000.mp4"
    ]
    assert storage.uploaded == []


def test_retry_after_a_crash_between_upload_and_unlink_is_idempotent(tmp_path):
    # The overlap the firstmate analysis flagged: the storage adapter sends
    # x-upsert false, so a segment that reached storage before a crash left a
    # queue entry whose every retry could fail on the duplicate. main already
    # closes this by checking the stored object's size first and treating a
    # byte-identical object as uploaded; this pins that the in-process retry
    # inherits it and clears the queue entry rather than retrying forever.
    queue_dir = tmp_path / "queue"
    _queued(queue_dir, "20260704_120000.mp4")
    storage = SizeCheckedStorageClient()
    # The pre-crash upload: object in storage, file still queued.
    storage.upload(
        "segments", "device-abc/20260704_120000.mp4", queue_dir / "20260704_120000.mp4"
    )
    health = UploadHealth(consecutive_failures=UPLOAD_FAILURES_TO_CRITICAL)

    outcome = _retry(queue_dir, storage, health)

    assert (outcome.uploaded, outcome.failed, outcome.rejected) == (1, 0, 0)
    assert list_pending(queue_dir) == []
    assert not health.is_critical
    assert storage.bodies_sent == 1


def test_retry_refuses_to_overwrite_a_different_file_under_an_existing_key(tmp_path):
    # The other half of that contract: object keys are second-resolution local
    # timestamps, so a clock step back can name a new segment after one
    # already stored. That must stay queued and count as a failure, never
    # replace the earlier recording.
    queue_dir = tmp_path / "queue"
    _queued(queue_dir, "20260704_120000.mp4")
    storage = SizeCheckedStorageClient()
    storage.stored["device-abc/20260704_120000.mp4"] = 999_999
    health = UploadHealth()

    outcome = _retry(queue_dir, storage, health)

    assert (outcome.uploaded, outcome.failed, outcome.rejected) == (0, 1, 0)
    assert storage.stored["device-abc/20260704_120000.mp4"] == 999_999
    assert [p.name for p in list_pending(queue_dir)] == ["20260704_120000.mp4"]
    assert health.consecutive_failures == 1


def test_a_cancelled_retry_round_leaves_the_rest_of_the_backlog_queued(tmp_path):
    # A battery halt or a stop signal during a long drain must not keep
    # uploading on a dying battery; whatever is left stays queued.
    queue_dir = tmp_path / "queue"
    _queued(queue_dir, *[f"2026070{n}_120000.mp4" for n in range(1, 5)])
    storage = FakeStorageClient()
    health = UploadHealth()

    outcome = _retry(
        queue_dir,
        storage,
        health,
        cancel=lambda: len(storage.uploaded) >= 2,
    )

    assert outcome.uploaded == 2
    assert len(pending_segments(queue_dir)) == 2
