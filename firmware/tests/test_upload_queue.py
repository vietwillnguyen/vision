from visio_recorder.upload_queue import enqueue, list_pending, mark_uploaded


def test_enqueue_moves_file_into_queue_dir(tmp_path):
    source = tmp_path / "source" / "seg.mp4"
    source.parent.mkdir()
    source.write_bytes(b"data")
    queue_dir = tmp_path / "queue"

    dest = enqueue(queue_dir, source)

    assert dest == queue_dir / "seg.mp4"
    assert dest.exists()
    assert not source.exists()


def test_list_pending_returns_sorted_queue_contents(tmp_path):
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir()
    (queue_dir / "20260704_120500.mp4").write_bytes(b"")
    (queue_dir / "20260704_120000.mp4").write_bytes(b"")
    (queue_dir / "FLAG_20260704_120300.marker").touch()

    pending = list_pending(queue_dir)

    assert [p.name for p in pending] == [
        "20260704_120000.mp4",
        "20260704_120500.mp4",
        "FLAG_20260704_120300.marker",
    ]


def test_list_pending_skips_a_split_scratch_directory(tmp_path):
    # A power cut mid-split leaves ".<stem>.split/" in the queue; it is not an
    # upload, and the original beside it is split again.
    queue_dir = tmp_path / "queue"
    (queue_dir / ".20260704_120000.split").mkdir(parents=True)
    (queue_dir / "20260704_120000.mp4").write_bytes(b"")

    assert [p.name for p in list_pending(queue_dir)] == ["20260704_120000.mp4"]


def test_list_pending_on_missing_dir_returns_empty():
    from pathlib import Path

    assert list_pending(Path("/nonexistent/queue")) == []


def test_mark_uploaded_deletes_file(tmp_path):
    path = tmp_path / "seg.mp4"
    path.write_bytes(b"data")

    mark_uploaded(path)

    assert not path.exists()
