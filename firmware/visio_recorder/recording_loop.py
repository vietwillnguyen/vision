import logging
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from visio_recorder.battery import BatteryReader, read_battery_status
from visio_recorder.led import LedDriver, LedState, apply_led_state
from visio_recorder.muxer import CommandRunner, mux_segment
from visio_recorder.upload_limit import (
    UploadLimit,
    UploadOutcome,
    rejected_dir,
    upload_queued,
)
from visio_recorder.upload_queue import enqueue, list_pending
from visio_recorder.uploader import StorageClient

_BYTES_PER_GB = 1024**3

UPLOAD_FAILURES_TO_CRITICAL = 3

RETRY_BASE_SEC = 30.0
RETRY_MAX_SEC = 480.0

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
    battery_is_low: bool, is_uploading: bool, upload_critical: bool = False
) -> LedState:
    """The one LED state a given set of conditions calls for.

    ``upload_critical`` outranks everything: a device losing footage is worth
    more than reporting a low battery or an upload in flight, and it is also
    what the battery-halt path already does by applying Critical last.
    """
    if upload_critical:
        return LedState.CRITICAL
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


def pending_segments(queue_dir: Path) -> list[Path]:
    """The queued segments the upload worker may retry while recording.

    Flag markers are left out. ``FlagUploadWorker`` uploads and unlinks those
    on its own thread for as long as the daemon runs, so draining them from
    here too would race it: both threads would see the object missing, both
    would upload, and the loser would be told the object already exists - a
    failure that is not a failure, feeding the Critical LED escalation. A
    marker keeps its documented contract instead, the next boot's
    ``flush_pending``, which runs before that worker exists.
    """
    return [path for path in list_pending(queue_dir) if path.suffix == ".mp4"]


def drain_pending(
    paths: list[Path],
    storage_client: StorageClient,
    device_id: str,
    upload_limit: UploadLimit,
    cancel: Callable[[], bool] | None = None,
) -> UploadOutcome:
    """Upload each of ``paths``, returning the totals across all of them.

    A queued segment over the upload limit (for example one recorded by an
    older firmware with five-minute segments) is split first, so ``uploaded``
    counts files that reached storage, not queue entries.

    ``cancel`` is checked before each file, not inside one: it lets the caller
    give up on a backlog (the daemon is stopping, or the battery hit the halt
    threshold) without abandoning an upload mid-flight. Whatever is left is
    still queued, so the next round or the next boot takes it.
    """
    total = UploadOutcome()
    for path in paths:
        if cancel is not None and cancel():
            _logger.info("stopping the queue drain early; %s stays queued", path.name)
            break
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
    flag-upload worker exists, so there is nothing to race.
    """
    return drain_pending(
        list_pending(queue_dir), storage_client, device_id, upload_limit
    ).uploaded


def _upload_succeeded(outcome: UploadOutcome) -> bool:
    # A rejected (unsplittable, oversized) segment is lost footage, so it
    # counts towards the Critical LED like a failed upload does.
    return outcome.failed == 0 and outcome.rejected == 0


def _upload_showing_led(
    *,
    upload: Callable[[], UploadOutcome],
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
    """
    apply_led_state(
        led_driver,
        next_led_state(
            battery_is_low,
            is_uploading=True,
            upload_critical=upload_health.is_critical,
        ),
    )
    outcome = upload()
    upload_health.record(upload_ok=_upload_succeeded(outcome))
    apply_led_state(
        led_driver,
        next_led_state(
            battery_is_low,
            is_uploading=False,
            upload_critical=upload_health.is_critical,
        ),
    )
    return outcome


def retry_pending_segments(
    *,
    queue_dir: Path,
    storage_client: StorageClient,
    led_driver: LedDriver,
    battery_reader: BatteryReader,
    device_id: str,
    upload_limit: UploadLimit,
    upload_health: UploadHealth,
    cancel: Callable[[], bool] | None = None,
) -> UploadOutcome:
    """One in-process retry round over the segments still queued.

    Called by the upload worker when its backoff elapses with nothing new to
    do, so a segment whose upload failed is retried during the same run rather
    than waiting for the next boot's ``flush_pending``. It is the same upload
    path ``on_segment_complete`` uses, so the 50 MB limit is handled the same
    way: an oversized segment is split and its parts uploaded, and one that
    cannot be split is moved to ``rejected/`` and so leaves the queue for
    good - it can never become a retry loop.

    The round's outcome feeds the same ``UploadHealth`` counter a segment's own
    upload does, so retry failures escalate to Critical and a retry success
    clears it. An empty queue is a no-op that deliberately leaves the counter
    alone: reporting "nothing failed" would clear Critical without anything
    having reached storage.

    No ``device_status`` upsert of its own: the next segment completes within
    one segment period and reports the new ``segments_pending`` and
    ``segments_uploaded_today`` then, which keeps this round off the status
    client's failure path and out of the question of what ``audio_active``
    would mean with no capture involved.
    """
    paths = pending_segments(queue_dir)
    if not paths:
        return UploadOutcome()
    _logger.info("retrying %d queued segment(s) in process", len(paths))
    battery_status = read_battery_status(battery_reader)
    return _upload_showing_led(
        upload=lambda: drain_pending(
            paths, storage_client, device_id, upload_limit, cancel
        ),
        led_driver=led_driver,
        battery_is_low=battery_status.is_low,
        upload_health=upload_health,
    )


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
    upload_health: UploadHealth | None = None,
) -> SegmentResult:
    """Mux, queue and upload one finished capture, driving the LED and status.

    ``upload_health`` is the worker's run-long consecutive-failure counter.
    Both LED states are derived from it, so a run already escalated to
    Critical keeps showing red through this segment's whole upload instead of
    blinking blue then green; the counter is updated from this segment's
    outcome before the second LED call, so a success clears the red here
    rather than one segment later. Omitting it uploads the segment with no
    escalation history, which is what a single-segment caller wants.
    """
    upload_health = UploadHealth() if upload_health is None else upload_health
    mp4_path = mux_segment(command_runner, h264_path, framerate)
    h264_path.unlink()
    queued_path = enqueue(queue_dir, mp4_path)

    battery_status = read_battery_status(battery_reader)
    outcome = _upload_showing_led(
        upload=lambda: upload_queued(
            queued_path, storage_client, device_id, upload_limit
        ),
        led_driver=led_driver,
        battery_is_low=battery_status.is_low,
        upload_health=upload_health,
    )
    segments_uploaded_today += outcome.uploaded
    upload_ok = _upload_succeeded(outcome)

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
