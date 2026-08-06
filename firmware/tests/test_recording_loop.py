from datetime import datetime

from tests.fakes import (
    FailingStorageClient,
    FakeBatteryReader,
    FakeCommandRunner,
    FakeDiskStatsReader,
    FakeLedDriver,
    FakeStatusClient,
    FakeStorageClient,
    FakeStorageClientFailingOn,
)
from visio_recorder.led import LedPattern, LedState
from visio_recorder.recording_loop import (
    DiskStats,
    ShutilDiskStatsReader,
    flush_pending,
    next_led_state,
    on_segment_complete,
)
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
    )

    assert result.upload_ok is True
    assert result.segments_uploaded_today == 1


def test_a_successful_upload_drains_a_backlog_left_by_an_earlier_wifi_gap(tmp_path):
    # flush_pending used to run only at daemon startup, so segments stranded by
    # a WiFi gap during the day sat queued until the next restart - on a pendant
    # that is worn all day, potentially for the whole day. A segment uploading
    # successfully is itself the proof connectivity is back, so it is the moment
    # to drain, and it costs a directory listing when there is nothing to drain.
    h264_path = tmp_path / "raw" / "20260704_120000.h264"
    h264_path.parent.mkdir()
    h264_path.write_bytes(b"")
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir()
    stranded = ["20260704_100000.mp4", "20260704_110000.mp4"]
    for name in stranded:
        (queue_dir / name).touch()
    storage = FakeStorageClient()
    status_client = FakeStatusClient()

    result = on_segment_complete(
        h264_path=h264_path,
        queue_dir=queue_dir,
        command_runner=FakeCommandRunner(),
        storage_client=storage,
        status_client=status_client,
        led_driver=FakeLedDriver(),
        battery_reader=FakeBatteryReader(85),
        device_id="device-abc",
        segments_uploaded_today=0,
        framerate=30,
        disk_stats_reader=FakeDiskStatsReader(DiskStats(used_gb=0.0, free_gb=0.0)),
        data_dir=tmp_path,
    )

    assert result.upload_ok is True
    # The segment itself plus both stranded ones.
    assert result.segments_uploaded_today == 3
    assert sorted(storage.uploaded) == [
        "device-abc/20260704_100000.mp4",
        "device-abc/20260704_110000.mp4",
        "device-abc/20260704_120000.mp4",
    ]
    assert list_pending(queue_dir) == []
    # The status row the app's Device tab reads reports the drained queue, not
    # the backlog as it stood when the segment completed.
    assert status_client.upserts[0]["segments_pending"] == 0
    assert status_client.upserts[0]["segments_uploaded_today"] == 3


def test_a_failed_upload_leaves_the_backlog_alone(tmp_path):
    # The drain is gated on this segment succeeding, because that success is
    # the whole connectivity signal. Without the gate a daemon with no network
    # would re-attempt the entire backlog on every completed segment.
    h264_path = tmp_path / "raw" / "20260704_120000.h264"
    h264_path.parent.mkdir()
    h264_path.write_bytes(b"")
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir()
    (queue_dir / "20260704_100000.mp4").touch()
    storage = FakeStorageClientFailingOn("20260704_120000.mp4")

    result = on_segment_complete(
        h264_path=h264_path,
        queue_dir=queue_dir,
        command_runner=FakeCommandRunner(),
        storage_client=storage,
        status_client=FakeStatusClient(),
        led_driver=FakeLedDriver(),
        battery_reader=FakeBatteryReader(85),
        device_id="device-abc",
        segments_uploaded_today=0,
        framerate=30,
        disk_stats_reader=FakeDiskStatsReader(DiskStats(used_gb=0.0, free_gb=0.0)),
        data_dir=tmp_path,
    )

    assert result.upload_ok is False
    assert result.segments_uploaded_today == 0
    assert storage.uploaded == []
    assert [p.name for p in list_pending(queue_dir)] == [
        "20260704_100000.mp4",
        "20260704_120000.mp4",
    ]


def test_flush_pending_uploads_and_clears_all_queued_files(tmp_path):
    (tmp_path / "20260708_235500.mp4").touch()
    (tmp_path / "FLAG_20260708_235700.marker").touch()
    client = FakeStorageClient()

    uploaded = flush_pending(tmp_path, client, "device-abc")

    assert uploaded == 2
    assert list(tmp_path.iterdir()) == []


def test_flush_pending_leaves_failing_files_queued_and_continues(tmp_path):
    (tmp_path / "20260708_235500.mp4").touch()
    (tmp_path / "20260708_235000.mp4").touch()
    client = FakeStorageClientFailingOn("20260708_235000.mp4")

    uploaded = flush_pending(tmp_path, client, "device-abc")

    assert uploaded == 1
    assert [p.name for p in tmp_path.iterdir()] == ["20260708_235000.mp4"]
