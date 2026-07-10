import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from visio_recorder.battery import BatteryReader, read_battery_status
from visio_recorder.capture import record_segment
from visio_recorder.led import LedDriver, LedState, apply_led_state
from visio_recorder.muxer import CommandRunner
from visio_recorder.recording_loop import (
    UPLOAD_FAILURES_TO_CRITICAL,
    DiskStatsReader,
    StatusClient,
    on_segment_complete,
)
from visio_recorder.uploader import StorageClient


@dataclass
class StartupResult:
    proceed: bool
    battery_pct: int


def run_startup_sequence(
    battery_reader: BatteryReader, led_driver: LedDriver
) -> StartupResult:
    status = read_battery_status(battery_reader)
    if status.should_halt:
        apply_led_state(led_driver, LedState.CRITICAL)
        return StartupResult(proceed=False, battery_pct=status.pct)
    if status.is_low:
        apply_led_state(led_driver, LedState.LOW_BATTERY)
    else:
        apply_led_state(led_driver, LedState.RECORDING)
    return StartupResult(proceed=True, battery_pct=status.pct)


@dataclass
class LoopDeps:
    command_runner: CommandRunner
    storage_client: StorageClient
    status_client: StatusClient
    led_driver: LedDriver
    battery_reader: BatteryReader
    disk_stats_reader: DiskStatsReader
    data_dir: Path
    queue_dir: Path
    device_id: str
    segment_duration_ms: int
    framerate: int


def run_recording_loop(
    deps: LoopDeps,
    stop: threading.Event,
    clock: Callable[[], datetime] = datetime.now,
) -> None:
    """Record segments on the main thread; upload them on one background worker.

    The main thread only records and enqueues. A single worker thread owns all
    upload state (segment count, consecutive failures) and every LED call except
    the halt CRITICAL applied here at exit. Both the halt and stop paths drain
    the queue via a ``None`` sentinel before joining the worker; on halt, the
    CRITICAL LED is applied after the drain so it is the final LED state and
    cannot be overwritten by the drained segments' LED restores.
    """
    work: "queue.Queue[Path | None]" = queue.Queue()

    def worker() -> None:
        segments_uploaded_today = 0
        consecutive_failures = 0
        while True:
            item = work.get()
            if item is None:
                return
            result = on_segment_complete(
                h264_path=item,
                queue_dir=deps.queue_dir,
                command_runner=deps.command_runner,
                storage_client=deps.storage_client,
                status_client=deps.status_client,
                led_driver=deps.led_driver,
                battery_reader=deps.battery_reader,
                device_id=deps.device_id,
                segments_uploaded_today=segments_uploaded_today,
                framerate=deps.framerate,
                disk_stats_reader=deps.disk_stats_reader,
                data_dir=deps.data_dir,
            )
            segments_uploaded_today = result.segments_uploaded_today
            if result.upload_ok:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                # ">=" so a sustained outage re-applies CRITICAL after every
                # failure past the threshold; on_segment_complete has already
                # restored a normal LED state by this point.
                if consecutive_failures >= UPLOAD_FAILURES_TO_CRITICAL:
                    apply_led_state(deps.led_driver, LedState.CRITICAL)

    worker_thread = threading.Thread(target=worker)
    worker_thread.start()
    halted = False
    try:
        while True:
            battery_status = read_battery_status(deps.battery_reader)
            if battery_status.should_halt:
                halted = True
                break
            if stop.is_set():
                break
            h264_path = record_segment(
                deps.command_runner,
                deps.data_dir,
                clock(),
                deps.segment_duration_ms,
                deps.framerate,
            )
            work.put(h264_path)
    finally:
        work.put(None)
        worker_thread.join(timeout=5)
    if halted:
        apply_led_state(deps.led_driver, LedState.CRITICAL)
