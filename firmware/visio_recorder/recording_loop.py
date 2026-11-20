import logging
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Protocol

from visio_recorder.battery import BatteryState
from visio_recorder.led import LedDriver, LedState, apply_led_state
from visio_recorder.muxer import CommandRunner, mux_segment
from visio_recorder.upload_limit import (
    UploadLimit,
    UploadOutcome,
    rejected_dir,
    upload_queued,
)
from visio_recorder.upload_queue import enqueue, list_pending, markers_before_segments
from visio_recorder.uploader import StorageClient

_BYTES_PER_GB = 1024**3

UPLOAD_FAILURES_TO_CRITICAL = 3

RETRY_BASE_SEC = 30.0
RETRY_MAX_SEC = 480.0

# How long a battery halt waits for the upload queue to drain before powering
# off. Two minutes at the 1.5 W power budget is about 0.05 Wh out of an 11.1 Wh
# pack, so the cost is not the energy: it is that a bound has to exist at all,
# because a dying battery is exactly when an upload is most likely to hang on a
# weak signal and an unbounded wait ends in a brownout rather than a power-off.
UPLOAD_DRAIN_TIMEOUT_SEC = 120.0
# The pipeline's RESTART_GAP_TOLERANCE_SEC: a press this long after a segment
# ends still flags that segment, so it is how long the segment must wait for
# its flags before it is sent.
RESTART_GAP_SEC = 3.0

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryBackoff:
    """How long the upload worker waits before retrying the queue in process.

    The wait doubles per consecutive failure and is capped, so a brief WiFi
    drop is recovered from within half a minute while a long outage settles
    into one attempt every eight minutes instead of hammering a dead network
    on a battery. The cap matters more than the base: an outage can last all
    day, and every attempt costs radio power the pendant is running on.

    The defaults are read against a 60 s segment (``segment_size``): the first
    retry lands inside one segment period, and even the capped wait is short
    next to the hours a next-boot-only retry could cost.
    """

    base_sec: float = RETRY_BASE_SEC
    max_sec: float = RETRY_MAX_SEC

    def delay_sec(self, consecutive_failures: int) -> float:
        """Seconds to wait before the next retry round.

        ``consecutive_failures`` is ``UploadHealth``'s counter, so the schedule
        is driven by the same state that drives the Critical LED: the wait is
        reset by exactly the thing that clears the red, a successful upload.
        Zero failures (a queue inherited from the startup flush) waits the base
        delay.
        """
        if consecutive_failures <= 1:
            return self.base_sec
        return min(self.base_sec * 2.0 ** (consecutive_failures - 1), self.max_sec)


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


@dataclass
class UploadHealth:
    """How many uploads have failed in a row, and so whether to show Critical.

    The upload worker owns one of these for the whole run.
    It exists because the Critical LED is a *latched* condition, not a
    per-segment one: the spec says three consecutive upload failures escalate
    to the Critical red flash and "any subsequent successful upload resets the
    counter", so from the escalation until that success the LED must stay red.
    Deriving the LED state from this counter is what keeps a later segment's
    Uploading blue and Recording green from painting over the red; before this
    existed the counter lived in the worker, which re-applied Critical only
    *after* ``on_segment_complete`` had already written both of those colours.

    A rejected segment counts as a failure like a failed upload does, so
    ``record`` takes the whole segment outcome, not just the upload's.
    """

    consecutive_failures: int = 0

    @property
    def is_critical(self) -> bool:
        return self.consecutive_failures >= UPLOAD_FAILURES_TO_CRITICAL

    def record(self, *, upload_ok: bool) -> None:
        if upload_ok:
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1


