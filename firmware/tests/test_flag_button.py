import os
import stat
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from tests.fakes import FailingStorageClient, FakeClock, FakeStorageClient
from visio_recorder.flag_button import (
    FlagUploadWorker,
    make_flag_press_handler,
    write_flag_marker,
)
from visio_recorder.recording_loop import RetryBackoff
from visio_recorder.upload_queue import pending_markers
from visio_recorder.uploader import StorageClient

# A deadline no test clock ever reaches, for the cases that are about the
# press-time upload rather than the retry.
_NO_RETRY = RetryBackoff(base_sec=3600.0, max_sec=3600.0)


def test_write_flag_marker_creates_timestamped_file(tmp_path):
    queue_dir = tmp_path / "queue"
    pressed_at = datetime(2026, 7, 4, 12, 3, 45)

    marker_path = write_flag_marker(queue_dir, pressed_at)

    assert marker_path == queue_dir / "FLAG_20260704_120345.marker"
    assert marker_path.exists()


def test_write_flag_marker_name_matches_the_pipeline_parser_contract(tmp_path):
    # Pins the marker filename to the format the pipeline's
    # parse_flag_marker_filename hard-requires (FLAG_YYYYMMDD_HHMMSS.marker).
    pressed_at = datetime(2026, 7, 4, 12, 3, 45)

    marker_path = write_flag_marker(tmp_path / "queue", pressed_at)

    parsed = datetime.strptime(marker_path.name, "FLAG_%Y%m%d_%H%M%S.marker")
    assert parsed == pressed_at


def test_write_flag_marker_creates_queue_dir_if_missing(tmp_path):
    queue_dir = tmp_path / "does" / "not" / "exist"
    write_flag_marker(queue_dir, datetime(2026, 7, 4, 8, 0, 0))
    assert queue_dir.exists()


def _fsync_spy(monkeypatch, log=None):
    """Record what every os.fsync call synced: (is_a_directory, inode)."""
    synced: list[tuple[bool, int]] = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        info = os.fstat(fd)
        synced.append((stat.S_ISDIR(info.st_mode), info.st_ino))
        if log is not None:
            log.append("fsync")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    return synced


def test_write_flag_marker_syncs_the_file_and_the_queue_directory(
    tmp_path, monkeypatch
):
    # The press is local-first: it is complete once the marker survives a
    # power cut, not once it reaches storage. For an empty file the directory
    # entry is the part that is actually at risk, so both are synced.
    queue_dir = tmp_path / "queue"
    synced = _fsync_spy(monkeypatch)

    marker_path = write_flag_marker(queue_dir, datetime(2026, 7, 4, 12, 3, 45))

    assert (False, marker_path.stat().st_ino) in synced
    assert (True, queue_dir.stat().st_ino) in synced


def test_write_flag_marker_syncs_the_directory_after_the_file(tmp_path, monkeypatch):
    queue_dir = tmp_path / "queue"
    synced = _fsync_spy(monkeypatch)

    marker_path = write_flag_marker(queue_dir, datetime(2026, 7, 4, 12, 3, 45))

    file_sync = synced.index((False, marker_path.stat().st_ino))
    dir_sync = synced.index((True, queue_dir.stat().st_ino))
    assert dir_sync > file_sync


def test_write_flag_marker_leaves_no_descriptors_open(tmp_path):
    # It runs on gpiozero's callback thread for the life of the daemon, so a
    # leaked descriptor per press would eventually exhaust the process.
    queue_dir = tmp_path / "queue"
    before = len(os.listdir("/proc/self/fd"))
    for second in range(20):
        write_flag_marker(queue_dir, datetime(2026, 7, 4, 12, 0, second))
    assert len(os.listdir("/proc/self/fd")) == before


