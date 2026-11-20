import logging
import math
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
from typing import Any, Protocol

from supabase import create_client

from visio_recorder.battery import (
    BATTERY_BOOT_SAMPLE_INTERVAL_SEC,
    BATTERY_BOOT_SAMPLES,
    HALT_CONFIRMATIONS,
    WAKE_ON_CHARGE_PCT,
    BatteryMonitor,
    BatteryReader,
    BatteryState,
    BatteryVerdict,
    HaltReason,
)
from visio_recorder.capture import (
    AudioSettings,
    InterruptibleCommandRunner,
    SubprocessInterruptibleRunner,
    VideoSettings,
    probe_audio_device,
    record_segment,
    segment_path,
)
from visio_recorder.device_identity import load_paired_device_id
from visio_recorder.drivers import (
    GpioZeroFlagButton,
    PiJuiceBatteryReader,
    Ws2812LedDriver,
    open_pijuice,
)
from visio_recorder.flag_button import FlagUploadWorker, make_flag_press_handler
from visio_recorder.led import LedController, LedDriver, LedState, apply_led_state
from visio_recorder.muxer import CommandRunner, SubprocessCommandRunner
from visio_recorder.onboarding import (
    ConnectionActivator,
    NmcliConnectionActivator,
    QrScanner,
    RpicamZbarScanner,
    activate_connection,
    reactivate_connection_if_configured,
    run_onboarding,
)
from visio_recorder.pairing import (
    DEVICE_FILE,
    MAX_FAILED_PENDING_ACTIVATIONS,
    PAIRING_FILE,
    SESSION_FILE,
    Pairing,
    PairingRejectedError,
    PairingState,
    PairingUnavailableError,
    clear_local_pairing,
    complete_pairing,
    pairing_state,
    record_failed_activation,
    reset_failed_activations,
)
from visio_recorder.power import (
    PiJuicePowerController,
    PowerController,
    SystemdPowerController,
)
from visio_recorder.recording_loop import (
    RESTART_GAP_SEC,
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
    queue_segment,
    recover_orphaned_segments,
    retry_pending_segments,
    upload_segment,
    wait_out_restart_gap,
)
from visio_recorder.segment_size import (
    AUDIO_BITRATE_RESERVE_BPS,
    DEFAULT_MAX_UPLOAD_BYTES,
    SIZE_BUDGET_FRACTION,
    default_segment_duration_ms,
    predicted_segment_bytes,
)
from visio_recorder.supabase_clients import (
    SessionRevokedError,
    SupabaseClients,
    restore_supabase_client,
    supabase_clients_for,
)
from visio_recorder.upload_limit import FfprobeDurationProbe, UploadLimit
from visio_recorder.uploader import StorageClient

_DEFAULT_DATA_DIR = "/var/lib/visio-recorder"
_ONBOARDING_MAX_ATTEMPTS = 60
_NM_KEYFILE_PATH = Path("/etc/NetworkManager/system-connections/visio.nmconnection")
_NM_CONNECTION_ID = "visio"  # the [connection] id inside the keyfile
# How long a stopping (or halting) upload worker gives the flag worker to send
# the markers queued ahead of a segment. A normal flush may take the flag
# worker's own 30 s timeout, but that alone would spend systemd's whole
# TimeoutStopSec=30 before the cut-short last segment starts uploading.
STOP_MARKER_FLUSH_TIMEOUT_SEC = 5.0
# How long a battery halt still waits for the queue once a stop is also
# pending: the cut-short capture, the mux and the status push come first, and
# the power-off has to follow inside systemd's TimeoutStopSec=30.
STOP_HALT_DRAIN_TIMEOUT_SEC = 15.0
_HALT_DRAIN_POLL_SEC = 0.1

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
    runner: CommandRunner,
    pijuice: Any | None,
) -> PowerController:
    """How this device powers itself off at the end of a battery halt.

    A confirmed halt always powers off. With a PiJuice present the HAT's own
    sequence runs first and the OS halt last; without one there is nothing to
    arm and ``systemctl poweroff`` is the whole story.
    """
    if pijuice is None:
        return SystemdPowerController(runner)
    return PiJuicePowerController(pijuice.power, runner)