def next_led_state(
    battery_is_low: bool,
    is_uploading: bool,
    upload_critical: bool = False,
    battery_halted: bool = False,
) -> LedState:
    """The one LED state a given set of conditions calls for.

    The conditions are not mutually exclusive - a pendant can be low on
    battery, uploading and halting at once - so this is a priority ladder, and
    it is the single place that ladder is written down. The pendant spec's LED
    state machine section owns the target design; its pure resolver replaces
    this function as the capture region, health conditions and the states the
    recording stop/start control design adds land.

    ``battery_halted`` is first. A halt is terminal, and the solid red is the
    pendant's only chance to tell the wearer *why* it is about to go dark;
    "charge me" is the one actionable message at that moment. It is Halted,
    not Critical: Critical's flash means "still recording, needs you", while a
    halt has stopped recording. The halt path applies it last, after the
    upload worker is joined, so no drained segment's LED restore can paint
    over it - the invariant this function exists to hold.

    ``upload_critical`` outranks the running states below it for the same
    reason the halt outranks it: a device losing footage is worth more than
    reporting a low battery or an upload in flight.
    """
    if battery_halted:
        return LedState.HALTED
    if upload_critical:
        return LedState.CRITICAL
    if battery_is_low:
        return LedState.LOW_BATTERY
    if is_uploading:
        return LedState.UPLOADING
    return LedState.RECORDING


class LoopOutcome(Enum):
    """Why the recording loop returned, and so what the daemon does next.

    ``RECOVERED`` is the one that is not an ending: external power appeared
    while a battery halt was draining its queue, so the power-off was
    cancelled and the loop should be re-entered rather than the process
    exiting. Without it a pendant plugged in at the wrong moment would power
    off anyway and wait for the charge wake, which is the "stays down until
    someone intervenes" behaviour this replaces.
    """

    STOPPED = "stopped"
    HALTED = "halted"
    RECOVERED = "recovered"


