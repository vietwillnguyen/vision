import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from visio_recorder.muxer import SubprocessCommandRunner
from visio_recorder.upload_limit import (
    FfprobeDurationProbe,
    SegmentSplitDeferred,
    SegmentSplitError,
    UploadLimit,
    build_split_command,
    rejected_dir,
    split_segment,
    upload_queued,
)
from visio_recorder.uploader import ObjectAlreadyStoredError, ObjectTooLargeError

MAX = 1000


class FakeProbe:
    def __init__(self, duration_sec: float) -> None:
        self.duration_sec_value = duration_sec
        self.probed: list[Path] = []

    def duration_sec(self, path: Path) -> float:
        self.probed.append(path)
        return self.duration_sec_value


class FakeSplitRunner:
    """Stands in for ``ffmpeg -f segment``: writes parts and the CSV list.

    ``parts`` is a list of (size_bytes, start_sec, end_sec) to produce.
    """

    def __init__(
        self,
        parts: list[tuple[int, float, float]],
        fail: bool = False,
        error: Exception | None = None,
    ):
        self.parts = parts
        self.fail = fail
        self.error = error
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> None:
        self.calls.append(args)
        if self.error is not None:
            raise self.error
        if self.fail:
            raise subprocess.CalledProcessError(1, args)
        pattern = Path(args[-1])
        csv_path = Path(args[args.index("-segment_list") + 1])
        rows = []
        for index, (size, start, end) in enumerate(self.parts):
            part = pattern.parent / (pattern.name % index)
            part.write_bytes(b"x" * size)
            rows.append(f"{part.name},{start:.6f},{end:.6f}")
        csv_path.write_text("\n".join(rows) + "\n")


class RecordingStorage:
    def __init__(
        self,
        too_large_over: int | None = None,
        fail: bool = False,
        already_stored: bool = False,
    ):
        self.too_large_over = too_large_over
        self.fail = fail
        self.already_stored = already_stored
        self.uploaded: list[tuple[str, int]] = []

    def upload(self, bucket: str, object_path: str, local_path: Path) -> None:
        if self.already_stored:
            raise ObjectAlreadyStoredError(f"{object_path} exists but is not visible")
        size = local_path.stat().st_size
        if self.too_large_over is not None and size > self.too_large_over:
            raise ObjectTooLargeError(f"{object_path} is {size} bytes")
        if self.fail:
            raise RuntimeError("network down")
        self.uploaded.append((object_path, size))


def _segment(queue_dir: Path, name: str, size: int) -> Path:
    queue_dir.mkdir(parents=True, exist_ok=True)
    path = queue_dir / name
    path.write_bytes(b"x" * size)
    return path


def test_a_file_within_the_limit_is_returned_untouched(tmp_path):
    path = _segment(tmp_path / "queue", "20261116_090000.mp4", MAX)
    runner = FakeSplitRunner([])

    assert split_segment(path, MAX, runner, FakeProbe(60.0)) == [path]
    assert runner.calls == []


def test_flag_markers_are_never_split(tmp_path):
    path = _segment(tmp_path / "queue", "FLAG_20261116_090000.marker", MAX + 1)

    assert split_segment(path, MAX, FakeSplitRunner([]), FakeProbe(60.0)) == [path]


def test_an_oversized_segment_is_split_into_parts_named_by_their_start(tmp_path):
    queue_dir = tmp_path / "queue"
    path = _segment(queue_dir, "20261116_090000.mp4", 2500)
    runner = FakeSplitRunner([(700, 0.0, 20.0), (700, 20.0, 40.0), (700, 40.0, 61.5)])

    parts = split_segment(path, MAX, runner, FakeProbe(61.5))

    assert [p.name for p in parts] == [
        "20261116_090000.mp4",
        "20261116_090020.mp4",
        "20261116_090040.mp4",
    ]
    assert all(p.stat().st_size == 700 for p in parts)
    # Only the parts are left: no original, no scratch directory.
    assert sorted(p.name for p in queue_dir.iterdir()) == [p.name for p in parts]