def test_press_handler_acks_once_the_marker_is_durable_and_before_the_upload(
    tmp_path, monkeypatch
):
    # Independent of WiFi by construction: the acknowledgement is ordered
    # after the syncs and before the path ever reaches the upload worker.
    events: list[str] = []
    _fsync_spy(monkeypatch, log=events)
    handler = make_flag_press_handler(
        queue_dir=tmp_path / "queue",
        submit=lambda path: events.append("submit"),
        ack=lambda: events.append("ack"),
        clock=FakeClock(datetime(2026, 7, 4, 12, 3, 45)),
    )

    handler()

    assert events == ["fsync", "fsync", "ack", "submit"]


def test_a_press_dropped_by_the_cooldown_does_not_ack(tmp_path):
    acks: list[str] = []
    handler = make_flag_press_handler(
        queue_dir=tmp_path / "queue",
        submit=lambda path: None,
        ack=lambda: acks.append("ack"),
        clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0), step_seconds=1),
        cooldown=timedelta(seconds=2),
    )

    handler()  # accepted
    handler()  # inside the cooldown

    assert acks == ["ack"]


def test_press_handler_writes_marker_and_submits_it_for_upload(tmp_path):
    queue_dir = tmp_path / "queue"
    submitted: list[Path] = []
    handler = make_flag_press_handler(
        queue_dir=queue_dir,
        submit=submitted.append,
        clock=FakeClock(datetime(2026, 7, 4, 12, 3, 45)),
    )

    handler()

    assert submitted == [queue_dir / "FLAG_20260704_120345.marker"]
    assert submitted[0].exists()


def test_press_handler_ignores_presses_within_the_cooldown(tmp_path):
    queue_dir = tmp_path / "queue"
    submitted: list[Path] = []
    # 1s between presses, under the 2s cooldown: only every other press lands.
    handler = make_flag_press_handler(
        queue_dir=queue_dir,
        submit=submitted.append,
        clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0), step_seconds=1),
        cooldown=timedelta(seconds=2),
    )

    handler()  # 12:00:00 - accepted
    handler()  # 12:00:01 - within cooldown, ignored
    handler()  # 12:00:02 - cooldown elapsed, accepted

    assert [p.name for p in submitted] == [
        "FLAG_20260704_120000.marker",
        "FLAG_20260704_120002.marker",
    ]


class BlockingStorageClient(StorageClient):
    """Blocks each upload until ``gate`` is set; records what it uploaded."""

    def __init__(self, gate: threading.Event) -> None:
        self._gate = gate
        self.uploaded: list[str] = []

    def upload(self, bucket: str, object_path: str, local_path: Path) -> None:
        assert self._gate.wait(timeout=5), "gate never opened"
        self.uploaded.append(object_path)


def test_press_handler_does_not_block_on_a_slow_upload(tmp_path):
    # The press callback runs on gpiozero's callback thread; a hanging upload
    # must not stall it. The handler returns while the upload is still gated.
    queue_dir = tmp_path / "queue"
    gate = threading.Event()
    storage = BlockingStorageClient(gate)
    worker = FlagUploadWorker(storage, "dev-1", queue_dir)
    handler = make_flag_press_handler(
        queue_dir=queue_dir,
        submit=worker.submit,
        clock=FakeClock(datetime(2026, 7, 4, 12, 3, 45)),
    )

    handler()

    assert storage.uploaded == []
    assert [p.name for p in queue_dir.iterdir()] == ["FLAG_20260704_120345.marker"]
    gate.set()
    worker.stop()
    assert storage.uploaded == ["dev-1/FLAG_20260704_120345.marker"]


def test_press_to_upload_clears_marker_from_the_queue(tmp_path):
    queue_dir = tmp_path / "queue"
    storage = FakeStorageClient()
    worker = FlagUploadWorker(storage, "dev-1", queue_dir)
    handler = make_flag_press_handler(
        queue_dir=queue_dir,
        submit=worker.submit,
        clock=FakeClock(datetime(2026, 7, 4, 12, 3, 45)),
    )

    handler()
    worker.stop()

    assert storage.uploaded == ["dev-1/FLAG_20260704_120345.marker"]
    assert list(queue_dir.iterdir()) == []


