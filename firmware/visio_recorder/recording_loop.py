from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from visio_recorder.battery import BatteryReader, read_battery_status
from visio_recorder.led import LedDriver, LedState, apply_led_state
from visio_recorder.muxer import CommandRunner, mux_segment
from visio_recorder.upload_queue import enqueue, list_pending, mark_uploaded
from visio_recorder.uploader import StorageClient, upload_segment


@dataclass
class DeviceStatus:
    device_id: str
    battery_pct: int
    storage_used_gb: float
    storage_free_gb: float
    segments_pending: int
    segments_uploaded_today: int
    recording_active: bool


class StatusClient(Protocol):
    def upsert_device_status(self, status: dict) -> None: ...


def next_led_state(battery_is_low: bool, is_uploading: bool) -> LedState:
    if battery_is_low:
        return LedState.LOW_BATTERY
    if is_uploading:
        return LedState.UPLOADING
    return LedState.RECORDING


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
) -> int:
    mp4_path = mux_segment(command_runner, h264_path, framerate)
    h264_path.unlink()
    queued_path = enqueue(queue_dir, mp4_path)

    battery_status = read_battery_status(battery_reader)
    apply_led_state(
        led_driver, next_led_state(battery_status.is_low, is_uploading=True)
    )

    upload_segment(storage_client, device_id, queued_path)
    mark_uploaded(queued_path)
    segments_uploaded_today += 1

    apply_led_state(
        led_driver, next_led_state(battery_status.is_low, is_uploading=False)
    )

    # Real disk-usage stats (shutil.disk_usage) are wired in Epic 5 against
    # the actual SD card; there is no meaningful fake for a syscall here.
    status = DeviceStatus(
        device_id=device_id,
        battery_pct=battery_status.pct,
        storage_used_gb=0.0,
        storage_free_gb=0.0,
        segments_pending=len(list_pending(queue_dir)),
        segments_uploaded_today=segments_uploaded_today,
        recording_active=True,
    )
    status_client.upsert_device_status(asdict(status))

    return segments_uploaded_today
