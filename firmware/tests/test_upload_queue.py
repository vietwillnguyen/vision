from pathlib import Path

from visio_recorder.upload_queue import (
    enqueue,
    is_flag_marker,
    list_pending,
    mark_uploaded,
    markers_before_segments,
    pending_markers,
)


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


def _queue(tmp_path, *names):
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir(exist_ok=True)
    for name in names:
        (queue_dir / name).write_bytes(b"")
    return queue_dir


def test_is_flag_marker_recognises_only_markers(tmp_path):
    assert is_flag_marker(Path("FLAG_20260704_120000.marker"))
    assert not is_flag_marker(Path("20260704_120000.mp4"))


def test_pending_markers_lists_only_the_markers_in_name_order(tmp_path):
    queue_dir = _queue(
        tmp_path,
        "20260704_120000.mp4",
        "FLAG_20260704_120500.marker",
        "FLAG_20260704_120030.marker",
    )

    assert [p.name for p in pending_markers(queue_dir)] == [
        "FLAG_20260704_120030.marker",
        "FLAG_20260704_120500.marker",
    ]


def test_pending_markers_of_a_missing_queue_is_empty(tmp_path):
    assert pending_markers(tmp_path / "nope") == []


def test_markers_before_segments_sends_every_flag_first(tmp_path):
    # Name order puts digits before "FLAG_", which is exactly backwards: the
    # pipeline scores a segment with the markers that are in storage when it
    # runs, so a drain that stops early must not have left flags behind.
    queue_dir = _queue(
        tmp_path,
        "20260704_120000.mp4",
        "20260704_120100.mp4",
        "FLAG_20260704_120030.marker",
        "FLAG_20260704_120101.marker",
    )

    assert [p.name for p in markers_before_segments(list_pending(queue_dir))] == [
        "FLAG_20260704_120030.marker",
        "FLAG_20260704_120101.marker",
        "20260704_120000.mp4",
        "20260704_120100.mp4",
    ]


def test_markers_before_segments_orders_each_kind_by_time(tmp_path):
    queue_dir = _queue(
        tmp_path,
        "20260704_120100.mp4",
        "20260704_120000.mp4",
        "FLAG_20260704_120500.marker",
        "FLAG_20260704_120030.marker",
    )

    # Reversed input: the ordering is the function's, not the caller's.
    ordered = markers_before_segments(sorted(queue_dir.iterdir(), reverse=True))

    assert [p.name for p in ordered] == [
        "FLAG_20260704_120030.marker",
        "FLAG_20260704_120500.marker",
        "20260704_120000.mp4",
        "20260704_120100.mp4",
    ]


def test_markers_before_segments_keeps_every_entry(tmp_path):
    queue_dir = _queue(
        tmp_path,
        "20260704_120000.mp4",
        "FLAG_20260704_120030.marker",
        "rejected-looking-name.txt",
    )
    pending = list_pending(queue_dir)

    assert sorted(markers_before_segments(pending)) == sorted(pending)