@dataclass
class StartupResult:
    proceed: bool
    # None, like ``battery``, only when the gauge could not be read at all.
    battery_pct: int | None
    # The debounced battery the boot samples built, handed to the loop so its
    # moving average starts from real readings rather than from nothing.
    battery: BatteryState | None


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

    The first reading is retried up to ``HALT_CONFIRMATIONS`` times at the
    same cadence, and the state is built from the first one that succeeds. A
    gauge unreadable for every attempt is the boot-time form of the
    ``unreadable`` halt: the boot is refused so ``main`` powers off, rather
    than crashing into a restart loop that never does.
    """
    battery: BatteryState | None = None
    for attempt in range(HALT_CONFIRMATIONS):
        if attempt:
            sleep(sample_interval_sec)
        try:
            battery = BatteryState(battery_reader, monitor)
            break
        except Exception:
            _logger.exception("battery reading failed at boot")
    if battery is None:
        _logger.error(
            "battery halt confirmed at boot (reason %s): gauge unreadable for "
            "%d consecutive readings; not recording",
            HaltReason.UNREADABLE.value,
            HALT_CONFIRMATIONS,
        )
        apply_led_state(led_driver, next_led_state(False, False, battery_halted=True))
        return StartupResult(proceed=False, battery_pct=None, battery=None)

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


class MarkerFlush(Protocol):
    """``FlagUploadWorker.flush_markers``: send every queued marker now."""

    def __call__(self, timeout: float | None = None) -> bool: ...


@dataclass
class LoopDeps:
    command_runner: CommandRunner
    # The capture is the one command a stop has to be able to cut short, so it
    # runs through its own interruptible runner; ``command_runner`` keeps the
    # mux, the split and the ALSA probe.
    capture_runner: InterruptibleCommandRunner
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
    # The clock the upload worker measures its retry deadline and each
    # segment's restart gap against, and the sleep that waits the gap out.
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    # FlagUploadWorker.flush_markers: run before any segment is uploaded, once
    # the segment's restart gap has passed, so flags reach storage with the
    # footage the pipeline would score them on.
    flush_markers: MarkerFlush | None = None
    restart_gap_sec: float = RESTART_GAP_SEC
    # The flush timeout once the loop is stopping, when the gap is not waited.
    stop_marker_flush_timeout_sec: float = STOP_MARKER_FLUSH_TIMEOUT_SEC
    # How long a battery halt waits for the queue to drain before powering
    # off. Bounded, unlike the stop path's join: a dying battery is exactly
    # when an upload is most likely to hang on a weak signal, and waiting
    # forever for it ends in a brownout rather than a power-off.
    upload_drain_timeout_sec: float = UPLOAD_DRAIN_TIMEOUT_SEC
    # The charge level the PiJuice is told to wake the Pi back up at.
    wake_on_charge_pct: int = WAKE_ON_CHARGE_PCT
    # Upload state that outlives one run of the recording loop, so a loop
    # re-entered after a cancelled halt keeps the Critical latch and the
    # day's upload count. Written only by the upload worker; the main thread
    # reads the count for its status pushes, and only the push after the join
    # is guaranteed the final one.
    upload_health: UploadHealth = field(default_factory=UploadHealth)
    segments_uploaded_today: int = 0
    # The cap on the halt drain once a stop (SIGTERM, SIGINT) is also pending,
    # so the power-off still lands inside systemd's TimeoutStopSec=30.
    stop_halt_drain_timeout_sec: float = STOP_HALT_DRAIN_TIMEOUT_SEC


class _StopAwareStatusClient:
    """The one path every ``device_status`` upsert of a recording run takes.

    Once ``stopping`` is set a row reports ``recording_active=False`` whatever
    it was built with, and the lock makes that check and the upsert one step,
    so a per-segment upsert built before the stop can never land after the
    stop's own push and turn the app back to Recording.
    """

    def __init__(self, inner: StatusClient, stopping: threading.Event) -> None:
        self._inner = inner
        self._stopping = stopping
        self._lock = threading.Lock()

    def upsert_device_status(self, status: dict) -> None:
        with self._lock:
            if self._stopping.is_set():
                status = {**status, "recording_active": False}
            self._inner.upsert_device_status(status)


@dataclass(frozen=True)
class _CapturedSegment:
    """A finished capture for the worker to mux, queue and upload.

    ``ended_at`` is when the capture returned, on ``LoopDeps.monotonic``: the
    start of the restart gap whose presses the pipeline gives this segment.
    """

    path: Path
    ended_at: float


@dataclass(frozen=True)
class _QueuedSegment:
    """A segment the main thread already muxed and queued, left to upload."""

    path: Path
    audio_active: bool
    ended_at: float


def _push_recording_status(
    deps: LoopDeps,
    status_client: StatusClient,
    segments_uploaded_today: int,
    recording_active: bool,
    audio_active: bool,
) -> None:
    """Tell the app whether the device is recording, without ever raising.

    A failure is logged and swallowed: at shutdown the network is often
    already gone, and neither a clean stop nor the start of recording may
    depend on reaching Supabase.
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
        status_client.upsert_device_status(asdict(status))
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

    The Halted solid red comes first and is the final LED state of a real halt: it
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
            deps.led_driver,
            next_led_state(
                recovery.is_low,
                is_uploading=False,
                upload_critical=deps.upload_health.is_critical,
            ),
        )
        return LoopOutcome.RECOVERED

    deps.power_controller.power_off(wake_on_charge_pct=deps.wake_on_charge_pct)
    return LoopOutcome.HALTED