def wait_out_restart_gap(
    ended_at: float,
    *,
    gap_sec: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> None:
    """Sleep until ``gap_sec`` after ``ended_at``, and never longer than that.

    ``ended_at`` is when the segment's capture returned, on ``monotonic``. A
    press up to ``gap_sec`` later belongs to that segment, so flushing markers
    any earlier would let the segment reach storage ahead of its own flag.
    The wait is capped at ``gap_sec`` whatever the clock says, so neither a
    stop, a halt, nor a clock step can hold an upload for longer.
    """
    remaining = min(ended_at + gap_sec - monotonic(), gap_sec)
    if remaining > 0:
        sleep(remaining)


class WorkerAction(Enum):
    WAIT_FOR_SEGMENT = "wait_for_segment"
    WAIT_UNTIL_RETRY = "wait_until_retry"
    RETRY_NOW = "retry_now"


@dataclass(frozen=True)
class WorkerStep:
    action: WorkerAction
    retry_at: float | None


def next_worker_step(
    *,
    work_pending: bool,
    stopping: bool,
    retry_at: float | None,
    now: float,
    retry_backoff: RetryBackoff,
    consecutive_failures: int,
) -> WorkerStep:
    """What the upload worker does next, and the retry deadline it keeps.

    Pulled out of the worker thread for the same reason as ``next_led_state``:
    the decision is the part worth pinning, and here it needs no threads.
    Both upload workers use it - the segment one over ``pending_segments``,
    ``FlagUploadWorker`` over ``pending_markers`` - each with its own
    consecutive-failure counter, so ``work_pending`` is whatever that caller
    still has to send.

    With nothing queued, or the loop stopping, there is no deadline and the
    worker waits for its next segment or the shutdown sentinel; a stopping
    worker never starts a round, since ``cancel`` would end it before its
    first file. Otherwise the deadline is armed once, ``retry_backoff`` after
    ``now`` for the live ``consecutive_failures``, and kept as it is until the
    round it schedules has run: a segment arriving consumes the remaining wait
    rather than restarting it, so segments arriving more often than the
    backoff cannot postpone the retry. Once it has passed, retry now.
    """
    if stopping or not work_pending:
        return WorkerStep(WorkerAction.WAIT_FOR_SEGMENT, retry_at=None)
    if retry_at is None:
        retry_at = now + retry_backoff.delay_sec(consecutive_failures)
    if retry_at <= now:
        return WorkerStep(WorkerAction.RETRY_NOW, retry_at=retry_at)
    return WorkerStep(WorkerAction.WAIT_UNTIL_RETRY, retry_at=retry_at)


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


def pending_segments(queue_dir: Path) -> list[Path]:
    """The queued segments the upload worker may retry while recording.

    Flag markers are left out. ``FlagUploadWorker`` uploads and unlinks those
    on its own thread for as long as the daemon runs - including its own
    in-process retry - so draining them from here too would race it: both
    threads would see the object missing, both would upload, and the loser
    would be told the object already exists, a failure that is not a failure,
    feeding the Critical LED escalation. The ordering this worker does need,
    markers before its segments, it gets by asking that worker for a flush
    (``flush_markers`` below) rather than by uploading one itself.
    """
    return [path for path in list_pending(queue_dir) if path.suffix == ".mp4"]


def drain_pending(
    paths: list[Path],
    storage_client: StorageClient,
    device_id: str,
    upload_limit: UploadLimit,
    cancel: Callable[[], bool] | None = None,
    before_first_attempt: Callable[[], None] | None = None,
) -> UploadOutcome:
    """Upload each of ``paths``, returning the totals across all of them.

    A queued segment over the upload limit (for example one recorded by an
    older firmware with five-minute segments) is split first, so ``uploaded``
    counts files that reached storage, not queue entries.

    ``cancel`` is checked before each file, not inside one: it lets the caller
    give up on a backlog (the daemon is stopping, or the battery hit the halt
    threshold) without abandoning an upload mid-flight. Whatever is left is
    still queued, so the next round or the next boot takes it.

    ``before_first_attempt`` runs once, just before the first upload is tried,
    and not at all when nothing is: ``attempted`` on the result says how many
    files were tried, so a caller can tell a drain that did nothing from one
    whose every upload succeeded.
    """
    total = UploadOutcome()
    for path in paths:
        if cancel is not None and cancel():
            _logger.info("stopping the queue drain early; %s stays queued", path.name)
            break
        if total.attempted == 0 and before_first_attempt is not None:
            before_first_attempt()
        total.attempted += 1
        try:
            outcome = upload_queued(path, storage_client, device_id, upload_limit)
        except Exception:
            _logger.exception("failed to upload queued segment %s", path)
            total.failed += 1
            continue
        total.uploaded += outcome.uploaded
        total.failed += outcome.failed
        total.rejected += outcome.rejected
    return total


def flush_pending(
    queue_dir: Path,
    storage_client: StorageClient,
    device_id: str,
    upload_limit: UploadLimit,
) -> int:
    """Upload everything queued at startup; returns how many reached storage.

    Flag markers included, unlike ``pending_segments``: this runs before the
    flag-upload worker exists, so there is nothing to race - and they go
    first. A boot that cannot finish the backlog (the battery goes, WiFi goes
    again, the daemon is stopped) would otherwise send a day's segments and
    leave their flags on the card, and the nightly run scores a segment
    against the markers that are in storage when it runs.
    """
    return drain_pending(
        markers_before_segments(list_pending(queue_dir)),
        storage_client,
        device_id,
        upload_limit,
    ).uploaded


def _upload_succeeded(outcome: UploadOutcome) -> bool:
    # A rejected (unsplittable, oversized) segment is lost footage, so it
    # counts towards the Critical LED like a failed upload does.
    return outcome.failed == 0 and outcome.rejected == 0


def _show_upload_led(
    led_driver: LedDriver,
    *,
    battery_is_low: bool,
    is_uploading: bool,
    upload_health: UploadHealth,
) -> None:
    apply_led_state(
        led_driver,
        next_led_state(
            battery_is_low,
            is_uploading=is_uploading,
            upload_critical=upload_health.is_critical,
        ),
    )


def _upload_showing_led(
    *,
    upload: Callable[[Callable[[], None]], UploadOutcome],
    led_driver: LedDriver,
    battery_is_low: bool,
    upload_health: UploadHealth,
) -> UploadOutcome:
    """Run one upload attempt bracketed by the LED states it should show.

    Both states are derived from ``upload_health``, which is also updated from
    the attempt's own outcome before the second one. So a run already
    escalated to Critical shows red for the whole attempt instead of blue then
    green, and the success that resets the counter turns the LED green on its
    own attempt rather than one attempt later.

    ``upload`` is handed the call that shows the first state and makes it just
    before it tries a file. One that tries none reports ``attempted == 0`` and
    leaves both the LED and ``upload_health`` untouched: "nothing failed"
    would otherwise clear Critical without anything having reached storage.
    """
    outcome = upload(
        lambda: _show_upload_led(
            led_driver,
            battery_is_low=battery_is_low,
            is_uploading=True,
            upload_health=upload_health,
        )
    )
    if outcome.attempted == 0:
        return outcome
    upload_health.record(upload_ok=_upload_succeeded(outcome))
    _show_upload_led(
        led_driver,
        battery_is_low=battery_is_low,
        is_uploading=False,
        upload_health=upload_health,
    )
    return outcome


def retry_pending_segments(
    *,
    queue_dir: Path,
    storage_client: StorageClient,
    led_driver: LedDriver,
    battery: BatteryState,
    device_id: str,
    upload_limit: UploadLimit,
    upload_health: UploadHealth,
    cancel: Callable[[], bool] | None = None,
    flush_markers: Callable[[], bool] | None = None,
) -> UploadOutcome:
    """One in-process retry round over the segments still queued.

    Called by the upload worker when its retry deadline passes, so a segment
    whose upload failed is retried during the same run rather than waiting for
    the next boot's ``flush_pending``. It is the same upload
    path ``on_segment_complete`` uses, so the 50 MB limit is handled the same
    way: an oversized segment is split and its parts uploaded, and one that
    cannot be split is moved to ``rejected/`` and so leaves the queue for
    good - it can never become a retry loop.

    The round's outcome feeds the same ``UploadHealth`` counter a segment's own
    upload does, so retry failures escalate to Critical and a retry success
    clears it. A round that tries no file, because the queue is empty or
    ``cancel`` was already true, is a no-op that deliberately leaves the
    counter and the LED alone: reporting "nothing failed" would clear Critical
    without anything having reached storage.

    No ``device_status`` upsert of its own: the next segment completes within
    one segment period and reports the new ``segments_pending`` and
    ``segments_uploaded_today`` then, which keeps this round off the status
    client's failure path and out of the question of what ``audio_active``
    would mean with no capture involved.

    ``flush_markers`` is ``FlagUploadWorker.flush_markers``, called once just
    before the round's first segment and not at all by a round that uploads
    nothing, so flags reach storage ahead of the footage they belong to
    without spending a request per round on a network no segment is about to
    use either. Its result is deliberately ignored: a marker that could not
    be sent is not lost footage, so the segments go up regardless and the
    pipeline's late-marker handling is the backstop.
    """
    paths = pending_segments(queue_dir)
    # Read, never sampled: the main loop owns the debounce, and folding a
    # worker-thread reading into it would corrupt the moving average.
    battery_verdict = battery.current()

    def drain(show_uploading: Callable[[], None]) -> UploadOutcome:
        def before_first_attempt() -> None:
            _logger.info("retrying %d queued segment(s) in process", len(paths))
            show_uploading()
            if flush_markers is not None:
                flush_markers()

        return drain_pending(
            paths, storage_client, device_id, upload_limit, cancel, before_first_attempt
        )

    return _upload_showing_led(
        upload=drain,
        led_driver=led_driver,
        battery_is_low=battery_verdict.is_low,
        upload_health=upload_health,
    )


def queue_segment(
    command_runner: CommandRunner, h264_path: Path, queue_dir: Path, framerate: int
) -> Path:
    """Mux one finished capture into the upload queue and return its queued path.

    The raw capture is removed once the MP4 exists, so from here on the queue
    is the segment's only copy and the next boot's ``flush_pending`` recovers
    it if this run never uploads it.
    """
    mp4_path = mux_segment(command_runner, h264_path, framerate)
    h264_path.unlink()
    return enqueue(queue_dir, mp4_path)


def on_segment_complete(
    h264_path: Path,
    queue_dir: Path,
    command_runner: CommandRunner,
    storage_client: StorageClient,
    status_client: StatusClient,
    led_driver: LedDriver,
    battery: BatteryState,
    device_id: str,
    segments_uploaded_today: int,
    framerate: int,
    disk_stats_reader: DiskStatsReader,
    data_dir: Path,
    upload_limit: UploadLimit,
    upload_health: UploadHealth | None = None,
    flush_markers: Callable[[], bool] | None = None,
) -> SegmentResult:
    """Mux, queue and upload one finished capture: ``queue_segment`` then
    ``upload_segment``."""
    return upload_segment(
        queued_path=queue_segment(command_runner, h264_path, queue_dir, framerate),
        # Per segment: a .ts capture carries the microphone track.
        audio_active=h264_path.suffix == ".ts",
        queue_dir=queue_dir,
        storage_client=storage_client,
        status_client=status_client,
        led_driver=led_driver,
        battery=battery,
        device_id=device_id,
        segments_uploaded_today=segments_uploaded_today,
        disk_stats_reader=disk_stats_reader,
        data_dir=data_dir,
        upload_limit=upload_limit,
        upload_health=upload_health,
        flush_markers=flush_markers,
    )


def upload_segment(
    queued_path: Path,
    audio_active: bool,
    queue_dir: Path,
    storage_client: StorageClient,
    status_client: StatusClient,
    led_driver: LedDriver,
    battery: BatteryState,
    device_id: str,
    segments_uploaded_today: int,
    disk_stats_reader: DiskStatsReader,
    data_dir: Path,
    upload_limit: UploadLimit,
    upload_health: UploadHealth | None = None,
    flush_markers: Callable[[], bool] | None = None,
) -> SegmentResult:
    """Upload one queued segment, driving the LED and reporting the status.

    ``upload_health`` is the worker's run-long consecutive-failure counter.
    Both LED states are derived from it, so a run already escalated to
    Critical keeps showing red through this segment's whole upload instead of
    blinking blue then green; the counter is updated from this segment's
    outcome before the second LED call, so a success clears the red here
    rather than one segment later. Omitting it uploads the segment with no
    escalation history, which is what a single-segment caller wants.

    ``flush_markers`` is ``FlagUploadWorker.flush_markers``. It runs inside
    the Uploading LED, before this segment is sent, so a flag pressed during
    the segment reaches storage with the footage it belongs to rather than
    after it. Its result is ignored on purpose: footage outranks marker
    ordering, and a marker that could not be sent must not count towards the
    Critical escalation that means lost video.
    """
    upload_health = UploadHealth() if upload_health is None else upload_health
    # Read, never sampled: the main loop owns the debounce.
    battery_verdict = battery.current()

    def upload(show_uploading: Callable[[], None]) -> UploadOutcome:
        show_uploading()
        if flush_markers is not None:
            flush_markers()
        return replace(
            upload_queued(queued_path, storage_client, device_id, upload_limit),
            attempted=1,
        )

    outcome = _upload_showing_led(
        upload=upload,
        led_driver=led_driver,
        battery_is_low=battery_verdict.is_low,
        upload_health=upload_health,
    )
    segments_uploaded_today += outcome.uploaded
    upload_ok = _upload_succeeded(outcome)

    status = build_device_status(
        device_id=device_id,
        battery_pct=battery_verdict.pct,
        disk_stats_reader=disk_stats_reader,
        data_dir=data_dir,
        queue_dir=queue_dir,
        segments_uploaded_today=segments_uploaded_today,
        recording_active=True,
        audio_active=audio_active,
    )
    status_client.upsert_device_status(asdict(status))

    return SegmentResult(
        segments_uploaded_today=segments_uploaded_today, upload_ok=upload_ok
    )