def test_worker_leaves_marker_queued_when_upload_fails(tmp_path):
    queue_dir = tmp_path / "queue"
    worker = FlagUploadWorker(
        FailingStorageClient(), "dev-1", queue_dir, retry_backoff=_NO_RETRY
    )
    handler = make_flag_press_handler(
        queue_dir=queue_dir,
        submit=worker.submit,
        clock=FakeClock(datetime(2026, 7, 4, 12, 3, 45)),
    )

    handler()
    worker.stop()  # a failed upload is retried by next boot's flush

    assert [p.name for p in queue_dir.iterdir()] == ["FLAG_20260704_120345.marker"]


def test_worker_keeps_uploading_after_a_failure(tmp_path):
    class FlakyStorageClient(StorageClient):
        def __init__(self) -> None:
            self.uploaded: list[str] = []
            self._failed_once = False

        def upload(self, bucket: str, object_path: str, local_path: Path) -> None:
            if not self._failed_once:
                self._failed_once = True
                raise RuntimeError("network down")
            self.uploaded.append(object_path)

    storage = FlakyStorageClient()
    queue_dir = tmp_path / "queue"
    # The in-process retry is held off, so this stays a test of the worker
    # taking the next marker after one failed rather than of the retry.
    worker = FlagUploadWorker(storage, "dev-1", queue_dir, retry_backoff=_NO_RETRY)
    first = write_flag_marker(queue_dir, datetime(2026, 7, 4, 12, 0, 0))
    second = write_flag_marker(queue_dir, datetime(2026, 7, 4, 12, 0, 5))

    worker.submit(first)
    worker.submit(second)
    worker.stop()

    assert storage.uploaded == ["dev-1/FLAG_20260704_120005.marker"]
    assert [p.name for p in (tmp_path / "queue").iterdir()] == [
        "FLAG_20260704_120000.marker"
    ]


def test_press_handler_marker_name_matches_the_pipeline_parser_contract(tmp_path):
    storage = FakeStorageClient()
    queue_dir = tmp_path / "queue"
    worker = FlagUploadWorker(storage, "dev-1", queue_dir)
    handler = make_flag_press_handler(
        queue_dir=queue_dir,
        submit=worker.submit,
        clock=FakeClock(datetime(2026, 7, 4, 12, 3, 45)),
    )

    handler()
    worker.stop()

    filename = storage.uploaded[0].split("/")[-1]
    parsed = datetime.strptime(filename, "FLAG_%Y%m%d_%H%M%S.marker")
    assert parsed == datetime(2026, 7, 4, 12, 3, 45)


@dataclass(frozen=True)
class RecordingBackoff(RetryBackoff):
    """A real backoff that also records the failure count it was indexed by."""

    asked: list[int] = field(default_factory=list)

    def delay_sec(self, consecutive_failures: int) -> float:
        self.asked.append(consecutive_failures)
        return super().delay_sec(consecutive_failures)


class CountingStorageClient(StorageClient):
    """Fails every upload, announcing each attempt."""

    def __init__(self) -> None:
        self.attempts: list[str] = []
        self.attempted = threading.Condition()

    def upload(self, bucket: str, object_path: str, local_path: Path) -> None:
        with self.attempted:
            self.attempts.append(object_path)
            self.attempted.notify_all()
        raise RuntimeError("network down")

    def wait_for_attempts(self, count: int) -> bool:
        with self.attempted:
            return self.attempted.wait_for(lambda: len(self.attempts) >= count, 5)