def _join_halt_drain(
    deps: LoopDeps, worker_thread: threading.Thread, stop: threading.Event
) -> None:
    """Wait for a halting worker to drain the queue, but never for too long.

    The bound is ``deps.upload_drain_timeout_sec`` from now. A stop that is
    pending, or arrives while this waits, caps the rest of the wait at
    ``deps.stop_halt_drain_timeout_sec``: systemd kills the daemon
    ``TimeoutStopSec=30`` after its SIGTERM, and a halt killed mid-drain never
    reaches its power-off, which is the brownout the halt exists to prevent.
    Real time on purpose, like ``Thread.join``'s own timeout.
    """
    started = time.monotonic()
    deadline = started + deps.upload_drain_timeout_sec
    stop_seen = False
    while worker_thread.is_alive():
        now = time.monotonic()
        if not stop_seen and stop.is_set():
            stop_seen = True
            deadline = min(deadline, now + deps.stop_halt_drain_timeout_sec)
        if now >= deadline:
            break
        worker_thread.join(timeout=min(deadline - now, _HALT_DRAIN_POLL_SEC))
    if worker_thread.is_alive():
        _logger.warning(
            "the upload queue did not drain within %.0f s of the battery halt; "
            "whatever is left stays queued for the next boot",
            time.monotonic() - started,
        )


def _has_footage(capture_path: Path) -> bool:
    """Whether a capture left anything worth muxing.

    A capture cut short before its first frame leaves an empty file, or no
    file at all when it was signalled before it opened the output. Either way
    there is nothing to mux, and the loop must not turn that into an
    exception: it happens on the ordinary stop path.
    """
    try:
        return capture_path.stat().st_size > 0
    except FileNotFoundError:
        return False


