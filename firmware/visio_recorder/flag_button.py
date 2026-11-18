import logging
import os
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from visio_recorder.recording_loop import RetryBackoff, WorkerAction, next_worker_step
from visio_recorder.upload_queue import mark_uploaded, pending_markers
from visio_recorder.uploader import StorageClient, upload_segment

FLAG_PRESS_COOLDOWN = timedelta(seconds=2)

# How long the upload worker will wait for a marker flush before uploading
# its segment anyway. Bounded because footage is worth more than marker
# ordering, and the pipeline's late-marker handling is the backstop.
MARKER_FLUSH_TIMEOUT_SEC = 30.0

# Named so logs, and the test that pins the single-uploader invariant,
# can tell this thread's uploads from the segment worker's.
FLAG_UPLOAD_THREAD_NAME = "flag-upload-worker"

_logger = logging.getLogger(__name__)


class FlagButton(Protocol):
    def on_press(self, callback: Callable[[], None]) -> None: ...


def write_flag_marker(queue_dir: Path, pressed_at: datetime) -> Path:
    """Create the marker and make it survive a power cut before returning.

    The press is local-first: the device is the source of truth for a flag
    and the network is an opportunistic sync, so "the press landed" has to
    mean the marker is on the card, not that it reached storage. The marker
    is empty, so syncing its data costs almost nothing; the directory entry
    is the part actually at risk, and syncing the queue directory is what
    makes the name durable.
    """
    queue_dir.mkdir(parents=True, exist_ok=True)
    marker_path = queue_dir / f"FLAG_{pressed_at.strftime('%Y%m%d_%H%M%S')}.marker"
    fd = os.open(marker_path, os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC, 0o644)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    dir_fd = os.open(queue_dir, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return marker_path


@dataclass
class _FlushRequest:
    """A caller asking for every pending marker to be uploaded now."""

    done: threading.Event = field(default_factory=threading.Event)
    all_uploaded: bool = False


class FlagUploadWorker:
    """Owns every flag-marker upload on a single dedicated thread.

    Press callbacks run on gpiozero's internal callback thread, which must
    never block on the network (a slow upload would delay detection of later
    presses), so they only hand the marker path over via ``submit``. The
    marker is still uploaded immediately - as soon as this thread picks it
    up - so it reaches the pipeline before tonight's run.

    A failed upload no longer waits for the next boot. This thread retries
    whatever is still queued for as long as the daemon runs, on the schedule
    segments use: while markers are pending it blocks on its work queue only
    until a retry deadline, and ``next_worker_step`` arms that deadline from
    the shared ``RetryBackoff``. The counter indexing it is this worker's
    own, not the upload worker's ``UploadHealth``: two threads must not write
    one counter, and a marker is metadata rather than footage, so a marker
    that cannot be sent must not by itself escalate the Critical LED that
    means this device is losing video. A real outage fails the segments
    alongside it and escalates there anyway.

    A round stops at its first failure. Markers are empty files, so one
    refusal already proves the network is down and trying the rest would only
    spend radio power to learn the same thing.

    ``flush_markers`` is the other half of the local-first contract: the
    upload worker calls it before a segment and waits, so a segment never
    reaches storage ahead of the flags the pipeline would score it with. It
    is a request on *this* worker's queue rather than an upload the caller
    makes, so a marker still has exactly one uploader and the two threads
    never race one.
    """

    def __init__(
        self,
        storage_client: StorageClient,
        device_id: str,
        queue_dir: Path,
        retry_backoff: RetryBackoff | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        flush_timeout_sec: float = MARKER_FLUSH_TIMEOUT_SEC,
    ) -> None:
        self._storage_client = storage_client
        self._device_id = device_id
        self._queue_dir = queue_dir
        self._retry_backoff = RetryBackoff() if retry_backoff is None else retry_backoff
        self._monotonic = monotonic
        self._flush_timeout_sec = flush_timeout_sec
        self._consecutive_failures = 0
        self._work: queue.Queue[Path | _FlushRequest | None] = queue.Queue()
        # Set before the sentinel, so a worker on its way out starts no round.
        self._stopping = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=FLAG_UPLOAD_THREAD_NAME, daemon=True
        )
        self._thread.start()

    def submit(self, marker_path: Path) -> None:
        self._work.put(marker_path)

    def flush_markers(self, timeout: float | None = None) -> bool:
        """Upload every queued marker now; True if none are left behind.

        Blocks until this worker has run the flush, or until ``timeout``.
        A worker that has already stopped reports False at once rather than
        stalling the caller for the whole timeout.
        """
        if self._stopping.is_set():
            return False
        request = _FlushRequest()
        self._work.put(request)
        wait = self._flush_timeout_sec if timeout is None else timeout
        if not request.done.wait(wait):
            _logger.warning(
                "flag markers were still being flushed after %.0fs; "
                "uploading the segment anyway",
                wait,
            )
            return False
        return request.all_uploaded

    def stop(self) -> None:
        """Drain queued markers, then join the worker thread."""
        self._stopping.set()
        self._work.put(None)
        self._thread.join()
        # Anything that raced the sentinel onto the queue would otherwise
        # wait out its caller's whole timeout.
        while True:
            try:
                item = self._work.get_nowait()
            except queue.Empty:
                return
            if isinstance(item, _FlushRequest):
                item.done.set()

    def _run(self) -> None:
        retry_at: float | None = None
        while True:
            now = self._monotonic()
            step = next_worker_step(
                work_pending=bool(pending_markers(self._queue_dir)),
                stopping=self._stopping.is_set(),
                retry_at=retry_at,
                now=now,
                retry_backoff=self._retry_backoff,
                consecutive_failures=self._consecutive_failures,
            )
            retry_at = step.retry_at
            try:
                if step.action is WorkerAction.RETRY_NOW:
                    raise queue.Empty
                item = self._work.get(
                    timeout=None if retry_at is None else retry_at - now
                )
            except queue.Empty:
                self._retry_round()
                retry_at = None
                continue
            if item is None:
                return
            if isinstance(item, _FlushRequest):
                item.all_uploaded = self._retry_round()
                item.done.set()
                # The flush is a round, so the next deadline is armed afresh.
                retry_at = None
                continue
            self._upload(item)

    def _retry_round(self) -> bool:
        """Upload what is queued, oldest first; True if nothing was left."""
        for marker_path in pending_markers(self._queue_dir):
            if self._stopping.is_set():
                return False
            if not self._upload(marker_path):
                return False
        return True

    def _upload(self, marker_path: Path) -> bool:
        if not marker_path.exists():
            # A flush or a retry round reads the queue rather than the work
            # item, so it can reach a marker before the press's own hand-off
            # is dequeued. Everything here runs on one thread, so a path that
            # is gone is one this worker has already sent: re-uploading it
            # would push the same object twice and then fail on the unlink.
            return True
        try:
            upload_segment(self._storage_client, self._device_id, marker_path)
        except Exception:
            self._consecutive_failures += 1
            _logger.exception(
                "flag marker upload failed; %s stays queued for the next retry",
                marker_path,
            )
            return False
        self._consecutive_failures = 0
        mark_uploaded(marker_path)
        return True


def make_flag_press_handler(
    queue_dir: Path,
    submit: Callable[[Path], None],
    ack: Callable[[], None] | None = None,
    clock: Callable[[], datetime] = datetime.now,
    cooldown: timedelta = FLAG_PRESS_COOLDOWN,
) -> Callable[[], None]:
    """Build the callback a button press invokes.

    Presses inside ``cooldown`` of the last accepted press are dropped (switch
    bounce and accidental double-taps; second-resolution marker names collide
    anyway). The callback only debounces, writes and syncs the marker, asks
    for the acknowledgement blink, and hands the path to ``submit`` (the
    flag-upload worker). It never touches the network and never touches the
    LED driver, so gpiozero's callback thread stays responsive and the LED
    keeps its single owner.

    ``ack`` is ordered after the sync and before ``submit``: the wearer is
    told the press landed because it is durable, not because it was sent.
    """
    last_accepted: datetime | None = None

    def on_press() -> None:
        nonlocal last_accepted
        pressed_at = clock()
        if last_accepted is not None and pressed_at - last_accepted < cooldown:
            return
        last_accepted = pressed_at
        marker_path = write_flag_marker(queue_dir, pressed_at)
        if ack is not None:
            ack()
        submit(marker_path)

    return on_press
