import logging
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from visio_recorder.battery import BatteryReader, read_battery_status
from visio_recorder.led import LedDriver, LedState, apply_led_state
from visio_recorder.muxer import CommandRunner, mux_segment
from visio_recorder.upload_limit import UploadLimit, rejected_dir, upload_queued
from visio_recorder.upload_queue import enqueue, list_pending
from visio_recorder.uploader import StorageClient

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
    segments_rejected: int
    audio_active: bool
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
    audio_active: bool,
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
        segments_rejected=_count_rejected(queue_dir),
        audio_active=audio_active,
        updated_at=datetime.now(UTC).isoformat(),
    )


def _count_rejected(queue_dir: Path) -> int:
    directory = rejected_dir(queue_dir)
    if not directory.exists():
        return 0
    return sum(1 for p in directory.iterdir() if p.is_file())


def recover_orphaned_segments(
    data_dir: Path, queue_dir: Path, command_runner: CommandRunner, framerate: int
) -> list[Path]:
    """Queue segments an unclean stop left behind in ``data_dir``.

    Run once at boot, before recording starts, so no capture is in flight.
    ``flush_pending`` only scans ``queue_dir``, so without this a capture cut
    short (rpicam-vid killed, power lost, daemon crash) stayed in ``data_dir``
    forever: never muxed, never uploaded, never deleted. Three leftovers are
    possible, from the stages of ``on_segment_complete``:

    - a ``.h264`` (or, when recording audio, a ``.ts``) alone (capture or mux
      never started): mux it and queue it;
    - a ``.h264`` plus a ``.mp4`` (mux interrupted): the ``.mp4`` may be
      truncated, so rebuild it from the ``.h264`` (ffmpeg ``-y`` overwrites);
    - a ``.mp4`` alone (crash between unlinking the ``.h264`` and enqueueing):
      the mux finished, so queue it as is.

    When the mux fails, its partial ``.mp4`` is always removed to free space.
    The ``.h264`` is deleted only when it is zero bytes (provably no footage,
    e.g. killed before its first frame); a non-empty capture may have failed
    for a transient reason such as a full disk, so it is kept for the next
    boot and the remaining orphans are still processed.
    Returns the queued paths, oldest first.
    """
    recovered: list[Path] = []
    captures = [*data_dir.glob("*.h264"), *data_dir.glob("*.ts")]
    for h264_path in sorted(captures):
        mp4_path = h264_path.with_suffix(".mp4")
        try:
            mp4_path = mux_segment(command_runner, h264_path, framerate)
        except Exception:
            mp4_path.unlink(missing_ok=True)
            if h264_path.stat().st_size == 0:
                _logger.exception(
                    "could not mux empty orphaned capture %s; deleting it", h264_path
                )
                h264_path.unlink()
            else:
                _logger.exception(
                    "could not mux orphaned capture %s; keeping it for the next boot",
                    h264_path,
                )
            continue
        h264_path.unlink()
        recovered.append(enqueue(queue_dir, mp4_path))
    for mp4_path in sorted(data_dir.glob("*.mp4")):
        recovered.append(enqueue(queue_dir, mp4_path))
    for path in recovered:
        _logger.warning("recovered orphaned segment %s", path.name)
    return sorted(recovered, key=lambda p: p.name)


def flush_pending(
    queue_dir: Path,
    storage_client: StorageClient,
    device_id: str,
    upload_limit: UploadLimit,
) -> int:
    """Upload everything queued; returns how many files reached storage.

    A queued segment over the upload limit (for example one recorded by an
    older firmware with five-minute segments) is split first, so the count is
    of uploaded files, not of queue entries.
    """
    uploaded = 0
    for path in list_pending(queue_dir):
        try:
            outcome = upload_queued(path, storage_client, device_id, upload_limit)
        except Exception:
            _logger.exception("flush_pending failed to upload queued segment %s", path)
            continue
        uploaded += outcome.uploaded
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
    upload_limit: UploadLimit,
) -> SegmentResult:
    mp4_path = mux_segment(command_runner, h264_path, framerate)
    h264_path.unlink()
    queued_path = enqueue(queue_dir, mp4_path)

    battery_status = read_battery_status(battery_reader)
    apply_led_state(
        led_driver, next_led_state(battery_status.is_low, is_uploading=True)
    )

    outcome = upload_queued(queued_path, storage_client, device_id, upload_limit)
    segments_uploaded_today += outcome.uploaded
    # A rejected (unsplittable, oversized) segment is lost footage, so it
    # counts towards the Critical LED like a failed upload does.
    upload_ok = outcome.failed == 0 and outcome.rejected == 0

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
        segments_uploaded_today += flush_pending(
            queue_dir, storage_client, device_id, upload_limit
        )

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
        # Per segment: a .ts capture carries the microphone track.
        audio_active=h264_path.suffix == ".ts",
    )
    status_client.upsert_device_status(asdict(status))

    return SegmentResult(
        segments_uploaded_today=segments_uploaded_today, upload_ok=upload_ok
    )
