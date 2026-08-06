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

    if upload_ok:
        # This segment's upload succeeding *is* the connectivity signal, so it
        # is the cheapest correct moment to drain whatever an earlier WiFi gap
        # left behind. Without this, flush_pending runs only at daemon startup,
        # so a gap during the day strands its segments until the next restart.
        # Deliberately no connectivity watcher and no polling: on the common
        # path the queue is empty and this costs one directory listing, and the
        # backlog self-heals on the first segment after the network returns.
        #
        # Outside the try, and reading upload_ok rather than joining it:
        # flush_pending logs and swallows per-file failures, so a backlog that
        # is still unreachable must not turn this segment's success into the
        # failure that drives the CRITICAL LED.
        segments_uploaded_today += flush_pending(queue_dir, storage_client, device_id)

    apply_led_state(
        led_driver, next_led_state(battery_status.is_low, is_uploading=False)
    )

    stats = disk_stats_reader.usage(data_dir)
    status = DeviceStatus(
        device_id=device_id,
        battery_pct=battery_status.pct,
        storage_used_gb=stats.used_gb,
        storage_free_gb=stats.free_gb,
        segments_pending=len(list_pending(queue_dir)),
        segments_uploaded_today=segments_uploaded_today,
        recording_active=True,
        updated_at=datetime.now(UTC).isoformat(),
    )
    status_client.upsert_device_status(asdict(status))

    return SegmentResult(
        segments_uploaded_today=segments_uploaded_today, upload_ok=upload_ok
    )
