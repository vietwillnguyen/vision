from datetime import datetime, timedelta

from visio_recorder.flag_button import make_flag_press_handler, write_flag_marker

from tests.fakes import FailingStorageClient, FakeClock, FakeStorageClient


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


def test_press_handler_uploads_marker_and_clears_it_from_the_queue(tmp_path):
    queue_dir = tmp_path / "queue"
    storage = FakeStorageClient()
    handler = make_flag_press_handler(
        queue_dir=queue_dir,
        storage_client=storage,
        device_id="dev-1",
        clock=FakeClock(datetime(2026, 7, 4, 12, 3, 45)),
    )

    handler()

    assert storage.uploaded == ["dev-1/FLAG_20260704_120345.marker"]
    assert list(queue_dir.iterdir()) == []


def test_press_handler_leaves_marker_queued_when_upload_fails(tmp_path):
    queue_dir = tmp_path / "queue"
    handler = make_flag_press_handler(
        queue_dir=queue_dir,
        storage_client=FailingStorageClient(),
        device_id="dev-1",
        clock=FakeClock(datetime(2026, 7, 4, 12, 3, 45)),
    )

    handler()  # must not raise: a failed upload is retried by next boot's flush

    assert [p.name for p in queue_dir.iterdir()] == ["FLAG_20260704_120345.marker"]


def test_press_handler_ignores_presses_within_the_cooldown(tmp_path):
    queue_dir = tmp_path / "queue"
    storage = FakeStorageClient()
    # 1s between presses, under the 2s cooldown: only every other press lands.
    handler = make_flag_press_handler(
        queue_dir=queue_dir,
        storage_client=storage,
        device_id="dev-1",
        clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0), step_seconds=1),
        cooldown=timedelta(seconds=2),
    )

    handler()  # 12:00:00 - accepted
    handler()  # 12:00:01 - within cooldown, ignored
    handler()  # 12:00:02 - cooldown elapsed, accepted

    assert storage.uploaded == [
        "dev-1/FLAG_20260704_120000.marker",
        "dev-1/FLAG_20260704_120002.marker",
    ]


def test_press_handler_marker_name_matches_the_pipeline_parser_contract(tmp_path):
    storage = FakeStorageClient()
    handler = make_flag_press_handler(
        queue_dir=tmp_path / "queue",
        storage_client=storage,
        device_id="dev-1",
        clock=FakeClock(datetime(2026, 7, 4, 12, 3, 45)),
    )

    handler()

    filename = storage.uploaded[0].split("/")[-1]
    parsed = datetime.strptime(filename, "FLAG_%Y%m%d_%H%M%S.marker")
    assert parsed == datetime(2026, 7, 4, 12, 3, 45)
