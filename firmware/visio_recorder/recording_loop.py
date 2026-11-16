import logging
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from visio_recorder.battery import BatteryReader, read_battery_status
from visio_recorder.led import LedDriver, LedState, apply_led_state
from visio_recorder.muxer import CommandRunner, mux_segment
from visio_recorder.upload_queue import enqueue, list_pending, mark_uploaded
from visio_recorder.uploader import StorageClient, upload_segment

_BYTES_PER_GB = 1024**3

UPLOAD_FAILURES_TO_CRITICAL = 3

_logger = logging.getLogger(__name__)


@dataclass
class DiskStats:
    used_gb: float
    free_gb: float


class DiskStatsReader(Protocol):
    def usage(self, path: Path) -> DiskStats: ...


class ShutilDiskStatsReader:
    def usage(self, path: Path) -> DiskStats:
        usage = shutil.disk_usage(path)
        return DiskStats(
            used_gb=usage.used / _BYTES_PER_GB,
            free_gb=usage.free / _BYTES_PER_GB,
        )


@dataclass
class DeviceStatus:
    device_id: str
    battery_pct: int
    storage_used_gb: float
    storage_free_gb: float
    segments_pending: int
    segments_uploaded_today: int
    recording_active: bool
    updated_at: str


class StatusClient(Protocol):
    def upsert_device_status(self, status: dict) -> None: ...


@dataclass
class SegmentResult:
    segments_uploaded_today: int
    upload_ok: bool


def next_led_state(battery_is_low: bool, is_uploading: bool) -> LedState:
    if battery_is_low:
        return LedState.LOW_BATTERY
    if is_uploading:
        return LedState.UPLOADING
    return LedState.RECORDING


def build_device_status(
    *,
    device_id: str,
    battery_pct: int,
    disk_stats_reader: DiskStatsReader,
    data_dir: Path,
    queue_dir: Path,
    segments_uploaded_today: int,
    recording_active: bool,
) -> DeviceStatus:
    stats = disk_stats_reader.usage(data_dir)
    return DeviceStatus(
        device_id=device_id,
        battery_pct=battery_pct,
        storage_used_gb=stats.used_gb,
        storage_free_gb=stats.free_gb,
        segments_pending=len(list_pending(queue_dir)),
        segments_uploaded_today=segments_uploaded_today,
        recording_active=recording_active,
        updated_at=datetime.now(UTC).isoformat(),
    )


def recover_orphaned_segments(
    data_dir: Path, queue_dir: Path, command_runner: CommandRunner, framerate: int
) -> list[Path]:
    """Queue segments an unclean stop left behind in ``data_dir``.

    Run once at boot, before recording starts, so no capture is in flight.
    ``flush_pending`` only scans ``queue_dir``, so without this a capture cut
    short (rpicam-vid killed, power lost, daemon crash) stayed in ``data_dir``
    forever: never muxed, never uploaded, never deleted. Three leftovers are
    possible, from the stages of ``on_segment_complete``:

    - a ``.h264`` alone (capture or mux never started): mux it and queue it;
    - a ``.h264`` plus a ``.mp4`` (mux interrupted): the ``.mp4`` may be
      truncated, so rebuild it from the ``.h264`` (ffmpeg ``-y`` overwrites);
    - a ``.mp4`` alone (crash between unlinking the ``.h264`` and enqueueing):
      the mux finished, so queue it as is.

    A capture ffmpeg cannot mux (e.g. killed before its first frame) is
    deleted, since keeping it would only leak the space this sweep reclaims.
    Returns the queued paths, oldest first.
    """
    recovered: list[Path] = []
    for h264_path in sorted(data_dir.glob("*.h264")):
        mp4_path = h264_path.with_suffix(".mp4")
        try:
            mp4_path = mux_segment(command_runner, h264_path, framerate)
        except Exception:
            _logger.exception(
                "could not mux orphaned capture %s; deleting it", h264_path
            )
            mp4_path.unlink(missing_ok=True)
            h264_path.unlink()
            continue
        h264_path.unlink()
        recovered.append(enqueue(queue_dir, mp4_path))
    for mp4_path in sorted(data_dir.glob("*.mp4")):
        recovered.append(enqueue(queue_dir, mp4_path))
    for path in recovered:
        _logger.warning("recovered orphaned segment %s", path.name)
    return sorted(recovered, key=lambda p: p.name)


def flush_pending(
    queue_dir: Path, storage_client: StorageClient, device_id: str
) -> int:
    uploaded = 0
    for path in list_pending(queue_dir):
        try:
            upload_segment(storage_client, device_id, path)
        except Exception:
            _logger.exception("flush_pending failed to upload queued segment %s", path)
            continue
        mark_uploaded(path)
        uploaded += 1
    return uploaded


def on_segment_complete(
    h264_path: Path,
    queue_dir: Path,
    command_runner: CommandRunner,
    storage_client: StorageClient,
    status_client: StatusClient,
    led_driver: LedDriver,
    battery_reader: BatteryReader,
    device_id: str,
    segments_uploaded_today: int,
    framerate: int,
    disk_stats_reader: DiskStatsReader,
    data_dir: Path,
) -> SegmentResult:
    mp4_path = mux_segment(command_runner, h264_path, framerate)
    h264_path.unlink()
    queued_path = enqueue(queue_dir, mp4_path)

    battery_status = read_battery_status(battery_reader)
    apply_led_state(
        led_driver, next_led_state(battery_status.is_low, is_uploading=True)
    )

    upload_ok = True
    try:
        upload_segment(storage_client, device_id, queued_path)
        mark_uploaded(queued_path)
        segments_uploaded_today += 1
    except Exception:
        upload_ok = False

    apply_led_state(
        led_driver, next_led_state(battery_status.is_low, is_uploading=False)
    )

    status = build_device_status(
        device_id=device_id,
        battery_pct=battery_status.pct,
        disk_stats_reader=disk_stats_reader,
        data_dir=data_dir,
        queue_dir=queue_dir,
        segments_uploaded_today=segments_uploaded_today,
        recording_active=True,
    )
    status_client.upsert_device_status(asdict(status))

    return SegmentResult(
        segments_uploaded_today=segments_uploaded_today, upload_ok=upload_ok
    )
