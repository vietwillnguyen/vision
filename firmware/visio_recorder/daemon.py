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
from typing import Any

from visio_recorder.battery import (
    BATTERY_BOOT_SAMPLE_INTERVAL_SEC,
    BATTERY_BOOT_SAMPLES,
    WAKE_ON_CHARGE_PCT,
    BatteryMonitor,
    BatteryReader,
    BatteryState,
    BatteryVerdict,
)
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
    open_pijuice,
)
from visio_recorder.flag_button import FlagUploadWorker, make_flag_press_handler
from visio_recorder.led import LedDriver, apply_led_state
from visio_recorder.muxer import CommandRunner, SubprocessCommandRunner
from visio_recorder.onboarding import (
    NmcliConnectionActivator,
    RpicamZbarScanner,
    activate_connection,
    reactivate_connection_if_configured,
    run_onboarding,
)
from visio_recorder.power import (
    DisabledPowerController,
    PiJuicePowerController,
    PowerController,
    SystemdPowerController,
)
from visio_recorder.recording_loop import (
    UPLOAD_DRAIN_TIMEOUT_SEC,
    DiskStatsReader,
    LoopOutcome,
    RetryBackoff,
    ShutilDiskStatsReader,
    StatusClient,
    UploadHealth,
    WorkerAction,
    build_device_status,
    flush_pending,
    next_led_state,
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


def resolve_power_controller(
    env: Mapping[str, str],
    runner: CommandRunner,
    pijuice: Any | None,
) -> PowerController:
    """How this device powers itself off at the end of a battery halt.

    ``VISIO_BATTERY_POWEROFF=off`` is for bench work: the whole halt path -
    the drain, the final status push, the red LED, the wake level - can be
    watched on a real device without the device going dark before it can be
    read. Any other value powers off for real.

    With a PiJuice present the HAT's own sequence runs first and the OS halt
    last; without one there is nothing to arm and ``systemctl poweroff`` is
    the whole story.
    """
    if env.get("VISIO_BATTERY_POWEROFF", "on").strip().lower() == "off":
        _logger.warning(
            "VISIO_BATTERY_POWEROFF=off: a battery halt will stop recording "
            "but leave this device powered"
        )
        return DisabledPowerController()
    if pijuice is None:
        return SystemdPowerController(runner)
    return PiJuicePowerController(pijuice.power, runner)


@dataclass
class StartupResult:
    proceed: bool
    battery_pct: int
    # The debounced battery the boot samples built, handed to the loop so its
    # moving average starts from real readings rather than from nothing.
    battery: BatteryState


def run_startup_sequence(
    battery_reader: BatteryReader,
    led_driver: LedDriver,
    sleep: Callable[[float], None] = time.sleep,
    samples: int = BATTERY_BOOT_SAMPLES,
    sample_interval_sec: float = BATTERY_BOOT_SAMPLE_INTERVAL_SEC,
    monitor: BatteryMonitor | None = None,
) -> StartupResult:
    """Decide whether this boot may record, on more than one reading.

    The boot check used to halt a whole day's recording on a single sample. It
    now takes ``samples`` of them, ``sample_interval_sec`` apart, and stops
    early the moment one confirms a halt - so a genuinely flat pendant is
    still caught in about as long as it used to be, while a pendant whose
    gauge reported one bad value at power-on goes on to record.

    Three seconds is the whole cost against a day of recording. Returns the
    ``BatteryState`` the samples went into, so the recording loop inherits a
    warm moving average instead of starting blind.
    """
    battery = BatteryState(battery_reader, monitor)
    verdict = battery.current()
    for _ in range(max(0, samples - 1)):
        if verdict.should_halt:
            break
        sleep(sample_interval_sec)
        verdict = battery.sample()

    if verdict.should_halt:
        _logger.error(
            "battery halt confirmed at boot (%d%%, reason %s); not recording",
            verdict.pct,
            verdict.halt_reason.value if verdict.halt_reason else "unknown",
        )
        apply_led_state(led_driver, next_led_state(False, False, battery_halted=True))
        return StartupResult(proceed=False, battery_pct=verdict.pct, battery=battery)

    apply_led_state(led_driver, next_led_state(verdict.is_low, is_uploading=False))
    return StartupResult(proceed=True, battery_pct=verdict.pct, battery=battery)


@dataclass
class LoopDeps:
    command_runner: CommandRunner
    storage_client: StorageClient
    status_client: StatusClient
    led_driver: LedDriver
    # The debounced battery. The main loop is its only sampler; the upload
    # worker reads the published verdict.
    battery: BatteryState
    power_controller: PowerController
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
    # How long a battery halt waits for the queue to drain before powering
    # off. Bounded, unlike the stop path's join: a dying battery is exactly
    # when an upload is most likely to hang on a weak signal, and waiting
    # forever for it ends in a brownout rather than a power-off.
    upload_drain_timeout_sec: float = UPLOAD_DRAIN_TIMEOUT_SEC
    # The charge level the PiJuice is told to wake the Pi back up at.
    wake_on_charge_pct: int = WAKE_ON_CHARGE_PCT


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
            battery_pct=deps.battery.current().pct,
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


def _complete_battery_halt(
    deps: LoopDeps,
    halt_verdict: BatteryVerdict,
    worker_thread: threading.Thread,
) -> LoopOutcome:
    """Show the halt, then either cancel it or power the pendant off.

    The Critical red comes first and is the final LED state of a real halt: it
    is applied after the worker join, so nothing a drained segment does can
    paint over it, and it is the pendant's only chance to tell the wearer why
    it is about to go dark.

    Then one more reading. A charger connected while the queue was draining
    means the halt's premise is gone, so the power-off is cancelled and the
    loop is re-entered - plugging a pendant in keeps it running rather than
    powering it off and waiting for the charge wake. The bounded drain is also
    abandoned at that point: the bound existed because the battery was dying,
    and on external power there is no hurry, so a worker still in flight is
    joined properly before the loop restarts rather than racing a second one.
    """
    apply_led_state(
        deps.led_driver,
        next_led_state(halt_verdict.is_low, is_uploading=False, battery_halted=True),
    )
    recovery = deps.battery.sample()
    if recovery.external_power:
        _logger.warning(
            "external power returned during the battery halt drain at %d%%; "
            "cancelling the power-off and resuming",
            recovery.pct,
        )
        if worker_thread.is_alive():
            worker_thread.join()
        apply_led_state(
            deps.led_driver, next_led_state(recovery.is_low, is_uploading=False)
        )
        return LoopOutcome.RECOVERED

    deps.power_controller.power_off(wake_on_charge_pct=deps.wake_on_charge_pct)
    return LoopOutcome.HALTED


def run_recording_loop(
    deps: LoopDeps,
    stop: threading.Event,
    clock: Callable[[], datetime] = datetime.now,
    sleep: Callable[[float], None] = time.sleep,
) -> LoopOutcome:
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
                    battery=deps.battery,
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
                    battery=deps.battery,
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
    # A daemon thread only because of the halt path's bounded join: once that
    # bound has elapsed the loop has already decided to stop waiting for a
    # wedged upload, and a non-daemon thread would then hold the process open
    # at interpreter exit anyway. Every other path joins it outright, so
    # nothing else changes.
    worker_thread = threading.Thread(target=worker, daemon=True)
    worker_thread.start()
    halt_verdict: BatteryVerdict | None = None
    not_before: datetime | None = None
    try:
        while True:
            verdict = deps.battery.sample()
            if verdict.should_halt:
                halt_verdict = verdict
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
        if halt_verdict is None:
            worker_thread.join()
        else:
            worker_thread.join(timeout=deps.upload_drain_timeout_sec)
            if worker_thread.is_alive():
                _logger.warning(
                    "the upload queue did not drain within %.0f s of the "
                    "battery halt; whatever is left stays queued for the next "
                    "boot",
                    deps.upload_drain_timeout_sec,
                )
        _push_recording_status(
            deps,
            segments_uploaded_today=uploaded_today[0],
            recording_active=False,
            audio_active=audio is not None,
        )
    if halt_verdict is None:
        return LoopOutcome.STOPPED
    return _complete_battery_halt(deps, halt_verdict, worker_thread)


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

    command_runner = SubprocessCommandRunner()
    pijuice = open_pijuice()
    battery_reader = PiJuiceBatteryReader(pijuice)
    led_driver = Ws2812LedDriver()
    power_controller = resolve_power_controller(os.environ, command_runner, pijuice)

    startup = run_startup_sequence(battery_reader, led_driver)
    if not startup.proceed:
        # A confirmed halt at boot must not leave the Pi powered and
        # discharging; that is what used to corrupt SD cards.
        power_controller.power_off(wake_on_charge_pct=WAKE_ON_CHARGE_PCT)
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
        battery=startup.battery,
        power_controller=power_controller,
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
        while run_recording_loop(deps, stop) is LoopOutcome.RECOVERED:
            # Charge came back while a halt was draining its queue, so the
            # power-off was cancelled. Re-enter with a fresh upload worker
            # rather than exiting and waiting for the PiJuice's charge wake.
            _logger.warning("battery recovered; restarting the recording loop")
    finally:
        flag_upload_worker.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