def test_split_targets_parts_well_under_the_limit(tmp_path):
    # 2500 bytes at 80 % of a 1000-byte limit needs 4 parts of 15.375 s.
    path = _segment(tmp_path / "queue", "20261116_090000.mp4", 2500)
    runner = FakeSplitRunner([(600, 0.0, 61.5)] * 1)

    with pytest.raises(SegmentSplitError):
        split_segment(path, MAX, runner, FakeProbe(61.5))
    segment_time = runner.calls[0][runner.calls[0].index("-segment_time") + 1]
    assert segment_time == "15.375"


def test_part_start_offsets_are_truncated_to_whole_seconds(tmp_path):
    path = _segment(tmp_path / "queue", "20261116_235950.mp4", 1500)
    runner = FakeSplitRunner([(750, 0.0, 9.97), (750, 9.97, 20.0)])

    parts = split_segment(path, MAX, runner, FakeProbe(20.0))

    # 23:59:50 + 9.97 s crosses no second boundary twice; the name carries
    # the date of the part's own start.
    assert [p.name for p in parts] == ["20261116_235950.mp4", "20261116_235959.mp4"]


def test_split_that_leaves_a_part_over_the_limit_fails_and_keeps_the_original(
    tmp_path,
):
    queue_dir = tmp_path / "queue"
    path = _segment(queue_dir, "20261116_090000.mp4", 2500)
    runner = FakeSplitRunner([(1200, 0.0, 30.0), (1300, 30.0, 60.0)])

    with pytest.raises(SegmentSplitError, match="over"):
        split_segment(path, MAX, runner, FakeProbe(60.0))

    assert sorted(p.name for p in queue_dir.iterdir()) == ["20261116_090000.mp4"]
    assert path.stat().st_size == 2500


def test_split_failure_from_ffmpeg_keeps_the_original(tmp_path):
    queue_dir = tmp_path / "queue"
    path = _segment(queue_dir, "20261116_090000.mp4", 2500)

    with pytest.raises(SegmentSplitError):
        split_segment(path, MAX, FakeSplitRunner([], fail=True), FakeProbe(60.0))

    assert sorted(p.name for p in queue_dir.iterdir()) == ["20261116_090000.mp4"]


def test_a_scratch_directory_left_by_a_power_cut_is_replaced(tmp_path):
    queue_dir = tmp_path / "queue"
    path = _segment(queue_dir, "20261116_090000.mp4", 1500)
    stale = queue_dir / ".20261116_090000.split"
    stale.mkdir()
    (stale / "part_000.mp4").write_bytes(b"stale")
    runner = FakeSplitRunner([(750, 0.0, 30.0), (750, 30.0, 60.0)])

    parts = split_segment(path, MAX, runner, FakeProbe(60.0))

    assert [p.name for p in parts] == ["20261116_090000.mp4", "20261116_090030.mp4"]
    assert not stale.exists()


def test_build_split_command_copies_streams_into_timed_mp4_parts(tmp_path):
    cmd = build_split_command(tmp_path / "in.mp4", 15.375, tmp_path / "work")
    assert cmd == [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(tmp_path / "in.mp4"),
        "-map",
        "0",
        "-c",
        "copy",
        "-f",
        "segment",
        "-segment_time",
        "15.375",
        "-segment_format",
        "mp4",
        "-reset_timestamps",
        "1",
        "-segment_list",
        str(tmp_path / "work" / "parts.csv"),
        "-segment_list_type",
        "csv",
        str(tmp_path / "work" / "part_%03d.mp4"),
    ]


def _limit(runner, duration=60.0, max_bytes=MAX) -> UploadLimit:
    return UploadLimit(max_bytes=max_bytes, runner=runner, probe=FakeProbe(duration))