def test_a_failed_marker_is_retried_in_process_without_a_restart(tmp_path):
    # Before this, a marker whose upload failed sat in the queue until the
    # next boot's flush: a pendant that lost WiFi for a minute held that flag
    # until someone power-cycled it, and the pipeline scored the segment
    # unflagged that night.
    queue_dir = tmp_path / "queue"

    class FailFirstThenSucceed(FakeStorageClient):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0
            self.retry_succeeded = threading.Event()

        def upload(self, bucket, object_path, local_path):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("network down")
            super().upload(bucket, object_path, local_path)
            self.retry_succeeded.set()

    storage = FailFirstThenSucceed()
    worker = FlagUploadWorker(
        storage,
        "dev-1",
        queue_dir,
        retry_backoff=RetryBackoff(base_sec=0.01, max_sec=0.04),
    )
    worker.submit(write_flag_marker(queue_dir, datetime(2026, 7, 4, 12, 0, 0)))

    assert storage.retry_succeeded.wait(timeout=5), (
        "the flag worker never retried the failed marker in process"
    )
    worker.stop()

    assert storage.uploaded == ["dev-1/FLAG_20260704_120000.marker"]
    assert pending_markers(queue_dir) == []


def test_a_failed_marker_is_not_retried_until_the_backoff_elapses(tmp_path):
    # The whole point of the backoff on a battery-powered pendant: one
    # attempt, then a wait, not a hot loop against a dead network.
    queue_dir = tmp_path / "queue"
    storage = CountingStorageClient()
    worker = FlagUploadWorker(
        storage, "dev-1", queue_dir, retry_backoff=RetryBackoff(), monotonic=lambda: 0.0
    )
    worker.submit(write_flag_marker(queue_dir, datetime(2026, 7, 4, 12, 0, 0)))

    assert storage.wait_for_attempts(1)
    # A frozen clock can never reach the 30 s deadline, so a second press is
    # the only thing that wakes the worker - and it must not trigger a round.
    worker.submit(write_flag_marker(queue_dir, datetime(2026, 7, 4, 12, 0, 5)))
    assert storage.wait_for_attempts(2)
    worker.stop()

    assert storage.attempts == [
        "dev-1/FLAG_20260704_120000.marker",
        "dev-1/FLAG_20260704_120005.marker",
    ]
    assert len(pending_markers(queue_dir)) == 2


def test_the_retry_deadline_is_indexed_by_the_workers_own_failure_count(tmp_path):
    # Same schedule as a segment's, driven by this worker's own counter
    # rather than the upload worker's UploadHealth: a marker is metadata, so
    # failing to send one must not by itself escalate the Critical LED.
    queue_dir = tmp_path / "queue"
    backoff = RecordingBackoff(base_sec=0.01, max_sec=0.04)
    storage = CountingStorageClient()
    worker = FlagUploadWorker(storage, "dev-1", queue_dir, retry_backoff=backoff)
    worker.submit(write_flag_marker(queue_dir, datetime(2026, 7, 4, 12, 0, 0)))

    assert storage.wait_for_attempts(4)
    worker.stop()

    # The press's own failure is 1; each retry round that fails adds one.
    assert backoff.asked[:3] == [1, 2, 3]


def test_a_retry_round_stops_at_its_first_failure(tmp_path):
    # One refusal already proves the network is down, and markers are empty
    # files, so trying the rest would spend radio power to learn the same.
    queue_dir = tmp_path / "queue"
    storage = CountingStorageClient()
    # Queued before the worker exists, the way a boot whose startup flush
    # could not reach storage leaves them.
    for second in (0, 5, 10):
        write_flag_marker(queue_dir, datetime(2026, 7, 4, 12, 0, second))
    worker = FlagUploadWorker(
        storage,
        "dev-1",
        queue_dir,
        retry_backoff=RetryBackoff(base_sec=0.01, max_sec=0.01),
    )

    assert storage.wait_for_attempts(2)
    worker.stop()

    # Every attempt is the oldest marker; the other two were never tried.
    assert set(storage.attempts) == {"dev-1/FLAG_20260704_120000.marker"}