def run_recording_loop(
    deps: LoopDeps,
    stop: threading.Event,
    clock: Callable[[], datetime] = datetime.now,
    sleep: Callable[[float], None] = time.sleep,
) -> LoopOutcome:
    """Record segments on the main thread; upload them on one background worker.

    The main thread only records and enqueues. A single worker thread owns all
    upload state (segment count, the ``UploadHealth`` consecutive-failure
    counter) and every LED call except the halt HALTED applied here at exit.
    Both the halt and stop paths drain the queue via a ``None`` sentinel before
    joining the worker; on halt, the HALTED LED is applied after the drain so
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

    ``deps.flush_markers`` is ``FlagUploadWorker.flush_markers``, and the
    worker calls it before every segment it is about to upload - its own and
    the first of each retry round - waiting for it to finish. That is what
    keeps a segment from reaching storage ahead of the flags the nightly run
    would score it with. The pipeline also gives a segment the presses made
    up to ``deps.restart_gap_sec`` after it ends, so the main thread queues
    each capture with the ``deps.monotonic`` time it returned, and the worker
    waits until that time plus the gap (never longer than the gap, on
    ``deps.sleep``) before the flush; a retry round waits on the newest
    segment this run has queued. Markers keep exactly one uploader, since this is a
    request on that worker's queue rather than an upload made here, and its
    result is ignored: footage outranks marker ordering, and the pipeline's
    late-marker handling is the backstop.

    ``stop`` is also the in-flight capture's cancel event, so a SIGTERM or
    SIGINT landing mid-segment cuts the capture short instead of being
    observed only when it ends - the wait that used to cost a whole segment
    (60 s by default, 300 s at the hard cap) and ran into systemd's stop
    timeout. The cut-short file is a complete, shorter segment and is uploaded
    like any other; only a cancel that lands before the first
    frame leaves nothing to mux - an empty file, or none at all - and that is
    discarded rather than queued, exactly as the audio-failure path treats an
    empty partial. A discarded capture never reaches the worker, so it
    neither uploads nor moves the restart-gap clock a retry round flushes on.

    Once ``stopping`` is set (a stop or a halt) the worker no longer waits out
    the restart gap before a flush, and gives the flush only
    ``deps.stop_marker_flush_timeout_sec``. Markers already on the card still
    go up ahead of the segment, the cut-short last one included, but nothing
    a stop has left to do may spend systemd's ``TimeoutStopSec=30`` budget on
    a gap that only matters to a recording that carries on: the
    recording-control design flags only while recording, and a press that
    still lands in those seconds is fsynced and goes up on the flag worker's
    own retries or the next boot's flush, with the pipeline's late-marker
    handling as the backstop.

    ``device_status.recording_active`` is pushed True before the first capture
    and False once the loop has exited for any reason (stop, halt, or a
    capture failure escaping). The False push comes before the worker drains
    the queue: on a stop the main thread muxes and queues the last segment
    itself first, so the row counts it as pending, and only then hands it to
    the worker to upload. An upload that outlasts systemd's stop timeout is
    then killed with the app already showing the stop, and the segment stays
    queued for the next boot's ``flush_pending``. Every upsert from the moment
    ``stopping`` is set reports False, so a drained segment's own upsert
    cannot undo that, and a second False push after the drain carries the
    final counts. Per-segment upserts before the stop report True.

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
    work: queue.Queue[_CapturedSegment | _QueuedSegment | None] = queue.Queue()
    # Set before the sentinel is queued, so a retry round in flight can give
    # up on the rest of its backlog instead of uploading through a halt.
    stopping = threading.Event()
    status_client = _StopAwareStatusClient(deps.status_client, stopping)

    def worker() -> None:
        upload_health = deps.upload_health
        retry_at: float | None = None
        last_ended_at = -math.inf

        def flush_after_gap(ended_at: float) -> Callable[[], bool] | None:
            flush_markers = deps.flush_markers
            if flush_markers is None:
                return None

            def flush() -> bool:
                if stopping.is_set():
                    return flush_markers(timeout=deps.stop_marker_flush_timeout_sec)
                wait_out_restart_gap(
                    ended_at,
                    gap_sec=deps.restart_gap_sec,
                    monotonic=deps.monotonic,
                    sleep=deps.sleep,
                )
                return flush_markers()

            return flush

        def retry_round() -> None:
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
                    flush_markers=flush_after_gap(last_ended_at),
                )
            except Exception:
                _logger.exception("queue retry round failed; worker continuing")
                return
            deps.segments_uploaded_today += outcome.uploaded

        while True:
            now = deps.monotonic()
            step = next_worker_step(
                work_pending=bool(pending_segments(deps.queue_dir)),
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
            last_ended_at = item.ended_at
            try:
                if isinstance(item, _QueuedSegment):
                    result = upload_segment(
                        queued_path=item.path,
                        audio_active=item.audio_active,
                        queue_dir=deps.queue_dir,
                        storage_client=deps.storage_client,
                        status_client=status_client,
                        led_driver=deps.led_driver,
                        battery=deps.battery,
                        device_id=deps.device_id,
                        segments_uploaded_today=deps.segments_uploaded_today,
                        disk_stats_reader=deps.disk_stats_reader,
                        data_dir=deps.data_dir,
                        upload_limit=deps.upload_limit,
                        upload_health=upload_health,
                        flush_markers=flush_after_gap(item.ended_at),
                    )
                else:
                    result = on_segment_complete(
                        h264_path=item.path,
                        queue_dir=deps.queue_dir,
                        command_runner=deps.command_runner,
                        storage_client=deps.storage_client,
                        status_client=status_client,
                        led_driver=deps.led_driver,
                        battery=deps.battery,
                        device_id=deps.device_id,
                        segments_uploaded_today=deps.segments_uploaded_today,
                        framerate=deps.video.framerate,
                        disk_stats_reader=deps.disk_stats_reader,
                        data_dir=deps.data_dir,
                        upload_limit=deps.upload_limit,
                        upload_health=upload_health,
                        flush_markers=flush_after_gap(item.ended_at),
                    )
            except Exception:
                _logger.exception(
                    "uploading segment %s failed; worker continuing", item.path
                )
                continue
            deps.segments_uploaded_today = result.segments_uploaded_today

    audio = deps.audio
    _push_recording_status(
        deps,
        status_client,
        segments_uploaded_today=deps.segments_uploaded_today,
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
    last_segment: _CapturedSegment | None = None
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
                    deps.capture_runner,
                    deps.data_dir,
                    started_at,
                    deps.segment_duration_ms,
                    deps.video,
                    audio,
                    cancel=stop,
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
                        work.put(_CapturedSegment(partial, deps.monotonic()))
                    else:
                        partial.unlink()
                continue
            ended_at = deps.monotonic()
            if not _has_footage(h264_path):
                _logger.info(
                    "the capture of %s wrote no footage; discarding it",
                    h264_path.name,
                )
                h264_path.unlink(missing_ok=True)
                continue
            if stop.is_set():
                last_segment = _CapturedSegment(h264_path, ended_at)
                break
            work.put(_CapturedSegment(h264_path, ended_at))
    finally:
        stopping.set()
        queued_last: _QueuedSegment | None = None
        if last_segment is not None:
            try:
                queued_last = _QueuedSegment(
                    path=queue_segment(
                        deps.command_runner,
                        last_segment.path,
                        deps.queue_dir,
                        deps.video.framerate,
                    ),
                    audio_active=last_segment.path.suffix == ".ts",
                    ended_at=last_segment.ended_at,
                )
            except Exception:
                _logger.exception(
                    "could not queue the last segment %s", last_segment.path.name
                )
        _push_recording_status(
            deps,
            status_client,
            segments_uploaded_today=deps.segments_uploaded_today,
            recording_active=False,
            audio_active=audio is not None,
        )
        if queued_last is not None:
            work.put(queued_last)
        work.put(None)
        if halt_verdict is None:
            worker_thread.join()
        else:
            _join_halt_drain(deps, worker_thread, stop)
        _push_recording_status(
            deps,
            status_client,
            segments_uploaded_today=deps.segments_uploaded_today,
            recording_active=False,
            audio_active=audio is not None,
        )
    if halt_verdict is None:
        return LoopOutcome.STOPPED
    return _complete_battery_halt(deps, halt_verdict, worker_thread)


def ensure_paired(
    data_dir: Path,
    scanner: QrScanner,
    activator: ConnectionActivator,
    led_driver: LedDriver,
    pair: Callable[[], Pairing],
    *,
    nm_keyfile_path: Path = _NM_KEYFILE_PATH,
    max_attempts: int = _ONBOARDING_MAX_ATTEMPTS,
) -> Pairing | None:
    """The boot gate: the paired device, or None when the daemon must exit 1.

    ``UNPAIRED`` deletes any session first (a pendant onboarded the old way
    still holds its owner's) and scans for a QR. ``PENDING`` (a scanned code
    not yet claimed) and a fresh scan both bring Wi-Fi up before ``pair``,
    so a first boot with no connectivity never reaches the network. Exiting 1
    lets systemd's Restart=on-failure retry from whatever state is on disk.
    """
    state = pairing_state(data_dir)
    if state is PairingState.PAIRED:
        reactivate_connection_if_configured(
            activator, _NM_CONNECTION_ID, nm_keyfile_path
        )
        device_id = load_paired_device_id(data_dir / DEVICE_FILE)
        return None if device_id is None else Pairing(device_id=device_id)
    if state is PairingState.UNPAIRED:
        # Also drops an unreadable pairing.json, which cannot be resumed.
        (data_dir / SESSION_FILE).unlink(missing_ok=True)
        (data_dir / PAIRING_FILE).unlink(missing_ok=True)
        payload = run_onboarding(
            scanner, nm_keyfile_path, data_dir / PAIRING_FILE, max_attempts
        )
        if payload is None:
            return None
    pending_path = data_dir / PAIRING_FILE
    if not activate_connection(activator, _NM_CONNECTION_ID, led_driver):
        if record_failed_activation(pending_path):
            _logger.error(
                "Wi-Fi did not come up on %d boots in a row; dropping the pairing "
                "code and scanning for a new QR next boot (check the Wi-Fi password)",
                MAX_FAILED_PENDING_ACTIVATIONS,
            )
        return None
    reset_failed_activations(pending_path)
    try:
        return pair()
    except PairingRejectedError:
        _logger.exception("pairing was refused; scanning for a new QR next boot")
        apply_led_state(led_driver, LedState.CRITICAL)
        return None
    except PairingUnavailableError:
        _logger.exception("pairing could not finish; retrying next boot")
        return None


def pair_and_connect(
    data_dir: Path,
    scanner: QrScanner,
    activator: ConnectionActivator,
    led_driver: LedDriver,
    url: str,
    key: str,
    *,
    client_factory: Callable[[str, str], Any] = create_client,
    nm_keyfile_path: Path = _NM_KEYFILE_PATH,
    max_attempts: int = _ONBOARDING_MAX_ATTEMPTS,
) -> tuple[str, SupabaseClients] | None:
    """Pass the boot gate and sign in; None when the daemon must exit 1."""
    paired = ensure_paired(
        data_dir,
        scanner,
        activator,
        led_driver,
        lambda: complete_pairing(url, key, data_dir, client_factory),
        nm_keyfile_path=nm_keyfile_path,
        max_attempts=max_attempts,
    )
    if paired is None:
        return None
    client = paired.client
    if client is None:
        try:
            client = restore_supabase_client(
                url, key, data_dir / SESSION_FILE, client_factory=client_factory
            )
        except SessionRevokedError:
            _logger.exception("this pendant's login was revoked; scanning next boot")
            handle_revoked_login(data_dir, led_driver)
            return None
    # Exactly one client, and so one auto-refresh timer and one session.json
    # persister, per process: a pairing boot keeps the client that paired.
    return paired.device_id, supabase_clients_for(client)


def handle_revoked_login(data_dir: Path, led_driver: LedDriver) -> None:
    """Go back to scanning after Remove device or a re-pair elsewhere.

    Only a definite rejection of the login reaches here; network failures
    never unpair. Queued footage stays on the SD card.
    """
    clear_local_pairing(data_dir)
    apply_led_state(led_driver, LedState.CRITICAL)


def main() -> int:
    """Compose the daemon from its already-tested parts.

    Thin on purpose: no branching logic of its own beyond the boot-sequence
    gates the pieces it calls already own. The boot order and what each early
    exit leaves running are pinned by the ``main()`` tests with every hardware
    factory faked; the hardware itself is validated in Epic 5 (see the plan's
    Handoff section).
    """
    config = load_config(os.environ)

    queue_dir = config.data_dir / "queue"
    config.data_dir.mkdir(parents=True, exist_ok=True)

    command_runner = SubprocessCommandRunner()
    pijuice = open_pijuice()
    battery_reader = PiJuiceBatteryReader(pijuice)
    # Every LED write in the process goes through this one owner, which also
    # renders the flag acknowledgement blink on its own thread.
    led_driver = LedController(Ws2812LedDriver())
    try:
        return _boot_and_record(
            config, queue_dir, command_runner, pijuice, battery_reader, led_driver
        )
    finally:
        # Every exit closes the LED owner: the battery halt's power-off, the
        # pairing gate sending the pendant back to scanning (a revoked login
        # included) and the end of recording. On the halt path the loop has
        # already applied the Halted red, which the controller never blinks
        # over, and any blink still in flight lost to that write, so closing
        # it cannot leave anything but the red on the pixel.
        led_driver.close()


def _boot_and_record(
    config: DaemonConfig,
    queue_dir: Path,
    command_runner: CommandRunner,
    pijuice: Any | None,
    battery_reader: BatteryReader,
    led_driver: LedController,
) -> int:
    """main()'s boot order, with the LED owner main() closes on every exit.

    The battery check runs first and may power off before the pendant's
    pairing state is even read; only a boot that may record passes the
    pairing gate, which yields the one supabase client every uploader and the
    status reporter share; the flag button and its worker start only after
    that, so an early exit leaves no worker thread behind.
    """
    power_controller = resolve_power_controller(command_runner, pijuice)

    startup = run_startup_sequence(battery_reader, led_driver)
    if not startup.proceed or startup.battery is None:
        # A confirmed halt at boot must not leave the Pi powered and
        # discharging; that is what used to corrupt SD cards.
        power_controller.power_off(wake_on_charge_pct=WAKE_ON_CHARGE_PCT)
        return 0

    connected = pair_and_connect(
        config.data_dir,
        RpicamZbarScanner(),
        NmcliConnectionActivator(),
        led_driver,
        config.supabase_url,
        config.supabase_anon_key,
    )
    if connected is None:
        return 1
    device_id, clients = connected

    capture_runner = SubprocessInterruptibleRunner()
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
        # ``stop`` is also the capture's cancel event, so this ends the
        # in-flight capture as well as the loop: a systemctl stop or a
        # poweroff finishes within the unit's TimeoutStopSec instead of
        # waiting out the segment and being SIGKILLed.
        stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Held for the daemon's lifetime: gpiozero releases the pin (and the
    # callback) when the Button object is garbage collected.
    flag_upload_worker = FlagUploadWorker(clients.storage, device_id, queue_dir)
    flag_button = GpioZeroFlagButton()
    flag_button.on_press(
        make_flag_press_handler(
            queue_dir=queue_dir,
            submit=flag_upload_worker.submit,
            ack=led_driver.ack_flag,
        )
    )

    deps = LoopDeps(
        command_runner=command_runner,
        capture_runner=capture_runner,
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
        flush_markers=flag_upload_worker.flush_markers,
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