def test_upload_queued_uploads_a_small_segment_and_removes_it(tmp_path):
    queue_dir = tmp_path / "queue"
    path = _segment(queue_dir, "20261116_090000.mp4", 10)
    storage = RecordingStorage()

    outcome = upload_queued(path, storage, "dev", _limit(FakeSplitRunner([])))

    assert (outcome.uploaded, outcome.failed, outcome.rejected) == (1, 0, 0)
    assert storage.uploaded == [("dev/20261116_090000.mp4", 10)]
    assert not path.exists()


def test_upload_queued_splits_an_oversized_segment_and_uploads_every_part(tmp_path):
    queue_dir = tmp_path / "queue"
    path = _segment(queue_dir, "20261116_090000.mp4", 1500)
    runner = FakeSplitRunner([(750, 0.0, 30.0), (750, 30.0, 60.0)])
    storage = RecordingStorage(too_large_over=MAX)

    outcome = upload_queued(path, storage, "dev", _limit(runner))

    assert (outcome.uploaded, outcome.failed, outcome.rejected) == (2, 0, 0)
    assert storage.uploaded == [
        ("dev/20261116_090000.mp4", 750),
        ("dev/20261116_090030.mp4", 750),
    ]
    assert list(queue_dir.iterdir()) == []


def test_upload_queued_resplits_a_part_storage_still_rejects_as_too_large(tmp_path):
    # The server's limit was lowered below VISIO_MAX_UPLOAD_BYTES: the 900-byte
    # file passes the local check but storage answers 413. It is split again
    # under half its own size instead of being retried forever.
    queue_dir = tmp_path / "queue"
    path = _segment(queue_dir, "20261116_090000.mp4", 900)
    runner = FakeSplitRunner([(300, 0.0, 20.0), (300, 20.0, 40.0), (300, 40.0, 60.0)])
    storage = RecordingStorage(too_large_over=400)

    outcome = upload_queued(path, storage, "dev", _limit(runner))

    assert (outcome.uploaded, outcome.failed, outcome.rejected) == (3, 0, 0)
    assert [name for name, _ in storage.uploaded] == [
        "dev/20261116_090000.mp4",
        "dev/20261116_090020.mp4",
        "dev/20261116_090040.mp4",
    ]


def test_upload_queued_moves_an_unsplittable_segment_aside_and_never_retries_it(
    tmp_path,
):
    queue_dir = tmp_path / "queue"
    path = _segment(queue_dir, "20261116_090000.mp4", 2500)
    runner = FakeSplitRunner([], fail=True)
    storage = RecordingStorage()

    outcome = upload_queued(path, storage, "dev", _limit(runner))

    assert (outcome.uploaded, outcome.failed, outcome.rejected) == (0, 0, 1)
    assert storage.uploaded == []
    assert list(queue_dir.iterdir()) == []
    assert [p.name for p in rejected_dir(queue_dir).iterdir()] == [
        "20261116_090000.mp4"
    ]


def test_upload_queued_sets_aside_a_segment_an_earlier_pairing_stored(tmp_path, caplog):
    # After a re-pair the new login cannot see what the previous one uploaded,
    # so storage answers 409 to a key that looks absent. Retrying it unchanged
    # would fail forever.
    queue_dir = tmp_path / "queue"
    path = _segment(queue_dir, "20261116_090000.mp4", 10)

    with caplog.at_level("ERROR"):
        outcome = upload_queued(
            path,
            RecordingStorage(already_stored=True),
            "dev",
            _limit(FakeSplitRunner([])),
        )

    assert (outcome.uploaded, outcome.failed, outcome.rejected) == (0, 0, 1)
    assert list(queue_dir.iterdir()) == []
    assert [p.name for p in rejected_dir(queue_dir).iterdir()] == [
        "20261116_090000.mp4"
    ]
    assert "20261116_090000.mp4" in caplog.text


