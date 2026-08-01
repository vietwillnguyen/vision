import threading
from datetime import datetime, timedelta
from pathlib import Path

from tests.fakes import FailingStorageClient, FakeClock, FakeStorageClient
from visio_recorder.flag_button import (
    FlagUploadWorker,
    make_flag_press_handler,
    write_flag_marker,
)
from visio_recorder.uploader import StorageClient


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
    worker = FlagUploadWorker(storage, "dev-1")
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
    worker = FlagUploadWorker(storage, "dev-1")
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
    worker = FlagUploadWorker(FailingStorageClient(), "dev-1")
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
    worker = FlagUploadWorker(storage, "dev-1")
    first = write_flag_marker(tmp_path / "queue", datetime(2026, 7, 4, 12, 0, 0))
    second = write_flag_marker(tmp_path / "queue", datetime(2026, 7, 4, 12, 0, 5))

    worker.submit(first)
    worker.submit(second)
    worker.stop()

    assert storage.uploaded == ["dev-1/FLAG_20260704_120005.marker"]
    assert [p.name for p in (tmp_path / "queue").iterdir()] == [
        "FLAG_20260704_120000.marker"
    ]


def test_press_handler_marker_name_matches_the_pipeline_parser_contract(tmp_path):
    storage = FakeStorageClient()
    worker = FlagUploadWorker(storage, "dev-1")
    handler = make_flag_press_handler(
        queue_dir=tmp_path / "queue",
        submit=worker.submit,
        clock=FakeClock(datetime(2026, 7, 4, 12, 3, 45)),
    )

    handler()
    worker.stop()

    filename = storage.uploaded[0].split("/")[-1]
    parsed = datetime.strptime(filename, "FLAG_%Y%m%d_%H%M%S.marker")
    assert parsed == datetime(2026, 7, 4, 12, 3, 45)
