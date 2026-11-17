import logging
import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from visio_recorder.battery import BatteryReader, read_battery_status
from visio_recorder.capture import (
    AudioSettings,
    VideoSettings,
    probe_audio_device,
    record_segment,
    segment_path,
)
from visio_recorder.device_identity import load_or_create_device_id, register_device
from visio_recorder.drivers import (
    GpioZeroFlagButton,
    PiJuiceBatteryReader,
    Ws2812LedDriver,
)
from visio_recorder.flag_button import FlagUploadWorker, make_flag_press_handler
from visio_recorder.led import LedDriver, LedState, apply_led_state
from visio_recorder.muxer import CommandRunner, SubprocessCommandRunner
from visio_recorder.onboarding import (
    NmcliConnectionActivator,
    RpicamZbarScanner,
    activate_connection,
    reactivate_connection_if_configured,
    run_onboarding,
)
from visio_recorder.recording_loop import (
    DiskStatsReader,
    RetryBackoff,
    ShutilDiskStatsReader,
    StatusClient,
    UploadHealth,
    WorkerAction,
    build_device_status,
    flush_pending,
    next_worker_step,
    on_segment_complete,
    pending_segments,
    recover_orphaned_segments,
    retry_pending_segments,
)
from visio_recorder.segment_size import (
    AUDIO_BITRATE_RESERVE_BPS,
    DEFAULT_MAX_UPLOAD_BYTES,
    SIZE_BUDGET_FRACTION,
    default_segment_duration_ms,
    predicted_segment_bytes,
)
from visio_recorder.supabase_clients import build_supabase_clients
from visio_recorder.upload_limit import FfprobeDurationProbe, UploadLimit
from visio_recorder.uploader import StorageClient

_DEFAULT_DATA_DIR = "/var/lib/visio-recorder"
_ONBOARDING_MAX_ATTEMPTS = 60
_NM_KEYFILE_PATH = Path("/etc/NetworkManager/system-connections/visio.nmconnection")
_NM_CONNECTION_ID = "visio"  # the [connection] id inside the keyfile
_DEVICE_NAME = "visio-pendant"

_logger = logging.getLogger(__name__)


@dataclass
class DaemonConfig:
    supabase_url: str
    supabase_anon_key: str
    data_dir: Path
    segment_duration_ms: int
    video: VideoSettings
    max_upload_bytes: int
    # None when VISIO_AUDIO=off; otherwise the microphone to try at startup.
    audio: AudioSettings | None