def test_upload_queued_keeps_a_segment_queued_on_a_network_failure(tmp_path):
    queue_dir = tmp_path / "queue"
    path = _segment(queue_dir, "20261116_090000.mp4", 10)

    outcome = upload_queued(
        path, RecordingStorage(fail=True), "dev", _limit(FakeSplitRunner([]))
    )

    assert (outcome.uploaded, outcome.failed, outcome.rejected) == (0, 1, 0)
    assert path.exists()


def test_a_full_disk_while_writing_parts_keeps_the_segment_queued(tmp_path):
    queue_dir = tmp_path / "queue"
    path = _segment(queue_dir, "20261116_090000.mp4", 2500)
    runner = FakeSplitRunner([], error=OSError(28, "No space left on device"))

    with pytest.raises(SegmentSplitDeferred):
        split_segment(path, MAX, runner, FakeProbe(60.0))

    outcome = upload_queued(path, RecordingStorage(), "dev", _limit(runner))

    assert (outcome.uploaded, outcome.failed, outcome.rejected) == (0, 1, 0)
    assert sorted(p.name for p in queue_dir.iterdir()) == ["20261116_090000.mp4"]
    assert not rejected_dir(queue_dir).exists()


@pytest.mark.parametrize(
    ("free", "expected"),
    [(0, (0, 1, 0)), (4999, (0, 1, 0)), (5000, (0, 0, 1))],
)
def test_ffmpeg_failing_with_under_twice_the_file_free_keeps_the_segment_queued(
    tmp_path, monkeypatch, free, expected
):
    queue_dir = tmp_path / "queue"
    path = _segment(queue_dir, "20261116_090000.mp4", 2500)
    runner = FakeSplitRunner([], fail=True)
    calls: list[tuple[str, int]] = []

    def disk_usage(probed):
        calls.append((str(probed), len(runner.calls)))
        return SimpleNamespace(free=free)

    monkeypatch.setattr(shutil, "disk_usage", disk_usage)

    outcome = upload_queued(path, RecordingStorage(), "dev", _limit(runner))

    assert (outcome.uploaded, outcome.failed, outcome.rejected) == expected
    assert calls == [(str(queue_dir), 1)]
    kept = sorted(p.name for p in queue_dir.iterdir())
    assert kept == (["20261116_090000.mp4"] if expected[1] else [])


def test_upload_queued_skips_a_path_already_gone(tmp_path):
    outcome = upload_queued(
        tmp_path / "queue" / "20261116_090000.mp4",
        RecordingStorage(),
        "dev",
        _limit(FakeSplitRunner([])),
    )

    assert (outcome.uploaded, outcome.failed, outcome.rejected) == (0, 0, 0)


def test_rejected_dir_sits_beside_the_queue(tmp_path):
    assert rejected_dir(tmp_path / "queue") == tmp_path / "rejected"


requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


@requires_ffmpeg
def test_real_ffmpeg_splits_a_muxed_segment_into_playable_parts(tmp_path):
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir()
    h264 = tmp_path / "20261116_090000.h264"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi",
            "-i", "testsrc2=size=320x240:rate=30,noise=alls=20:allf=t",
            "-t", "12", "-c:v", "libx264", "-preset", "ultrafast",
            "-b:v", "2M", "-g", "30", "-pix_fmt", "yuv420p", "-f", "h264", str(h264),
        ],
        check=True,
    )  # fmt: skip
    mp4 = queue_dir / "20261116_090000.mp4"
    # Exactly firmware's mux_segment command.
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-framerate", "30", "-i", str(h264),
         "-c", "copy", str(mp4)],
        check=True,
    )  # fmt: skip
    size = mp4.stat().st_size
    probe = FfprobeDurationProbe()
    total = probe.duration_sec(mp4)

    parts = split_segment(mp4, size // 2, SubprocessCommandRunner(), probe)

    assert len(parts) >= 3
    assert all(p.stat().st_size <= size // 2 for p in parts)
    assert parts[0].name == "20261116_090000.mp4"
    assert sum(probe.duration_sec(p) for p in parts) == pytest.approx(total, abs=0.1)
