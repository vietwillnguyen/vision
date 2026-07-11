import os
import queue
import signal
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from visio_recorder.battery import BatteryReader, read_battery_status
from visio_recorder.capture import record_segment
from visio_recorder.device_identity import load_or_create_device_id, register_device
from visio_recorder.drivers import PiJuiceBatteryReader, Ws2812LedDriver
from visio_recorder.led import LedDriver, LedState, apply_led_state
from visio_recorder.muxer import CommandRunner, SubprocessCommandRunner
from visio_recorder.onboarding import RpicamZbarScanner, run_onboarding
from visio_recorder.recording_loop import (
    UPLOAD_FAILURES_TO_CRITICAL,
    DiskStatsReader,
    ShutilDiskStatsReader,
    StatusClient,
    flush_pending,
    on_segment_complete,
)
from visio_recorder.supabase_clients import build_supabase_clients
from visio_recorder.uploader import StorageClient

_DEFAULT_DATA_DIR = "/var/lib/visio-recorder"
_DEFAULT_SEGMENT_DURATION_MS = "300000"
_DEFAULT_FRAMERATE = "30"
_ONBOARDING_MAX_ATTEMPTS = 60
_NM_KEYFILE_PATH = Path("/etc/NetworkManager/system-connections/visio.nmconnection")
_DEVICE_NAME = "visio-pendant"


@dataclass
class DaemonConfig:
    supabase_url: str
    supabase_anon_key: str
    data_dir: Path
    segment_duration_ms: int
    framerate: int


def load_config(env: Mapping[str, str]) -> DaemonConfig:
    for key in ("SUPABASE_URL", "SUPABASE_ANON_KEY"):
        if key not in env:
            raise ValueError(f"missing required environment variable: {key}")
    return DaemonConfig(
        supabase_url=env["SUPABASE_URL"],
        supabase_anon_key=env["SUPABASE_ANON_KEY"],
        data_dir=Path(env.get("VISIO_DATA_DIR", _DEFAULT_DATA_DIR)),
        segment_duration_ms=int(
            env.get("VISIO_SEGMENT_DURATION_MS", _DEFAULT_SEGMENT_DURATION_MS)
        ),
        framerate=int(env.get("VISIO_FRAMERATE", _DEFAULT_FRAMERATE)),
    )


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


def main() -> int:
    """Compose the daemon from its already-tested parts.

    Thin on purpose: no branching logic of its own beyond the boot-sequence
    gates the pieces it calls already own. Validated on real hardware in
    Epic 5 rather than through additional unit tests (see the plan's
    Handoff section).
    """
    config = load_config(os.environ)

    session_path = config.data_dir / "session.json"
    state_path = config.data_dir / "device.json"
    queue_dir = config.data_dir / "queue"
    config.data_dir.mkdir(parents=True, exist_ok=True)

    battery_reader = PiJuiceBatteryReader()
    led_driver = Ws2812LedDriver()

    startup = run_startup_sequence(battery_reader, led_driver)
    if not startup.proceed:
        return 0

    if not session_path.exists():
        payload = run_onboarding(
            RpicamZbarScanner(),
            _NM_KEYFILE_PATH,
            session_path,
            _ONBOARDING_MAX_ATTEMPTS,
        )
        if payload is None:
            return 1

    device_id_was_new = not state_path.exists()
    device_id = load_or_create_device_id(state_path)

    clients = build_supabase_clients(
        config.supabase_url, config.supabase_anon_key, session_path
    )

    if device_id_was_new:
        register_device(clients.registration, device_id, _DEVICE_NAME)

    flush_pending(queue_dir, clients.storage, device_id)

    stop = threading.Event()

    def _handle_signal(signum: int, frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    deps = LoopDeps(
        command_runner=SubprocessCommandRunner(),
        storage_client=clients.storage,
        status_client=clients.status,
        led_driver=led_driver,
        battery_reader=battery_reader,
        disk_stats_reader=ShutilDiskStatsReader(),
        data_dir=config.data_dir,
        queue_dir=queue_dir,
        device_id=device_id,
        segment_duration_ms=config.segment_duration_ms,
        framerate=config.framerate,
    )
    run_recording_loop(deps, stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