def load_config(env: Mapping[str, str]) -> DaemonConfig:
    """Read the daemon's settings from its EnvironmentFile.

    ``VISIO_SEGMENT_DURATION_MS`` defaults to the longest segment whose
    predicted size at ``VISIO_VIDEO_BITRATE`` fits the size budget under
    ``VISIO_MAX_UPLOAD_BYTES`` (one minute at the 4 Mbps default; see
    ``visio_recorder.segment_size``). An explicit duration is honoured even
    when it is predicted to overflow, with a warning: oversized files are
    split before upload, so it costs splits, not footage.

    Audio is on unless ``VISIO_AUDIO=off``; whether a microphone actually
    exists is decided at startup by ``resolve_audio``, not here.
    """
    for key in ("SUPABASE_URL", "SUPABASE_ANON_KEY"):
        if key not in env:
            raise ValueError(f"missing required environment variable: {key}")
    defaults = VideoSettings()
    video = VideoSettings(
        width=int(env.get("VISIO_VIDEO_WIDTH", defaults.width)),
        height=int(env.get("VISIO_VIDEO_HEIGHT", defaults.height)),
        framerate=int(env.get("VISIO_FRAMERATE", defaults.framerate)),
        bitrate_bps=int(env.get("VISIO_VIDEO_BITRATE", defaults.bitrate_bps)),
    )
    max_upload_bytes = int(env.get("VISIO_MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES))
    audio_mode = env.get("VISIO_AUDIO", "on").strip().lower()
    if audio_mode not in ("on", "off"):
        raise ValueError(f"VISIO_AUDIO must be 'on' or 'off', got {audio_mode!r}")
    audio_defaults = AudioSettings()
    audio = (
        None
        if audio_mode == "off"
        else AudioSettings(
            device=env.get("VISIO_AUDIO_DEVICE", audio_defaults.device),
            bitrate_bps=int(env.get("VISIO_AUDIO_BITRATE", audio_defaults.bitrate_bps)),
            samplerate=audio_defaults.samplerate,
        )
    )
    # Reserve at least the configured audio bitrate, so the default segment
    # length is the same whether or not a microphone turns up at startup.
    audio_bps = max(AUDIO_BITRATE_RESERVE_BPS, audio.bitrate_bps if audio else 0)
    if "VISIO_SEGMENT_DURATION_MS" in env:
        segment_duration_ms = int(env["VISIO_SEGMENT_DURATION_MS"])
        predicted = predicted_segment_bytes(
            segment_duration_ms, video.bitrate_bps, audio_bps
        )
        if predicted > max_upload_bytes * SIZE_BUDGET_FRACTION:
            _logger.warning(
                "VISIO_SEGMENT_DURATION_MS=%d at %d bps predicts %d-byte segments, "
                "over the size budget for the %d-byte upload limit; oversized "
                "segments will be split before upload",
                segment_duration_ms,
                video.bitrate_bps,
                predicted,
                max_upload_bytes,
            )
    else:
        segment_duration_ms = default_segment_duration_ms(
            max_upload_bytes, video.bitrate_bps, audio_bps
        )
    return DaemonConfig(
        supabase_url=env["SUPABASE_URL"],
        supabase_anon_key=env["SUPABASE_ANON_KEY"],
        data_dir=Path(env.get("VISIO_DATA_DIR", _DEFAULT_DATA_DIR)),
        segment_duration_ms=segment_duration_ms,
        video=video,
        max_upload_bytes=max_upload_bytes,
        audio=audio,
    )


def resolve_audio(
    runner: CommandRunner, audio: AudioSettings | None
) -> AudioSettings | None:
    """The microphone to record from, or None to record video only.

    A pendant without a working microphone (not wired yet, overlay missing,
    device renamed) must still record: the probe failing is a warning, not a
    startup failure. ``device_status.audio_active`` tells the app which way
    each segment went.
    """
    if audio is None:
        return None
    if probe_audio_device(runner, audio):
        return audio
    _logger.warning(
        "ALSA capture device %r did not open; recording video only. Check the "
        "I2S microphone wiring and the dtoverlay/asound.conf setup (README, "
        "firmware section), or set VISIO_AUDIO=off to silence this warning",
        audio.device,
    )
    return None


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
    video: VideoSettings
    upload_limit: UploadLimit
    # The microphone resolve_audio found at startup, or None for video only.
    audio: AudioSettings | None = None
    # How long the upload worker waits between in-process retries of the queue.
    retry_backoff: RetryBackoff = field(default_factory=RetryBackoff)
    # The clock the upload worker measures its retry deadline against.
    monotonic: Callable[[], float] = time.monotonic


def _push_recording_status(
    deps: LoopDeps,
    segments_uploaded_today: int,
    recording_active: bool,
    audio_active: bool,
) -> None:
    """Tell the app whether the device is recording, without ever raising.

    Called from the main thread only while the worker is idle (before the
    first segment, after the drain), so it never races the worker's own
    per-segment upserts. A failure is logged and swallowed: at shutdown the
    network is often already gone, and neither a clean stop nor the start of
    recording may depend on reaching Supabase.
    """
    try:
        status = build_device_status(
            device_id=deps.device_id,
            battery_pct=read_battery_status(deps.battery_reader).pct,
            disk_stats_reader=deps.disk_stats_reader,
            data_dir=deps.data_dir,
            queue_dir=deps.queue_dir,
            segments_uploaded_today=segments_uploaded_today,
            recording_active=recording_active,
            audio_active=audio_active,
        )
        deps.status_client.upsert_device_status(asdict(status))
    except Exception:
        _logger.exception(
            "could not report recording_active=%s to device_status", recording_active
        )


def run_recording_loop(
    deps: LoopDeps,
    stop: threading.Event,
    clock: Callable[[], datetime] = datetime.now,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Record segments on the main thread; upload them on one background worker.

    The main thread only records and enqueues. A single worker thread owns all
    upload state (segment count, the ``UploadHealth`` consecutive-failure
    counter) and every LED call except the halt CRITICAL applied here at exit.
    Both the halt and stop paths drain the queue via a ``None`` sentinel before
    joining the worker; on halt, the CRITICAL LED is applied after the drain so
    it is the final LED state and cannot be overwritten by the drained
    segments' LED restores.

    The worker no longer applies the upload-failure CRITICAL itself.
    ``on_segment_complete`` derives both of its LED states from the shared
    ``UploadHealth``, so once three uploads have failed in a row the red is
    held for every following segment - including while it uploads - until an
    upload succeeds and resets the counter.

    The worker also owns the in-process retry, which is why, while segments
    are queued, it waits on its work queue only until a retry deadline kept on
    ``deps.monotonic``. Once that deadline passes it runs
    ``retry_pending_segments`` over whatever is still queued, so a failed
    upload reaches storage during this run instead of waiting for the next
    boot's ``flush_pending``. ``next_worker_step`` makes that decision: the
    deadline is armed from ``deps.retry_backoff``, indexed by the same
    ``UploadHealth`` counter, so it grows while uploads keep failing and the
    success that clears the red brings the next one back to the base delay.
    It is armed when the queue becomes non-empty and again after each round,
    never by a new segment, so segments arriving more often than the backoff
    cannot postpone the retry. A new segment or the sentinel still returns
    from ``work.get`` immediately, so the retry delays neither. ``stopping``
    is set before the sentinel is queued: a stopping worker starts no round,
    and a round already draining a backlog gives up rather than uploading
    through a halt.

    ``device_status.recording_active`` is pushed True before the first capture
    and False once the loop has exited for any reason (stop, halt, or a
    capture failure escaping), after the drain so the final row carries the
    final counts. Per-segment upserts in between report True.

    With ``deps.audio`` set, each segment records the microphone too. If such
    a capture fails while the loop is not stopping (the microphone vanished,
    or libav could not keep up), the loop records video only from then on
    rather than failing every segment; the failed capture's partial ``.ts``
    is queued for the worker like any capture when it holds data (a
    zero-byte one is deleted), and per-segment upserts report
    ``audio_active`` false from the next segment on. The next capture waits
    for the following whole second if needed, so its second-resolution
    filename never collides with the failed capture's.
    """
    work: queue.Queue[Path | None] = queue.Queue()
    # Written only by the worker; read by the main thread only after join().
    uploaded_today = [0]
    # Set before the sentinel is queued, so a retry round in flight can give
    # up on the rest of its backlog instead of uploading through a halt.
    stopping = threading.Event()

    def worker() -> None:
        segments_uploaded_today = 0
        upload_health = UploadHealth()
        retry_at: float | None = None

        def retry_round() -> None:
            nonlocal segments_uploaded_today
            try:
                outcome = retry_pending_segments(
                    queue_dir=deps.queue_dir,
                    storage_client=deps.storage_client,
                    led_driver=deps.led_driver,
                    battery_reader=deps.battery_reader,
                    device_id=deps.device_id,
                    upload_limit=deps.upload_limit,
                    upload_health=upload_health,
                    cancel=stopping.is_set,
                )
            except Exception:
                _logger.exception("queue retry round failed; worker continuing")
                return
            segments_uploaded_today += outcome.uploaded
            uploaded_today[0] = segments_uploaded_today

        while True:
            now = deps.monotonic()
            step = next_worker_step(
                segments_pending=bool(pending_segments(deps.queue_dir)),
                stopping=stopping.is_set(),
                retry_at=retry_at,
                now=now,
                retry_backoff=deps.retry_backoff,
                consecutive_failures=upload_health.consecutive_failures,
            )
            retry_at = step.retry_at
            # Waiting with a timeout is what makes the retry in-process: the
            # worker owns it, so there is no second thread and no scheduler,
            # and a new segment or the sentinel still wakes it immediately.
            try:
                if step.action is WorkerAction.RETRY_NOW:
                    raise queue.Empty
                item = work.get(timeout=None if retry_at is None else retry_at - now)
            except queue.Empty:
                retry_round()
                retry_at = None
                continue
            if item is None:
                return
            try:
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
                    framerate=deps.video.framerate,
                    disk_stats_reader=deps.disk_stats_reader,
                    data_dir=deps.data_dir,
                    upload_limit=deps.upload_limit,
                    upload_health=upload_health,
                )
            except Exception:
                _logger.exception(
                    "on_segment_complete failed for segment %s; worker continuing",
                    item,
                )
                continue
            segments_uploaded_today = result.segments_uploaded_today
            uploaded_today[0] = segments_uploaded_today

    audio = deps.audio
    _push_recording_status(
        deps,
        segments_uploaded_today=0,
        recording_active=True,
        audio_active=audio is not None,
    )
    worker_thread = threading.Thread(target=worker)
    worker_thread.start()
    halted = False
    not_before: datetime | None = None
    try:
        while True:
            battery_status = read_battery_status(deps.battery_reader)
            if battery_status.should_halt:
                halted = True
                break
            if stop.is_set():
                break
            started_at = clock()
            if not_before is not None:
                wait = (not_before - started_at).total_seconds()
                if 0 < wait <= 1:
                    sleep(wait)
                    started_at = not_before
                not_before = None
            try:
                h264_path = record_segment(
                    deps.command_runner,
                    deps.data_dir,
                    started_at,
                    deps.segment_duration_ms,
                    deps.video,
                    audio,
                )
            except subprocess.CalledProcessError:
                if audio is None or stop.is_set():
                    raise
                _logger.exception(
                    "audio+video capture failed; recording video only for the "
                    "rest of this run"
                )
                partial = segment_path(deps.data_dir, started_at, audio)
                audio = None
                not_before = started_at.replace(microsecond=0) + timedelta(seconds=1)
                if partial.exists():
                    if partial.stat().st_size > 0:
                        work.put(partial)
                    else:
                        partial.unlink()
                continue
            work.put(h264_path)
    finally:
        stopping.set()
        work.put(None)
        worker_thread.join()
        _push_recording_status(
            deps,
            segments_uploaded_today=uploaded_today[0],
            recording_active=False,
            audio_active=audio is not None,
        )
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
    registered_marker_path = config.data_dir / "device_registered"
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
        if not activate_connection(
            NmcliConnectionActivator(), _NM_CONNECTION_ID, led_driver
        ):
            # Keyfile and session are on disk; systemd's Restart=on-failure
            # plus NetworkManager autoconnect retry from here.
            return 1
    else:
        reactivate_connection_if_configured(
            NmcliConnectionActivator(), _NM_CONNECTION_ID, _NM_KEYFILE_PATH
        )

    device_id = load_or_create_device_id(state_path)

    clients = build_supabase_clients(
        config.supabase_url, config.supabase_anon_key, session_path
    )

    if not registered_marker_path.exists():
        register_device(clients.registration, device_id, _DEVICE_NAME)
        registered_marker_path.touch()

    command_runner = SubprocessCommandRunner()
    audio = resolve_audio(command_runner, config.audio)
    upload_limit = UploadLimit(
        max_bytes=config.max_upload_bytes,
        runner=command_runner,
        probe=FfprobeDurationProbe(),
    )
    _logger.info(
        "recording %dx%d@%d fps at %d bps in %d ms segments (upload limit %d bytes)",
        config.video.width,
        config.video.height,
        config.video.framerate,
        config.video.bitrate_bps,
        config.segment_duration_ms,
        config.max_upload_bytes,
    )
    flush_pending(queue_dir, clients.storage, device_id, upload_limit)
    if recover_orphaned_segments(
        config.data_dir, queue_dir, command_runner, config.video.framerate
    ):
        flush_pending(queue_dir, clients.storage, device_id, upload_limit)

    stop = threading.Event()

    def _handle_signal(signum: int, frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Held for the daemon's lifetime: gpiozero releases the pin (and the
    # callback) when the Button object is garbage collected.
    flag_upload_worker = FlagUploadWorker(clients.storage, device_id)
    flag_button = GpioZeroFlagButton()
    flag_button.on_press(
        make_flag_press_handler(
            queue_dir=queue_dir,
            submit=flag_upload_worker.submit,
        )
    )

    deps = LoopDeps(
        command_runner=command_runner,
        storage_client=clients.storage,
        status_client=clients.status,
        led_driver=led_driver,
        battery_reader=battery_reader,
        disk_stats_reader=ShutilDiskStatsReader(),
        data_dir=config.data_dir,
        queue_dir=queue_dir,
        device_id=device_id,
        segment_duration_ms=config.segment_duration_ms,
        video=config.video,
        upload_limit=upload_limit,
        audio=audio,
    )
    try:
        run_recording_loop(deps, stop)
    finally:
        flag_upload_worker.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