def test_flush_markers_uploads_everything_pending_before_it_returns(tmp_path):
    # What keeps a segment from reaching storage ahead of its flags: the
    # upload worker asks for this and waits.
    queue_dir = tmp_path / "queue"
    storage = FakeStorageClient()
    worker = FlagUploadWorker(storage, "dev-1", queue_dir, retry_backoff=_NO_RETRY)
    for second in (0, 5):
        write_flag_marker(queue_dir, datetime(2026, 7, 4, 12, 0, second))

    assert worker.flush_markers() is True

    assert storage.uploaded == [
        "dev-1/FLAG_20260704_120000.marker",
        "dev-1/FLAG_20260704_120005.marker",
    ]
    assert pending_markers(queue_dir) == []
    worker.stop()


def test_flush_markers_reports_what_it_could_not_send(tmp_path):
    queue_dir = tmp_path / "queue"
    worker = FlagUploadWorker(
        FailingStorageClient(), "dev-1", queue_dir, retry_backoff=_NO_RETRY
    )
    write_flag_marker(queue_dir, datetime(2026, 7, 4, 12, 0, 0))

    assert worker.flush_markers() is False

    assert len(pending_markers(queue_dir)) == 1
    worker.stop()


def test_flush_markers_with_nothing_queued_is_a_no_op(tmp_path):
    queue_dir = tmp_path / "queue"
    storage = FakeStorageClient()
    worker = FlagUploadWorker(storage, "dev-1", queue_dir)

    assert worker.flush_markers() is True

    assert storage.uploaded == []
    worker.stop()


def test_flush_markers_does_not_hang_once_the_worker_has_stopped(tmp_path):
    # The upload worker outlives the flag worker on no shutdown path today,
    # but a flush that waited out its timeout would stall a drain for 30 s.
    queue_dir = tmp_path / "queue"
    worker = FlagUploadWorker(FakeStorageClient(), "dev-1", queue_dir)
    worker.stop()
    write_flag_marker(queue_dir, datetime(2026, 7, 4, 12, 0, 0))

    started = time.monotonic()
    assert worker.flush_markers() is False
    assert time.monotonic() - started < 1.0


def test_a_stopping_worker_starts_no_retry_round(tmp_path):
    # Mirrors the upload worker: a shutdown must not be held open uploading a
    # backlog, so the sentinel is collected instead of starting a round.
    queue_dir = tmp_path / "queue"
    storage = CountingStorageClient()
    write_flag_marker(queue_dir, datetime(2026, 7, 4, 12, 0, 0))
    worker = FlagUploadWorker(
        storage,
        "dev-1",
        queue_dir,
        retry_backoff=RetryBackoff(base_sec=0.01, max_sec=0.01),
    )

    assert storage.wait_for_attempts(1)
    with storage.attempted:
        attempts_at_stop = len(storage.attempts)
    worker.stop()

    assert len(storage.attempts) <= attempts_at_stop + 1
    assert len(pending_markers(queue_dir)) == 1


def test_a_marker_a_round_already_sent_is_not_sent_again(tmp_path):
    # Found by the offline simulation. A press hands its path to the worker,
    # but a flush or a retry round can reach the same marker first - they
    # read the queue, not the work item. The second pass used to re-upload it
    # and then crash on unlinking a file that was already gone, killing the
    # only thread that uploads markers for the rest of the run.
    queue_dir = tmp_path / "queue"
    storage = FakeStorageClient()
    worker = FlagUploadWorker(storage, "dev-1", queue_dir, retry_backoff=_NO_RETRY)
    marker = write_flag_marker(queue_dir, datetime(2026, 7, 4, 12, 0, 0))

    assert worker.flush_markers() is True  # the round sends and unlinks it
    worker.submit(marker)  # the press's own hand-off arrives afterwards
    # A later press still goes up, so the worker survived the stale path.
    worker.submit(write_flag_marker(queue_dir, datetime(2026, 7, 4, 12, 0, 5)))
    worker.stop()

    assert storage.uploaded == [
        "dev-1/FLAG_20260704_120000.marker",
        "dev-1/FLAG_20260704_120005.marker",
    ]
    assert pending_markers(queue_dir) == []
